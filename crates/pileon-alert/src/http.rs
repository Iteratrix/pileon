//! A [`Fetch`] over the network, as polite as the lab's fetcher.
//!
//! Each host gets a request budget (`AppView` 8/s, PLC 5/s, anything else
//! 4/s). 429s, the common 5xx and transport errors are retried with
//! exponential backoff (honoring `Retry-After`) up to seven times; other
//! responses are definitive.

use std::cell::RefCell;
use std::collections::HashMap;
use std::time::Duration;

use pileon_core::appview::{Fetch, FetchError, Reply, Request};
use serde_json::Value;
use tokio::time::Instant;

const USER_AGENT: &str = "pileon-alert/0.1 (github.com/Iteratrix/pileon)";
const RETRIES: u32 = 7;
const RETRYABLE: [u16; 5] = [429, 500, 502, 503, 504];

/// Rate-limited, retrying GETs.
pub struct HttpFetch {
    client: reqwest::Client,
    next: RefCell<HashMap<String, Instant>>,
}

impl HttpFetch {
    /// Creates a client with a 30 s timeout.
    ///
    /// # Errors
    ///
    /// Fails when the TLS backend cannot be set up.
    pub fn new() -> anyhow::Result<Self> {
        let client = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .timeout(Duration::from_secs(30))
            .build()?;
        Ok(Self {
            client,
            next: RefCell::new(HashMap::new()),
        })
    }

    async fn pace(&self, host: &str) {
        let rate = match host {
            "public.api.bsky.app" => 8.0,
            "plc.directory" => 5.0,
            _ => 4.0,
        };
        let interval = Duration::from_secs_f64(1.0 / rate);
        let wait = {
            let mut next = self.next.borrow_mut();
            let now = Instant::now();
            let slot = next.entry(host.to_owned()).or_insert(now);
            let start = (*slot).max(now);
            *slot = start + interval;
            start - now
        };
        if !wait.is_zero() {
            tokio::time::sleep(wait).await;
        }
    }

    async fn fetch(&self, url: &str) -> Result<Reply, FetchError> {
        let host = reqwest::Url::parse(url)
            .ok()
            .and_then(|u| u.host_str().map(str::to_owned))
            .unwrap_or_default();
        let mut reason = String::from("server error");
        for attempt in 0..RETRIES {
            self.pace(&host).await;
            let response = match self.client.get(url).send().await {
                Ok(response) => response,
                Err(err) => {
                    reason = err.to_string();
                    backoff(attempt, None).await;
                    continue;
                }
            };
            let status = response.status().as_u16();
            if RETRYABLE.contains(&status) {
                let retry_after = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok());
                reason = format!("status {status}");
                backoff(attempt, retry_after).await;
                continue;
            }
            if status >= 500 {
                reason = format!("status {status}");
                break;
            }
            let body = match response.bytes().await {
                Ok(bytes) => match serde_json::from_slice(&bytes) {
                    Ok(Value::Object(map)) => Some(map),
                    Ok(_) | Err(_) => None,
                },
                Err(err) => {
                    reason = err.to_string();
                    backoff(attempt, None).await;
                    continue;
                }
            };
            return Ok(Reply { status, body });
        }
        Err(FetchError {
            url: url.to_owned(),
            reason,
        })
    }
}

async fn backoff(attempt: u32, retry_after: Option<u64>) {
    let delay = 2_u64.saturating_pow(attempt).min(60);
    let delay = retry_after.map_or(delay, |r| r.max(delay));
    tokio::time::sleep(Duration::from_secs(delay)).await;
}

impl Fetch for HttpFetch {
    fn get(&self, request: &Request) -> impl Future<Output = Result<Reply, FetchError>> {
        let url = request.url();
        async move { self.fetch(&url).await }
    }
}

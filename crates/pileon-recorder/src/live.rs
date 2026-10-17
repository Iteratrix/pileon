//! The live Jetstream tail: connect, record, checkpoint, reconnect.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;
use std::time::Duration;

use anyhow::Context;
use futures_util::StreamExt;
use log::{info, warn};
use pileon_core::{Collection, Event};
use tokio::time::timeout;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderValue, StatusCode};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

use crate::recorder::{LikePolicy, Recorder};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const IDLE_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_BACKOFF: Duration = Duration::from_secs(60);
const USER_AGENT: &str = "pileon-recorder/0.1 (github.com/Iteratrix/pileon)";

/// Records the live tail of `host` into `root` until SIGINT or SIGTERM.
///
/// # Errors
///
/// Fails when the recording cannot be opened, when free disk space drops
/// below the floor, or when a checkpoint cannot be written. Connection errors
/// are retried with backoff and never end the run.
pub async fn run(root: &Path, host: &str, floor_gib: u64, likes: LikePolicy) -> anyhow::Result<()> {
    let mut recorder = Recorder::open(root, host, floor_gib, likes)?;
    let mut backoff = Duration::from_secs(1);
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let cursor = recorder.last_seq().map(|seq| seq + 1);
        let outcome = tokio::select! {
            () = &mut shutdown => break,
            outcome = session(&mut recorder, host, cursor) => outcome,
        };
        match outcome {
            Ok(received) => {
                if received {
                    backoff = Duration::from_secs(1);
                }
                warn!("stream ended; reconnecting");
            }
            Err(SessionError::CursorTooOld) => {
                record_gap(root, recorder.last_seq())?;
                recorder.forget_cursor();
                continue;
            }
            Err(SessionError::Fatal(err)) => {
                recorder.finish()?;
                return Err(err);
            }
            Err(SessionError::Transient(err)) => {
                warn!("connection problem: {err:#}; retrying in {backoff:?}");
            }
        }
        tokio::select! {
            () = &mut shutdown => break,
            () = tokio::time::sleep(backoff) => {}
        }
        backoff = (backoff * 2).min(MAX_BACKOFF);
    }
    info!("shutdown requested");
    recorder.finish()
}

enum SessionError {
    CursorTooOld,
    Transient(anyhow::Error),
    Fatal(anyhow::Error),
}

async fn session(
    recorder: &mut Recorder,
    host: &str,
    cursor: Option<u64>,
) -> Result<bool, SessionError> {
    let url = subscribe_url(host, cursor);
    let mut request = url
        .as_str()
        .into_client_request()
        .map_err(|err| SessionError::Fatal(err.into()))?;
    let headers = request.headers_mut();
    headers.insert(
        "Sec-WebSocket-Protocol",
        HeaderValue::from_static("xrpc.v1.json"),
    );
    headers.insert("User-Agent", HeaderValue::from_static(USER_AGENT));

    let connected = timeout(CONNECT_TIMEOUT, connect_async(request))
        .await
        .map_err(|_| SessionError::Transient(anyhow::anyhow!("connect timed out")))?;
    let (mut socket, _) = match connected {
        Ok(pair) => pair,
        Err(WsError::Http(response))
            if cursor.is_some() && response.status() == StatusCode::BAD_REQUEST =>
        {
            let body = response
                .body()
                .as_deref()
                .map(String::from_utf8_lossy)
                .unwrap_or_default();
            warn!("cursor {cursor:?} rejected (outside lookback?): {body}");
            return Err(SessionError::CursorTooOld);
        }
        Err(err) => return Err(SessionError::Transient(err.into())),
    };
    info!("connected to {host} at cursor {cursor:?}");

    let mut received = false;
    loop {
        let next = timeout(IDLE_TIMEOUT, socket.next()).await.map_err(|_| {
            SessionError::Transient(anyhow::anyhow!("no message in {IDLE_TIMEOUT:?}"))
        })?;
        let Some(message) = next else {
            return Ok(received);
        };
        let message = message.map_err(|err| SessionError::Transient(err.into()))?;
        match message {
            Message::Text(text) => {
                received = true;
                match Event::from_live_frame(text.as_str()) {
                    Ok(event) => recorder
                        .record(&event)
                        .context("writing row")
                        .map_err(SessionError::Fatal)?,
                    Err(err) => recorder.parse_error(&err),
                }
                recorder.maybe_checkpoint().map_err(SessionError::Fatal)?;
            }
            Message::Binary(bytes) => {
                warn!("ignoring unexpected {}-byte binary frame", bytes.len());
            }
            Message::Close(frame) => {
                info!("server closed the stream: {frame:?}");
                return Ok(received);
            }
            Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {}
        }
    }
}

/// Builds the `subscribeEvents` URL for the recorded collections.
fn subscribe_url(host: &str, cursor: Option<u64>) -> String {
    let mut url = format!(
        "wss://{host}/xrpc/network.bsky.jetstream.subscribeEvents?kinds=commit&kinds=account"
    );
    for collection in Collection::RECORDED {
        url.push_str("&collections=");
        url.push_str(collection.nsid());
    }
    if let Some(cursor) = cursor {
        url.push_str("&cursor=");
        url.push_str(&cursor.to_string());
    }
    url
}

fn record_gap(root: &Path, last_seq: Option<u64>) -> anyhow::Result<()> {
    let now_us = jiff::Timestamp::now().as_microsecond();
    let line = serde_json::json!({ "after_seq": last_seq, "resumed_us": now_us });
    let path = root.join("gaps.jsonl");
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .with_context(|| format!("opening {}", path.display()))?;
    writeln!(file, "{line}")?;
    file.sync_data()?;
    warn!("recorded a gap after seq {last_seq:?}; fill it from the archive");
    Ok(())
}

async fn shutdown_signal() {
    let mut term = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
        Ok(term) => term,
        Err(err) => {
            warn!("cannot listen for SIGTERM: {err}");
            let _ = tokio::signal::ctrl_c().await;
            return;
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscribe_url_names_every_recorded_collection_and_the_cursor() {
        let url = subscribe_url("jetstream.us-west.bsky.network", Some(42));
        assert!(url.starts_with(
            "wss://jetstream.us-west.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents?"
        ));
        assert!(url.contains("kinds=commit&kinds=account"));
        for collection in Collection::RECORDED {
            assert!(url.contains(&format!("collections={}", collection.nsid())));
        }
        assert!(url.ends_with("&cursor=42"));
        assert!(!subscribe_url("h", None).contains("cursor"));
    }
}

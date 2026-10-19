//! The live alert daemon, in shadow mode: alerts are logged, nobody is
//! notified.
//!
//! It warms the surge detector on the recorded partitions (every root, in
//! sequence order, exactly as `pileon-detect` reads them), then follows
//! Jetstream from the last recorded sequence number, feeding the same
//! detector through the same [`feed`]. Each surge is scored the moment it
//! opens, on its trigger snapshot, and the score is appended to
//! `alerts.jsonl`; each surge that closes is appended to `surges.jsonl` so
//! alerts can be judged later against the whole thread.

use std::cell::{Cell, RefCell};
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::time::{Duration, Instant};

use anyhow::Context;
use futures_util::StreamExt;
use log::{info, warn};
use pileon_core::alert::{Reference, Snapshot, gather, signals};
use pileon_core::surge::{Surge, SurgeConfig, SurgeDetector};
use pileon_core::{Event, map_event};
use pileon_detect::feed;
use pileon_detect::output::SurgeLine;
use pileon_detect::stream::{Merged, Row, Table, TableStream, partitions};
use serde_json::{Value, json};
use tokio::time::timeout;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderValue, StatusCode};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

use crate::http::HttpFetch;
use crate::score::describe;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const IDLE_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_BACKOFF: Duration = Duration::from_secs(60);
const REPORT_EVERY: Duration = Duration::from_secs(60);
const USER_AGENT: &str = "pileon-alert/0.1 (github.com/Iteratrix/pileon)";
const COLLECTIONS: [&str; 3] = [
    "app.bsky.feed.post",
    "app.bsky.feed.like",
    "app.bsky.feed.repost",
];

/// Where the daemon reads from and writes to.
pub struct Options {
    /// Recording roots to warm up on.
    pub roots: Vec<PathBuf>,
    /// First day to warm up on (`YYYY-MM-DD`); all history when `None`.
    pub since: Option<String>,
    /// The Jetstream host the recordings came from.
    pub host: String,
    /// The frozen live reference.
    pub reference: Reference,
    /// Directory for `alerts.jsonl` and `surges.jsonl`.
    pub dir: PathBuf,
}

/// Feeds every recorded row to `detector`; returns the last sequence number.
///
/// # Errors
///
/// Fails when a partition cannot be listed or read.
pub fn warm_up(
    detector: &mut SurgeDetector,
    roots: &[PathBuf],
    since: Option<&str>,
) -> anyhow::Result<Option<u64>> {
    let mut streams = Vec::new();
    for root in roots {
        for table in [Table::Posts, Table::Likes, Table::Reposts] {
            let files = partitions(root, table, since, None)
                .with_context(|| format!("listing {}", root.display()))?;
            streams.push(TableStream::new(table, files));
        }
    }
    let mut merged = Merged::new(streams)?;
    let started = Instant::now();
    let mut last_report = Instant::now();
    let (mut rows, mut last_seq) = (0_u64, None);
    while let Some(row) = merged.next_row()? {
        feed(detector, &row);
        rows += 1;
        last_seq = Some(row.seq());
        if last_report.elapsed() >= Duration::from_secs(30) {
            last_report = Instant::now();
            info!(
                "warm-up: {rows} rows in {:.0}s",
                started.elapsed().as_secs_f64()
            );
        }
    }
    info!(
        "warm-up done: {rows} rows in {:.0}s, last seq {last_seq:?}",
        started.elapsed().as_secs_f64()
    );
    Ok(last_seq)
}

/// Appends JSON lines, flushing each so a crash loses nothing written.
struct Log(RefCell<BufWriter<File>>);

impl Log {
    fn open(path: &Path) -> anyhow::Result<Self> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .with_context(|| format!("opening {}", path.display()))?;
        Ok(Self(RefCell::new(BufWriter::new(file))))
    }

    fn write(&self, value: &impl serde::Serialize) {
        let mut out = self.0.borrow_mut();
        let written = serde_json::to_writer(&mut *out, value)
            .map_err(anyhow::Error::from)
            .and_then(|()| Ok(out.write_all(b"\n")?))
            .and_then(|()| Ok(out.flush()?));
        if let Err(err) = written {
            warn!("could not write a log line: {err:#}");
        }
    }
}

fn now_us() -> i64 {
    jiff::Timestamp::now().as_microsecond()
}

async fn alert(fetch: Rc<HttpFetch>, reference: Rc<Reference>, log: Rc<Log>, surge: Surge) {
    let id = format!("{}:{}", surge.target, surge.onset_us);
    let snapshot = Snapshot::from_surge(&surge);
    let mut row = match gather(fetch.as_ref(), &snapshot).await {
        Ok(facts) => describe(
            &id,
            &snapshot,
            &signals(&snapshot, &facts),
            Some(&reference),
        ),
        Err(err) => {
            warn!("{id}: unscored: {err}");
            let mut row = serde_json::Map::new();
            row.insert("incident_id".to_owned(), json!(id));
            row.insert("error".to_owned(), json!(err.to_string()));
            row
        }
    };
    let Surge {
        triggered_us,
        warmup,
        trigger_target_post,
        trigger_quote,
        ..
    } = &surge;
    row.insert("triggered_us".to_owned(), json!(triggered_us));
    row.insert("scored_us".to_owned(), json!(now_us()));
    row.insert("warmup".to_owned(), json!(warmup));
    row.insert("trigger_target_post".to_owned(), json!(trigger_target_post));
    row.insert(
        "trigger_quote".to_owned(),
        trigger_quote
            .as_ref()
            .map_or(Value::Null, |q| json!({"uri": q.uri, "author": q.author})),
    );
    log.write(&row);
}

enum SessionError {
    CursorTooOld,
    Transient(anyhow::Error),
}

struct Follower {
    detector: SurgeDetector,
    fetch: Rc<HttpFetch>,
    reference: Rc<Reference>,
    alerts: Rc<Log>,
    surges: Log,
    last_seq: Option<u64>,
    last_event_us: i64,
    events: u64,
    opened: Rc<Cell<u64>>,
}

impl Follower {
    fn handle(&mut self, frame: &str) {
        let Ok(event) = Event::from_live_frame(frame) else {
            return;
        };
        self.last_seq = Some(event.seq);
        self.last_event_us = event.witnessed_us;
        self.events += 1;
        let Some(row) = map_event(&event).and_then(Row::from_core) else {
            return;
        };
        let (closed, _) = feed(&mut self.detector, &row);
        for surge in &closed {
            self.surges.write(&SurgeLine::new(surge));
        }
        for surge in self.detector.take_opened() {
            self.opened.set(self.opened.get() + 1);
            tokio::task::spawn_local(alert(
                Rc::clone(&self.fetch),
                Rc::clone(&self.reference),
                Rc::clone(&self.alerts),
                surge,
            ));
        }
    }

    async fn session(&mut self, host: &str, report: &mut Instant) -> Result<(), SessionError> {
        let cursor = self.last_seq.map(|seq| seq + 1);
        let mut request = subscribe_url(host, cursor)
            .as_str()
            .into_client_request()
            .map_err(|err| SessionError::Transient(err.into()))?;
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
                return Err(SessionError::CursorTooOld);
            }
            Err(err) => return Err(SessionError::Transient(err.into())),
        };
        info!("connected to {host} at cursor {cursor:?}");
        loop {
            let next = timeout(IDLE_TIMEOUT, socket.next()).await.map_err(|_| {
                SessionError::Transient(anyhow::anyhow!("no message in {IDLE_TIMEOUT:?}"))
            })?;
            let Some(message) = next else {
                return Ok(());
            };
            match message.map_err(|err| SessionError::Transient(err.into()))? {
                Message::Text(text) => self.handle(text.as_str()),
                Message::Close(frame) => {
                    info!("server closed the stream: {frame:?}");
                    return Ok(());
                }
                Message::Binary(_) | Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {}
            }
            if report.elapsed() >= REPORT_EVERY {
                *report = Instant::now();
                let (targets, open) = self.detector.load();
                let lag = (now_us() - self.last_event_us).max(0) / 1_000_000;
                info!(
                    "{} events, lag {lag}s; {targets} targets, {open} open, {} opened",
                    self.events,
                    self.opened.get()
                );
            }
        }
    }
}

fn subscribe_url(host: &str, cursor: Option<u64>) -> String {
    let mut url = format!("wss://{host}/xrpc/network.bsky.jetstream.subscribeEvents?kinds=commit");
    for collection in COLLECTIONS {
        url.push_str("&collections=");
        url.push_str(collection);
    }
    if let Some(cursor) = cursor {
        url.push_str("&cursor=");
        url.push_str(&cursor.to_string());
    }
    url
}

/// Warms up, then follows Jetstream until SIGINT or SIGTERM.
///
/// # Errors
///
/// Fails when warm-up fails or the output files cannot be opened.
pub async fn run(options: Options) -> anyhow::Result<()> {
    let Options {
        roots,
        since,
        host,
        reference,
        dir,
    } = options;
    std::fs::create_dir_all(&dir)?;
    let mut detector = SurgeDetector::new(SurgeConfig::default());
    let last_seq = warm_up(&mut detector, &roots, since.as_deref())?;
    detector.report_openings();
    let mut follower = Follower {
        detector,
        fetch: Rc::new(HttpFetch::new()?),
        reference: Rc::new(reference),
        alerts: Rc::new(Log::open(&dir.join("alerts.jsonl"))?),
        surges: Log::open(&dir.join("surges.jsonl"))?,
        last_seq,
        last_event_us: 0,
        events: 0,
        opened: Rc::new(Cell::new(0)),
    };
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    let mut backoff = Duration::from_secs(1);
    let mut report = Instant::now();
    loop {
        let outcome = tokio::select! {
            () = &mut shutdown => break,
            outcome = follower.session(&host, &mut report) => outcome,
        };
        match outcome {
            Ok(()) => {
                backoff = Duration::from_secs(1);
                warn!("stream ended; reconnecting");
            }
            Err(SessionError::CursorTooOld) => {
                warn!(
                    "cursor {:?} is past Jetstream's lookback; resuming at the tip",
                    follower.last_seq
                );
                follower.last_seq = None;
                continue;
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
    Ok(())
}

async fn shutdown_signal() {
    let interrupt = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let Ok(mut terminate) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        else {
            let _ = interrupt.await;
            return;
        };
        tokio::select! {
            _ = interrupt => {}
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = interrupt.await;
    }
}

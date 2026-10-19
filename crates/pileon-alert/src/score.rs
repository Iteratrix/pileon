//! Replays recorded surges through the live pipeline: the parity check.
//!
//! Each surge line (from a `pileon-detect` run with trigger snapshots)
//! becomes a [`Snapshot`] exactly as the lab's `live_incident` builds it:
//! arrival times at whole seconds from onset, as the compact record stores
//! them. Facts come from a [`Fetch`], normally the lab's cache, and each
//! row is written with the lab's column names so the two can be compared
//! value for value.

use std::collections::HashSet;
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;

use anyhow::Context;
use az::Az;
use pileon_core::alert::{Budget, Quoter, Reference, Signal, Signals, Snapshot, gather, signals};
use pileon_core::appview::Fetch;
use serde::Deserialize;
use serde_json::{Map, Value, json};

/// The fields of a surge line the live pipeline reads.
#[derive(Deserialize)]
struct SurgeIn {
    target: String,
    onset_us: i64,
    triggered_us: i64,
    trigger_target_post: Option<String>,
    trigger_quote: Option<QuoteIn>,
    recorded: Vec<(i64, String, String, String, String, bool)>,
}

#[derive(Deserialize)]
struct QuoteIn {
    uri: String,
    author: String,
}

impl SurgeIn {
    fn id(&self) -> String {
        format!("{}:{}", self.target, self.onset_us)
    }

    fn snapshot(self) -> Snapshot {
        let Self {
            target,
            onset_us,
            triggered_us,
            trigger_target_post,
            trigger_quote,
            recorded,
        } = self;
        let quoter = trigger_quote.map(|QuoteIn { uri, author }| Quoter { uri, author });
        let arrivals = recorded
            .into_iter()
            .map(|(offset_s, author, _, rkey, _, _)| {
                let post = format!("at://{author}/app.bsky.feed.post/{rkey}");
                (author, onset_us + offset_s * 1_000_000, post)
            });
        Snapshot::new(
            target,
            onset_us,
            triggered_us,
            trigger_target_post,
            quoter,
            arrivals,
        )
    }
}

/// Returns one scored row: the signals under the lab's names, the score,
/// its families and whether each frozen cutoff alerts.
pub fn describe(
    id: &str,
    snapshot: &Snapshot,
    found: &Signals,
    reference: Option<&Reference>,
) -> Map<String, Value> {
    let mut row = Map::new();
    row.insert("incident_id".to_owned(), json!(id));
    let cut = (snapshot.triggered_us - snapshot.onset_us).az::<f64>() / 1e6;
    row.insert("live_cut_s".to_owned(), json!(cut));
    row.insert("live_arrivals".to_owned(), json!(found.arrivals));
    for signal in Signal::ALL {
        row.insert(signal.name().to_owned(), json!(found.value(signal)));
    }
    if let Some(reference) = reference {
        let score = reference.score(found);
        let families: Map<String, Value> = score
            .families
            .iter()
            .map(|(family, value)| {
                (
                    json!(family).as_str().unwrap_or_default().to_owned(),
                    json!(value),
                )
            })
            .collect();
        let alerts: Map<String, Value> = [Budget::Five, Budget::Ten, Budget::Twenty]
            .into_iter()
            .filter_map(|budget| {
                let cutoff = reference.cutoff(budget)?;
                let name = json!(budget).as_str()?.to_owned();
                Some((name, json!(score.value.is_some_and(|v| v >= cutoff))))
            })
            .collect();
        row.insert("score".to_owned(), json!(score.value));
        row.insert("families".to_owned(), Value::Object(families));
        row.insert("alerts".to_owned(), Value::Object(alerts));
    }
    row
}

fn lines(path: &Path) -> anyhow::Result<Box<dyn BufRead>> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    Ok(
        if path.extension().and_then(|e| e.to_str()) == Some("zst") {
            Box::new(BufReader::new(zstd::stream::Decoder::new(file)?))
        } else {
            Box::new(BufReader::new(file))
        },
    )
}

/// Reads the `incident_id` of every JSON line in `path`.
///
/// # Errors
///
/// Fails when the file cannot be read or a line is not JSON.
pub fn ids_in(path: &Path) -> anyhow::Result<HashSet<String>> {
    let mut ids = HashSet::new();
    for line in lines(path)?.lines() {
        let row: Value = serde_json::from_str(&line?)?;
        if let Some(id) = row.get("incident_id").and_then(Value::as_str) {
            ids.insert(id.to_owned());
        }
    }
    Ok(ids)
}

/// Scores every surge in `surges` (or those in `wanted`) and writes one row
/// per surge to `out`; returns how many were written.
///
/// # Errors
///
/// Fails on unreadable input, a fetch that gives up, or a failed write.
pub async fn run<F: Fetch>(
    fetch: &F,
    surges: &Path,
    wanted: Option<&HashSet<String>>,
    reference: Option<&Reference>,
    out: &Path,
) -> anyhow::Result<u64> {
    let mut sink = BufWriter::new(File::create(out)?);
    let mut written = 0;
    for line in lines(surges)?.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let surge: SurgeIn = serde_json::from_str(&line).context("parsing a surge line")?;
        let id = surge.id();
        if wanted.is_some_and(|w| !w.contains(&id)) {
            continue;
        }
        let snapshot = surge.snapshot();
        let facts = gather(fetch, &snapshot).await?;
        let found = signals(&snapshot, &facts);
        serde_json::to_writer(&mut sink, &describe(&id, &snapshot, &found, reference))?;
        sink.write_all(b"\n")?;
        written += 1;
    }
    sink.flush()?;
    Ok(written)
}

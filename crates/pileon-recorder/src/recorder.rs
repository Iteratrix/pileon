//! The part both sources share: rows in, durable checkpoints out.

use std::collections::HashSet;
use std::fs::{self, File};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::io::{self, BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use az::Az;
use log::{info, warn};
use pileon_core::rows::{Header, PostRow, SubjectRow};
use pileon_core::{Event, ParseError, Row, Table, map_event};
use serde::Deserialize;

use crate::state::State;
use crate::writer::PartitionedWriter;

const CHECKPOINT_EVERY: Duration = Duration::from_secs(5);
const REPORT_EVERY: Duration = Duration::from_secs(60);
const GIB: u64 = 1 << 30;

/// Free disk space fell below the configured floor; recording stopped cleanly.
#[derive(Debug, thiserror::Error)]
#[error("only {available_gib:.1} GiB free under {root}; refusing to record below {floor_gib} GiB")]
pub struct DiskLow {
    /// Free space at the time of the check.
    pub available_gib: f64,
    /// The configured floor.
    pub floor_gib: u64,
    /// The recording root.
    pub root: String,
}

/// Which likes a recording keeps.
///
/// Likes are about two thirds of all bytes. The live tail keeps every like (a
/// rolling window lets incident analysis see likes that are later deleted);
/// the archive backfill keeps only likes on quote posts, the dunk-endorsement
/// signal, since any other like can be fetched on demand from the app view.
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum LikePolicy {
    /// Keep every like.
    All,
    /// Keep likes whose subject is a quote post seen in this recording.
    OnQuotes,
}

/// Records events into partitions and checkpoints the cursor.
pub struct Recorder {
    writer: PartitionedWriter,
    state: State,
    state_path: PathBuf,
    floor_gib: u64,
    quotes: Option<QuoteIndex>,
    pending_seq: Option<u64>,
    last_checkpoint: Instant,
    stats: Stats,
}

impl Recorder {
    /// Opens the recording at `root` for `host`.
    ///
    /// # Errors
    ///
    /// Fails when the root cannot be prepared, the state is unreadable, or the
    /// state belongs to a different host (sequence numbers are host-specific,
    /// so mixing hosts would corrupt the cursor).
    pub fn open(
        root: &Path,
        host: &str,
        floor_gib: u64,
        likes: LikePolicy,
    ) -> anyhow::Result<Self> {
        let quotes = match likes {
            LikePolicy::All => None,
            LikePolicy::OnQuotes => Some(
                QuoteIndex::rebuild(&root.join(Table::Posts.name()))
                    .context("rebuilding the quote-post index")?,
            ),
        };
        let writer = PartitionedWriter::open(root)
            .with_context(|| format!("opening recording at {}", root.display()))?;
        let state_path = root.join("state.json");
        let state = State::load(&state_path)
            .with_context(|| format!("reading {}", state_path.display()))?
            .unwrap_or_else(|| State::new(host));
        if state.host != host {
            bail!(
                "{} was recorded from {}, not {host}; sequence numbers are not comparable across hosts",
                root.display(),
                state.host
            );
        }
        let recorder = Self {
            writer,
            state,
            state_path,
            floor_gib,
            quotes,
            pending_seq: None,
            last_checkpoint: Instant::now(),
            stats: Stats::new(),
        };
        recorder.check_disk()?;
        Ok(recorder)
    }

    /// Returns the last durably recorded sequence number.
    pub fn last_seq(&self) -> Option<u64> {
        self.state.last_seq
    }

    /// Drops the cursor after the server refused it, so the next connection
    /// starts at the live tip. The caller records the gap first.
    pub fn forget_cursor(&mut self) {
        self.state.last_seq = None;
        self.pending_seq = None;
    }

    /// Records one event.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the row cannot be written.
    pub fn record(&mut self, event: &Event) -> io::Result<()> {
        self.stats.events += 1;
        self.stats.last_witnessed_us = event.witnessed_us;
        self.state.first_seq.get_or_insert(event.seq);
        self.pending_seq = Some(event.seq);
        let Some(row) = map_event(event) else {
            return Ok(());
        };
        if let Some(quotes) = &mut self.quotes {
            match &row {
                Row::Post(post) => quotes.note(post),
                Row::Like(SubjectRow { subject, .. }) if !quotes.contains(subject) => {
                    self.stats.likes_skipped += 1;
                    return Ok(());
                }
                Row::Like(_)
                | Row::Repost(_)
                | Row::Block(_)
                | Row::Postgate(_)
                | Row::Threadgate(_)
                | Row::ListItem(_)
                | Row::ListBlock(_)
                | Row::Delete(_)
                | Row::Account(_)
                | Row::Reject(_) => {}
            }
        }
        self.stats.count(row.table());
        self.writer.write(&row)
    }

    /// Notes an event that failed to parse; its content is not kept.
    pub fn parse_error(&mut self, error: &ParseError) {
        self.stats.parse_errors += 1;
        if self.stats.parse_errors <= 10 {
            warn!("unparseable event: {error}");
        }
    }

    /// Syncs data and saves the cursor if a checkpoint is due.
    ///
    /// # Errors
    ///
    /// Returns [`DiskLow`] when free space is below the floor, or an I/O error
    /// when syncing or saving fails.
    pub fn maybe_checkpoint(&mut self) -> anyhow::Result<()> {
        if self.last_checkpoint.elapsed() < CHECKPOINT_EVERY {
            return Ok(());
        }
        self.checkpoint()?;
        if self.stats.since.elapsed() >= REPORT_EVERY {
            self.stats.report(self.state.last_seq);
        }
        self.check_disk()
    }

    /// Checkpoints, closes every partition, and compresses them.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the final sync, save, or close fails.
    pub fn finish(mut self) -> anyhow::Result<()> {
        self.checkpoint()?;
        self.stats.report(self.state.last_seq);
        self.writer.finish().context("closing partitions")?;
        info!("recording closed at seq {:?}", self.state.last_seq);
        Ok(())
    }

    fn checkpoint(&mut self) -> anyhow::Result<()> {
        self.last_checkpoint = Instant::now();
        let Some(seq) = self.pending_seq.take() else {
            return Ok(());
        };
        self.writer.sync().context("syncing partitions")?;
        self.state.advance(seq);
        self.state
            .save(&self.state_path)
            .with_context(|| format!("saving {}", self.state_path.display()))
    }

    fn check_disk(&self) -> anyhow::Result<()> {
        let root = self.writer.root();
        let available = fs4::available_space(root)
            .with_context(|| format!("measuring free space under {}", root.display()))?;
        if available < self.floor_gib * GIB {
            return Err(DiskLow {
                available_gib: available.az::<f64>() / GIB.az::<f64>(),
                floor_gib: self.floor_gib,
                root: root.display().to_string(),
            }
            .into());
        }
        Ok(())
    }
}

/// The quote posts seen so far, as 64-bit hashes of `(did, rkey)`.
///
/// Hashing keeps the index near 16 bytes per quote; at ~0.25M quotes a day
/// the 72-day backfill needs a few hundred MB. A collision can only keep an
/// extra like, never drop one.
struct QuoteIndex {
    quotes: HashSet<u64>,
}

#[derive(Deserialize)]
struct QuoteLine {
    did: String,
    rkey: String,
    quote: Option<String>,
}

impl QuoteIndex {
    fn rebuild(posts_dir: &Path) -> io::Result<Self> {
        let mut index = Self {
            quotes: HashSet::new(),
        };
        if !posts_dir.exists() {
            return Ok(index);
        }
        let mut stack = vec![posts_dir.to_owned()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir)? {
                let path = entry?.path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                let reader: Box<dyn BufRead> = match path.extension().and_then(|e| e.to_str()) {
                    Some("zst") => Box::new(BufReader::new(zstd::stream::Decoder::new(
                        File::open(&path)?,
                    )?)),
                    Some("jsonl") => Box::new(BufReader::new(File::open(&path)?)),
                    Some(_) | None => continue,
                };
                for line in reader.lines() {
                    let line = line?;
                    let Ok(QuoteLine { did, rkey, quote }) = serde_json::from_str(&line) else {
                        continue;
                    };
                    if quote.is_some() {
                        index.quotes.insert(key(&did, &rkey));
                    }
                }
            }
        }
        info!("quote index rebuilt: {} quote posts", index.quotes.len());
        Ok(index)
    }

    fn note(&mut self, post: &PostRow) {
        let PostRow {
            header: Header { did, .. },
            rkey,
            quote,
            ..
        } = post;
        if quote.is_some() {
            self.quotes.insert(key(did, rkey));
        }
    }

    fn contains(&self, uri: &str) -> bool {
        let Some(path) = uri.strip_prefix("at://") else {
            return false;
        };
        let mut parts = path.splitn(3, '/');
        let (Some(did), Some("app.bsky.feed.post"), Some(rkey)) =
            (parts.next(), parts.next(), parts.next())
        else {
            return false;
        };
        self.quotes.contains(&key(did, rkey))
    }
}

fn key(did: &str, rkey: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    did.hash(&mut hasher);
    rkey.hash(&mut hasher);
    hasher.finish()
}

struct Stats {
    since: Instant,
    events: u64,
    likes_skipped: u64,
    parse_errors: u64,
    rows: [u64; Table::ALL.len()],
    last_witnessed_us: i64,
}

impl Stats {
    fn new() -> Self {
        Self {
            since: Instant::now(),
            events: 0,
            likes_skipped: 0,
            parse_errors: 0,
            rows: [0; Table::ALL.len()],
            last_witnessed_us: 0,
        }
    }

    fn count(&mut self, table: Table) {
        if let Some(slot) = Table::ALL.iter().position(|t| *t == table) {
            self.rows[slot] += 1;
        }
    }

    fn report(&mut self, last_seq: Option<u64>) {
        let secs = self.since.elapsed().as_secs_f64().max(1e-3);
        let now_us = jiff::Timestamp::now().as_microsecond();
        let lag_s = (now_us - self.last_witnessed_us).az::<f64>() / 1e6;
        let per_table: Vec<String> = Table::ALL
            .iter()
            .zip(self.rows)
            .filter(|(_, n)| *n > 0)
            .map(|(table, n)| format!("{}={n}", table.name()))
            .collect();
        info!(
            "{:.0} events/s over {secs:.0}s, lag {lag_s:.1}s, seq {last_seq:?}, parse errors {}, likes skipped {}; rows: {}",
            self.events.az::<f64>() / secs,
            self.parse_errors,
            self.likes_skipped,
            per_table.join(" ")
        );
        *self = Self {
            last_witnessed_us: self.last_witnessed_us,
            ..Self::new()
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(seq: u64, collection: &str, rkey: &str, record: &str) -> Event {
        let json = format!(
            r#"{{"$type":"message","payload":{{
            "$type":"network.bsky.jetstream.subscribeEvents#commit",
            "did":"did:plc:dunker","seq":{seq},"time":"2026-10-17T00:00:{seq:02}Z",
            "operation":"create","collection":"{collection}","rkey":"{rkey}","record":{record}}}}}"#
        );
        Event::from_live_frame(&json).unwrap()
    }

    fn quote_post(seq: u64, rkey: &str) -> Event {
        frame(
            seq,
            "app.bsky.feed.post",
            rkey,
            r#"{"text":"x","createdAt":"2026-10-17T00:00:00Z","embed":{"$type":"app.bsky.embed.record",
               "record":{"uri":"at://did:plc:target/app.bsky.feed.post/t1","cid":"c"}}}"#,
        )
    }

    fn like(seq: u64, subject: &str) -> Event {
        let record = format!(
            r#"{{"subject":{{"uri":"{subject}","cid":"c"}},"createdAt":"2026-10-17T00:00:00Z"}}"#
        );
        frame(seq, "app.bsky.feed.like", &format!("l{seq}"), &record)
    }

    fn like_rows(root: &Path) -> usize {
        let mut rows = 0;
        for entry in fs::read_dir(root.join("likes/2026-10-17")).unwrap() {
            let file = File::open(entry.unwrap().path()).unwrap();
            rows += BufReader::new(zstd::stream::Decoder::new(file).unwrap())
                .lines()
                .count();
        }
        rows
    }

    #[test]
    fn on_quotes_policy_keeps_only_likes_of_known_quote_posts() {
        let dir = tempfile::tempdir().unwrap();
        let mut recorder = Recorder::open(dir.path(), "h", 0, LikePolicy::OnQuotes).unwrap();
        recorder.record(&quote_post(1, "q1")).unwrap();
        recorder
            .record(&like(2, "at://did:plc:dunker/app.bsky.feed.post/q1"))
            .unwrap();
        recorder
            .record(&like(3, "at://did:plc:someone/app.bsky.feed.post/plain"))
            .unwrap();
        recorder.finish().unwrap();
        assert_eq!(like_rows(dir.path()), 1);
        let state = State::load(&dir.path().join("state.json"))
            .unwrap()
            .unwrap();
        assert_eq!((state.first_seq, state.last_seq), (Some(1), Some(3)));

        let mut reopened = Recorder::open(dir.path(), "h", 0, LikePolicy::OnQuotes).unwrap();
        reopened
            .record(&like(4, "at://did:plc:dunker/app.bsky.feed.post/q1"))
            .unwrap();
        reopened.finish().unwrap();
        assert_eq!(like_rows(dir.path()), 2, "index must survive a restart");
    }

    #[test]
    fn all_policy_keeps_every_like_and_host_mismatch_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let mut recorder = Recorder::open(dir.path(), "h", 0, LikePolicy::All).unwrap();
        recorder
            .record(&like(5, "at://did:plc:someone/app.bsky.feed.post/plain"))
            .unwrap();
        recorder.finish().unwrap();
        assert_eq!(like_rows(dir.path()), 1);
        let Err(err) = Recorder::open(dir.path(), "other-host", 0, LikePolicy::All) else {
            panic!("a different host must be refused");
        };
        assert!(err.to_string().contains("not comparable"));
    }
}

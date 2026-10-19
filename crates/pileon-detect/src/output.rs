//! Surges as written: one JSON object per line, arrivals compacted.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use anyhow::Context;
use pileon_core::surge::{ArrivalKind, QuotePost, RecordedArrival, Surge};
use serde::Serialize;

/// One surge as written: the detector's record with arrivals compacted to
/// `[seconds from onset, author, kind, post rkey, target-post rkey, stranger]`.
/// Both URIs are recoverable: an arrival post lives in its author's repo and a
/// target post in the target's.
#[derive(Serialize)]
pub struct SurgeLine<'a> {
    target: &'a str,
    onset_us: i64,
    triggered_us: i64,
    trigger_target_post: Option<&'a str>,
    trigger_quote: Option<&'a QuotePost>,
    last_us: i64,
    warmup: bool,
    baseline_per_hour: f64,
    trigger_strangers: u32,
    trigger_surprise: f64,
    peak_strangers: u32,
    peak_surprise: f64,
    arrivals: u32,
    strangers: u32,
    replies: u32,
    quotes: u32,
    mentions: u32,
    target_posts: &'a [(String, u32)],
    quote_posts: &'a [QuotePost],
    recorded: Vec<(i64, &'a str, &'static str, &'a str, &'a str, bool)>,
    truncated: bool,
}

impl<'a> SurgeLine<'a> {
    /// Compacts `surge` for writing.
    pub fn new(surge: &'a Surge) -> Self {
        let Surge {
            target,
            onset_us,
            triggered_us,
            trigger_target_post,
            trigger_quote,
            last_us,
            warmup,
            baseline_per_hour,
            trigger_strangers,
            trigger_surprise,
            peak_strangers,
            peak_surprise,
            arrivals,
            strangers,
            replies,
            quotes,
            mentions,
            target_posts,
            quote_posts,
            recorded,
            truncated,
        } = surge;
        let recorded = recorded
            .iter()
            .map(
                |RecordedArrival {
                     at_us,
                     author,
                     kind,
                     post,
                     target_post,
                     stranger,
                 }| {
                    let kind = match kind {
                        ArrivalKind::Reply => "r",
                        ArrivalKind::Quote => "q",
                        ArrivalKind::Mention => "m",
                    };
                    (
                        (at_us - onset_us).div_euclid(1_000_000),
                        author.as_str(),
                        kind,
                        rkey(post),
                        target_post.as_deref().map_or("", rkey),
                        *stranger,
                    )
                },
            )
            .collect();
        Self {
            target,
            onset_us: *onset_us,
            triggered_us: *triggered_us,
            trigger_target_post: trigger_target_post.as_deref(),
            trigger_quote: trigger_quote.as_ref(),
            last_us: *last_us,
            warmup: *warmup,
            baseline_per_hour: *baseline_per_hour,
            trigger_strangers: *trigger_strangers,
            trigger_surprise: *trigger_surprise,
            peak_strangers: *peak_strangers,
            peak_surprise: *peak_surprise,
            arrivals: *arrivals,
            strangers: *strangers,
            replies: *replies,
            quotes: *quotes,
            mentions: *mentions,
            target_posts,
            quote_posts,
            recorded,
            truncated: *truncated,
        }
    }
}

fn rkey(uri: &str) -> &str {
    uri.rsplit('/').next().unwrap_or(uri)
}

/// Writes surges as JSON lines, counting them.
pub struct SurgeWriter {
    out: Box<dyn Write>,
    /// Surges written.
    pub written: u64,
    /// Of those, surges in warm-up.
    pub warmup: u64,
}

impl SurgeWriter {
    /// Writes JSON lines to `path`, zstd-compressed when it ends in `.zst`.
    ///
    /// # Errors
    ///
    /// Fails when `path` cannot be created.
    pub fn create(path: &Path) -> anyhow::Result<Self> {
        let file = File::create(path).with_context(|| format!("creating {}", path.display()))?;
        let out: Box<dyn Write> = if path.extension().and_then(|e| e.to_str()) == Some("zst") {
            Box::new(BufWriter::new(
                zstd::stream::Encoder::new(file, 6)?.auto_finish(),
            ))
        } else {
            Box::new(BufWriter::new(file))
        };
        Ok(Self {
            out,
            written: 0,
            warmup: 0,
        })
    }

    /// Writes each surge as one line.
    ///
    /// # Errors
    ///
    /// Fails when a write fails.
    pub fn write(&mut self, surges: Vec<Surge>) -> anyhow::Result<()> {
        for surge in surges {
            self.written += 1;
            self.warmup += u64::from(surge.warmup);
            serde_json::to_writer(&mut self.out, &SurgeLine::new(&surge))?;
            self.out.write_all(b"\n")?;
        }
        Ok(())
    }

    /// Flushes buffered lines.
    ///
    /// # Errors
    ///
    /// Fails when the flush fails.
    pub fn flush(&mut self) -> anyhow::Result<()> {
        self.out.flush()?;
        Ok(())
    }
}

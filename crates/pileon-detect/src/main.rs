//! `pileon-detect`: finds surges in recorded Jetstream partitions.
//!
//! Reads the `posts`, `likes` and `reposts` tables from one or more recording
//! roots, merges them into one stream in sequence order, runs the surge
//! detector over it, and writes each closed surge as one JSON line.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::Context;
use az::Az;
use clap::Parser;
use log::info;
use pileon_core::surge::{
    ArrivalKind, EngagementKind, PostEvent, QuotePost, RecordedArrival, Surge, SurgeConfig,
    SurgeDetector, event_time_us,
};
use serde::Serialize;

mod stream;

use stream::{Merged, PostLine, Row, SubjectLine, Table, TableStream, partitions};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    /// Recording roots to read (repeat for several; overlapping sequence
    /// numbers from the same host are read once).
    #[arg(long, required = true)]
    root: Vec<PathBuf>,
    /// Where to write surges, one JSON object per line.
    #[arg(long)]
    out: PathBuf,
    /// First day to read (YYYY-MM-DD, witness time).
    #[arg(long)]
    since: Option<String>,
    /// Last day to read (YYYY-MM-DD, witness time).
    #[arg(long)]
    until: Option<String>,
    /// Skip likes and reposts (faster; quote-post engagement stays empty).
    #[arg(long)]
    posts_only: bool,
    #[command(flatten)]
    dials: Dials,
}

/// Detector dials; defaults match [`SurgeConfig::default`].
#[derive(clap::Args)]
struct Dials {
    /// Window that arrivals are counted in, minutes.
    #[arg(long, default_value_t = 60)]
    window_minutes: i64,
    /// Fewest distinct strangers in the window that can open a surge.
    #[arg(long, default_value_t = 8)]
    min_strangers: u32,
    /// Least surprise (-log10 Poisson tail vs the baseline) that opens a surge.
    #[arg(long, default_value_t = 6.0)]
    min_surprise: f64,
    /// Half-life of each target's baseline, hours.
    #[arg(long, default_value_t = 168.0)]
    half_life_hours: f64,
    /// Baseline for targets with no history, strangers per day.
    #[arg(long, default_value_t = 0.5)]
    prior_per_day: f64,
    /// Days an interaction keeps two accounts from being strangers.
    #[arg(long, default_value_t = 30)]
    stranger_days: u32,
    /// Interacting pairs per day the stranger memory is sized for.
    #[arg(long, default_value_t = 6_000_000)]
    pairs_per_day: u64,
    /// Arrivals kept before a surge opens, minutes.
    #[arg(long, default_value_t = 180)]
    lookback_minutes: i64,
    /// Close a surge after this many hours without a stranger.
    #[arg(long, default_value_t = 6)]
    quiet_hours: i64,
    /// Close a surge after this many hours regardless.
    #[arg(long, default_value_t = 48)]
    max_hours: i64,
    /// Flag surges in the first this-many days as warm-up.
    #[arg(long, default_value_t = 14)]
    warmup_days: i64,
    /// Most arrivals kept per surge.
    #[arg(long, default_value_t = 2_000)]
    max_recorded: usize,
}

impl From<&Dials> for SurgeConfig {
    fn from(dials: &Dials) -> Self {
        let Dials {
            window_minutes,
            min_strangers,
            min_surprise,
            half_life_hours,
            prior_per_day,
            stranger_days,
            pairs_per_day,
            lookback_minutes,
            quiet_hours,
            max_hours,
            warmup_days,
            max_recorded,
        } = dials;
        Self {
            window_minutes: *window_minutes,
            min_strangers: *min_strangers,
            min_surprise: *min_surprise,
            baseline_half_life_hours: *half_life_hours,
            prior_per_day: *prior_per_day,
            stranger_days: *stranger_days,
            pairs_per_day: *pairs_per_day,
            lookback_minutes: *lookback_minutes,
            quiet_hours: *quiet_hours,
            max_hours: *max_hours,
            warmup_days: *warmup_days,
            max_recorded: *max_recorded,
        }
    }
}

fn main() -> anyhow::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let cli = Cli::parse();
    let tables: &[Table] = if cli.posts_only {
        &[Table::Posts]
    } else {
        &[Table::Posts, Table::Likes, Table::Reposts]
    };
    let mut streams = Vec::new();
    for root in &cli.root {
        for &table in tables {
            let files = partitions(root, table, cli.since.as_deref(), cli.until.as_deref())
                .with_context(|| format!("listing {}", root.display()))?;
            info!("{}: {:?} {} partitions", root.display(), table, files.len());
            streams.push(TableStream::new(table, files));
        }
    }
    let mut merged = Merged::new(streams)?;
    let mut detector = SurgeDetector::new(SurgeConfig::from(&cli.dials));
    let mut out = SurgeWriter::create(&cli.out)?;
    let mut rows: u64 = 0;
    let started = Instant::now();
    let mut last_report = Instant::now();
    let mut last_time_us = 0;

    while let Some(row) = merged.next_row()? {
        rows += 1;
        let closed = match &row {
            Row::Post(PostLine {
                ts,
                did,
                rkey,
                created_at,
                parent,
                quote,
                mentions,
                ..
            }) => {
                let at_us = event_time_us(*ts, created_at.as_deref());
                last_time_us = at_us;
                let uri = format!("at://{did}/app.bsky.feed.post/{rkey}");
                detector.observe_post(&PostEvent {
                    at_us,
                    author: did,
                    uri: &uri,
                    parent: parent.as_deref(),
                    quote: quote.as_deref(),
                    mentions,
                })
            }
            Row::Like(SubjectLine {
                ts,
                created_at,
                subject,
                ..
            }) => detector.observe_engagement(
                subject,
                EngagementKind::Like,
                event_time_us(*ts, created_at.as_deref()),
            ),
            Row::Repost(SubjectLine {
                ts,
                created_at,
                subject,
                ..
            }) => detector.observe_engagement(
                subject,
                EngagementKind::Repost,
                event_time_us(*ts, created_at.as_deref()),
            ),
        };
        out.write(closed)?;
        if last_report.elapsed().as_secs() >= 30 {
            last_report = Instant::now();
            let (targets, open) = detector.load();
            let when = jiff::Timestamp::from_microsecond(last_time_us)
                .map(|t| t.strftime("%Y-%m-%d %H:%M").to_string())
                .unwrap_or_default();
            info!(
                "{rows} rows ({:.0}/s), at {when}; {targets} targets, {open} open, {} surges",
                rows.az::<f64>() / started.elapsed().as_secs_f64(),
                out.written
            );
        }
    }
    out.write(detector.finish())?;
    out.flush()?;
    info!(
        "done: {rows} rows in {:.0}s; {} surges ({} in warm-up); {} duplicate rows skipped",
        started.elapsed().as_secs_f64(),
        out.written,
        out.warmup,
        merged.duplicates
    );
    Ok(())
}

/// One surge as written: the detector's record with arrivals compacted to
/// `[seconds from onset, author, kind, post rkey, target-post rkey, stranger]`.
/// Both URIs are recoverable: an arrival post lives in its author's repo and a
/// target post in the target's.
#[derive(Serialize)]
struct SurgeLine<'a> {
    target: &'a str,
    onset_us: i64,
    triggered_us: i64,
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
    fn new(surge: &'a Surge) -> Self {
        let Surge {
            target,
            onset_us,
            triggered_us,
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

struct SurgeWriter {
    out: Box<dyn Write>,
    written: u64,
    warmup: u64,
}

impl SurgeWriter {
    /// Writes JSON lines to `path`, zstd-compressed when it ends in `.zst`.
    fn create(path: &Path) -> anyhow::Result<Self> {
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

    fn write(&mut self, surges: Vec<Surge>) -> anyhow::Result<()> {
        for surge in surges {
            self.written += 1;
            self.warmup += u64::from(surge.warmup);
            serde_json::to_writer(&mut self.out, &SurgeLine::new(&surge))?;
            self.out.write_all(b"\n")?;
        }
        Ok(())
    }

    fn flush(&mut self) -> anyhow::Result<()> {
        self.out.flush()?;
        Ok(())
    }
}

//! `pileon-alert`: the live structural detector (`lab/PREREGISTRATION.md`,
//! amendment 2), in shadow mode.
//!
//! `live` warms the surge detector on recorded partitions, follows Jetstream
//! and scores each surge the moment it opens, logging alerts without
//! notifying anyone. `score` replays recorded surges through the same
//! pipeline against the lab's response cache, so its rows can be compared
//! with the lab's `detector live` rows value for value.

use std::path::PathBuf;

use anyhow::Context;
use clap::{Parser, Subcommand};
use log::info;
use pileon_core::alert::Reference;

mod cache;
mod http;
mod live;
mod score;

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Follow Jetstream and log a scored alert for every surge as it opens.
    Live {
        /// Recording roots to warm up on (repeat for several).
        #[arg(long, required = true)]
        root: Vec<PathBuf>,
        /// First day to warm up on (YYYY-MM-DD); default: all history.
        #[arg(long)]
        since: Option<String>,
        /// Jetstream host the recordings came from.
        #[arg(long, default_value = "jetstream.us-west.bsky.network")]
        host: String,
        /// The frozen live reference.
        #[arg(long)]
        reference: PathBuf,
        /// Directory for alerts.jsonl and surges.jsonl.
        #[arg(long)]
        dir: PathBuf,
    },
    /// Score recorded surges from the lab's response cache (parity check).
    Score {
        /// Surges from a pileon-detect run with trigger snapshots.
        #[arg(long)]
        surges: PathBuf,
        /// The lab's response cache.
        #[arg(long)]
        cache: PathBuf,
        /// Only surges whose ids appear in this JSON-lines file.
        #[arg(long)]
        ids_from: Option<PathBuf>,
        /// The frozen live reference; without it, signals only.
        #[arg(long)]
        reference: Option<PathBuf>,
        /// Where to write one row per surge.
        #[arg(long)]
        out: PathBuf,
    },
}

fn read_reference(path: &PathBuf) -> anyhow::Result<Reference> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    Ok(Reference::from_json(&text)?)
}

fn main() -> anyhow::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_err()
    {
        log::debug!("a rustls crypto provider was already installed");
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let local = tokio::task::LocalSet::new();
    match Cli::parse().command {
        Command::Live {
            root,
            since,
            host,
            reference,
            dir,
        } => {
            let options = live::Options {
                roots: root,
                since,
                host,
                reference: read_reference(&reference)?,
                dir,
            };
            local.block_on(&runtime, live::run(options))
        }
        Command::Score {
            surges,
            cache,
            ids_from,
            reference,
            out,
        } => {
            let fetch = cache::CacheFetch::open(&cache)?;
            let wanted = ids_from.as_deref().map(score::ids_in).transpose()?;
            let reference = reference.as_ref().map(read_reference).transpose()?;
            let written = local.block_on(
                &runtime,
                score::run(&fetch, &surges, wanted.as_ref(), reference.as_ref(), &out),
            )?;
            info!(
                "wrote {written} rows to {}; {} cache misses",
                out.display(),
                fetch.misses()
            );
            Ok(())
        }
    }
}

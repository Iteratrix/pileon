//! `pileon-record`: records Bluesky interaction edges from Jetstream v2.
//!
//! Two sources, one mapping: `live` tails the WebSocket; `ingest` reads the
//! replay script's SDK event lines from stdin. Both write hourly zstd JSONL
//! partitions plus a `state.json` cursor under their `--root`.

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use log::error;

mod ingest;
mod live;
mod recorder;
mod state;
mod writer;

/// Exit status when free disk space is below the floor (`EX_TEMPFAIL`); the
/// systemd unit lists it in `RestartPreventExitStatus` so a full disk stops
/// the service instead of looping.
const EXIT_DISK_LOW: u8 = 75;

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Tail the live Jetstream WebSocket until SIGINT or SIGTERM.
    Live {
        #[command(flatten)]
        common: Common,
    },
    /// Record replay-script SDK event lines read from stdin.
    Ingest {
        #[command(flatten)]
        common: Common,
    },
}

#[derive(clap::Args)]
struct Common {
    /// Directory for this source's partitions and `state.json`.
    #[arg(long)]
    root: PathBuf,
    /// Jetstream host; sequence numbers are only meaningful per host.
    #[arg(long, default_value = "jetstream.us-west.bsky.network")]
    host: String,
    /// Stop recording when free space under `--root` drops below this many GiB.
    #[arg(long, default_value_t = 20)]
    min_free_gib: u64,
    /// Which likes to keep.
    #[arg(long, value_enum, default_value_t = recorder::LikePolicy::All)]
    likes: recorder::LikePolicy,
}

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Live { common } => run_live(&common),
        Command::Ingest { common } => {
            let Common {
                root,
                host,
                min_free_gib,
                likes,
            } = common;
            ingest::run(&root, &host, min_free_gib, likes)
        }
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) if err.is::<recorder::DiskLow>() => {
            error!("{err:#}");
            ExitCode::from(EXIT_DISK_LOW)
        }
        Err(err) => {
            error!("{err:#}");
            ExitCode::FAILURE
        }
    }
}

fn run_live(common: &Common) -> anyhow::Result<()> {
    let Common {
        root,
        host,
        min_free_gib,
        likes,
    } = common;
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_err()
    {
        log::debug!("a rustls crypto provider was already installed");
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(live::run(root, host, *min_free_gib, *likes))
}

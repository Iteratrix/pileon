//! Archive replay: SDK events as JSON lines on stdin.
//!
//! The replay script (`replay/replay.mjs`) drives the official Jetstream SDK,
//! which plans, downloads, and decodes the archive, and prints each event as
//! one JSON line. This module records them with the same mapping as the live
//! tail, so the two sources produce identical rows.

use std::io::{self, BufRead, BufReader};
use std::path::Path;

use anyhow::Context;
use log::info;
use pileon_core::Event;

use crate::recorder::{LikePolicy, Recorder};

/// Records SDK event lines from stdin into `root` until end of input.
///
/// Events at or below the recording's last durable sequence number are
/// skipped, so restarting the replay from that cursor never duplicates rows.
///
/// # Errors
///
/// Fails when the recording cannot be opened, stdin cannot be read, a row or
/// checkpoint cannot be written, or free disk space drops below the floor.
pub fn run(root: &Path, host: &str, floor_gib: u64, likes: LikePolicy) -> anyhow::Result<()> {
    let mut recorder = Recorder::open(root, host, floor_gib, likes)?;
    let resume_after = recorder.last_seq();
    info!("ingesting from stdin; skipping seq <= {resume_after:?}");
    let mut input = BufReader::with_capacity(1 << 20, io::stdin().lock());
    let mut line = String::new();
    let mut skipped: u64 = 0;
    loop {
        line.clear();
        if input.read_line(&mut line).context("reading stdin")? == 0 {
            break;
        }
        let text = line.trim_end();
        if text.is_empty() {
            continue;
        }
        let event = match Event::from_sdk_line(text) {
            Ok(event) => event,
            Err(err) => {
                recorder.parse_error(&err);
                continue;
            }
        };
        if resume_after.is_some_and(|after| event.seq <= after) {
            skipped += 1;
            continue;
        }
        recorder.record(&event).context("writing row")?;
        recorder.maybe_checkpoint()?;
    }
    if skipped > 0 {
        info!("skipped {skipped} already-recorded events");
    }
    recorder.finish()
}

//! The durable cursor: how far a source has been recorded.

use std::fs;
use std::io::{self, Write};
use std::path::Path;

use serde::{Deserialize, Serialize};

/// Progress of one recording source, persisted as `state.json`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct State {
    /// The Jetstream host whose sequence space `first_seq`/`last_seq` refer to.
    pub host: String,
    /// The sequence number of the first event this source ever recorded.
    pub first_seq: Option<u64>,
    /// The last sequence number known to be durably recorded.
    pub last_seq: Option<u64>,
    /// When this state was written, in microseconds since the Unix epoch.
    pub updated_us: i64,
}

impl State {
    /// Returns an empty state for `host`.
    pub fn new(host: &str) -> Self {
        Self {
            host: host.to_owned(),
            first_seq: None,
            last_seq: None,
            updated_us: 0,
        }
    }

    /// Reads the state at `path`, or `None` when no state exists yet.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the file exists but cannot be read or parsed.
    pub fn load(path: &Path) -> io::Result<Option<Self>> {
        match fs::read(path) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(err),
        }
    }

    /// Records that every event up to `seq` is durable.
    pub fn advance(&mut self, seq: u64) {
        self.last_seq = Some(seq);
    }

    /// Writes the state atomically (temp file, fsync, rename).
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the write or rename fails.
    pub fn save(&mut self, path: &Path) -> io::Result<()> {
        self.updated_us = jiff::Timestamp::now().as_microsecond();
        let partial = path.with_extension("json.tmp");
        let mut file = fs::File::create(&partial)?;
        serde_json::to_writer_pretty(&mut file, self)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&partial, path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_round_trips_and_keeps_first_seq() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        assert_eq!(State::load(&path).unwrap(), None);

        let mut state = State::new("jetstream.us-west.bsky.network");
        state.first_seq = Some(10);
        state.advance(25);
        state.save(&path).unwrap();

        let loaded = State::load(&path).unwrap().unwrap();
        assert_eq!(loaded.first_seq, Some(10));
        assert_eq!(loaded.last_seq, Some(25));
        assert_eq!(loaded.host, "jetstream.us-west.bsky.network");
        assert!(!path.with_extension("json.tmp").exists());
    }
}

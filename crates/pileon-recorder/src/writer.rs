//! Hourly, per-table JSONL partitions, compressed when the hour closes.
//!
//! Layout: `<root>/<table>/<YYYY-MM-DD>/<HH>-<first seq>.jsonl[.zst]`, hours in
//! UTC witness time. A partition is plain JSONL while open, so a crash loses at
//! most a torn final line, never a corrupt compressed frame. When a table moves
//! to a new hour the old file is handed to a background thread that writes
//! `.jsonl.zst` next to it and only then removes the plain file. Leftover plain
//! files from an unclean exit are repaired and compressed on the next open.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread::JoinHandle;

use jiff::Timestamp;
use jiff::tz::TimeZone;
use log::{error, info, warn};
use pileon_core::{Row, Table};

const HOUR_US: i64 = 3_600_000_000;
const ZSTD_LEVEL: i32 = 6;

/// Writes rows into hourly partitions under one root directory.
pub struct PartitionedWriter {
    root: PathBuf,
    open: HashMap<Table, OpenPartition>,
    compressor: Compressor,
}

struct OpenPartition {
    hour: i64,
    path: PathBuf,
    out: BufWriter<File>,
}

impl PartitionedWriter {
    /// Opens a writer rooted at `root`, recovering partitions an unclean exit
    /// left uncompressed.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the root cannot be created or scanned.
    pub fn open(root: &Path) -> io::Result<Self> {
        fs::create_dir_all(root)?;
        let compressor = Compressor::spawn();
        let leftovers = plain_partitions(root)?;
        if !leftovers.is_empty() {
            info!("recovering {} uncompressed partitions", leftovers.len());
        }
        for path in leftovers {
            truncate_torn_line(&path)?;
            compressor.submit(path);
        }
        Ok(Self {
            root: root.to_owned(),
            open: HashMap::new(),
            compressor,
        })
    }

    /// Appends one row to its table's partition for the row's hour.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when the partition cannot be created or written.
    pub fn write(&mut self, row: &Row) -> io::Result<()> {
        let table = row.table();
        let header = row.header();
        let hour = header.ts.div_euclid(HOUR_US);
        let rotate = self
            .open
            .get(&table)
            .is_none_or(|partition| partition.hour != hour);
        if rotate {
            if let Some(partition) = self.open.remove(&table) {
                self.close(partition)?;
            }
            let partition = self.create(table, hour, header.seq)?;
            self.open.insert(table, partition);
        }
        let Some(partition) = self.open.get_mut(&table) else {
            return Err(io::Error::other("partition vanished after rotation"));
        };
        serde_json::to_writer(&mut partition.out, row)?;
        partition.out.write_all(b"\n")
    }

    /// Flushes every open partition to stable storage.
    ///
    /// Call this before persisting a cursor, so the cursor never runs ahead of
    /// the data it vouches for.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when a flush or fsync fails.
    pub fn sync(&mut self) -> io::Result<()> {
        for partition in self.open.values_mut() {
            partition.out.flush()?;
            partition.out.get_ref().sync_data()?;
        }
        Ok(())
    }

    /// Closes and compresses every partition, then waits for compression to
    /// finish.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when a final flush fails.
    pub fn finish(mut self) -> io::Result<()> {
        let open: Vec<OpenPartition> = self.open.drain().map(|(_, p)| p).collect();
        for partition in open {
            self.close(partition)?;
        }
        self.compressor.shutdown();
        Ok(())
    }

    /// Returns the root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn create(&self, table: Table, hour: i64, seq: u64) -> io::Result<OpenPartition> {
        let start = Timestamp::from_microsecond(hour * HOUR_US)
            .map_err(io::Error::other)?
            .to_zoned(TimeZone::UTC);
        let dir = self
            .root
            .join(table.name())
            .join(start.strftime("%Y-%m-%d").to_string());
        fs::create_dir_all(&dir)?;
        let stem = format!("{}-{seq}", start.strftime("%H"));
        let mut path = dir.join(format!("{stem}.jsonl"));
        let mut attempt = 1;
        while path.exists() || path.with_extension("jsonl.zst").exists() {
            path = dir.join(format!("{stem}.{attempt}.jsonl"));
            attempt += 1;
        }
        let file = OpenOptions::new()
            .create_new(true)
            .append(true)
            .open(&path)?;
        Ok(OpenPartition {
            hour,
            path,
            out: BufWriter::with_capacity(1 << 20, file),
        })
    }

    fn close(&self, partition: OpenPartition) -> io::Result<()> {
        let OpenPartition { path, out, .. } = partition;
        let file = out.into_inner().map_err(io::IntoInnerError::into_error)?;
        file.sync_data()?;
        drop(file);
        self.compressor.submit(path);
        Ok(())
    }
}

struct Compressor {
    jobs: Option<Sender<PathBuf>>,
    worker: Option<JoinHandle<()>>,
}

impl Compressor {
    fn spawn() -> Self {
        let (jobs, queue) = mpsc::channel::<PathBuf>();
        let worker = std::thread::spawn(move || {
            for path in queue {
                if let Err(err) = compress(&path) {
                    error!("compressing {} failed (kept plain): {err}", path.display());
                }
            }
        });
        Self {
            jobs: Some(jobs),
            worker: Some(worker),
        }
    }

    fn submit(&self, path: PathBuf) {
        let Some(jobs) = &self.jobs else {
            warn!("compressor stopped; leaving {} plain", path.display());
            return;
        };
        if jobs.send(path).is_err() {
            warn!("compressor thread is gone; partition left plain");
        }
    }

    fn shutdown(&mut self) {
        self.jobs = None;
        if let Some(worker) = self.worker.take()
            && worker.join().is_err()
        {
            error!("compressor thread panicked");
        }
    }
}

impl Drop for Compressor {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn compress(path: &Path) -> io::Result<()> {
    let target = path.with_extension("jsonl.zst");
    let partial = path.with_extension("jsonl.zst.tmp");
    let mut source = BufReader::new(File::open(path)?);
    let mut encoder = zstd::stream::Encoder::new(File::create(&partial)?, ZSTD_LEVEL)?;
    encoder.include_checksum(true)?;
    io::copy(&mut source, &mut encoder)?;
    let file = encoder.finish()?;
    file.sync_all()?;
    drop(file);
    fs::rename(&partial, &target)?;
    fs::remove_file(path)?;
    Ok(())
}

fn plain_partitions(root: &Path) -> io::Result<Vec<PathBuf>> {
    let mut found = Vec::new();
    let mut stack = vec![root.to_owned()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir)? {
            let path = entry?.path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            match path.extension().and_then(|ext| ext.to_str()) {
                Some("jsonl") => found.push(path),
                Some("tmp") => fs::remove_file(&path)?,
                Some(_) | None => {}
            }
        }
    }
    found.sort();
    Ok(found)
}

fn truncate_torn_line(path: &Path) -> io::Result<()> {
    let mut file = OpenOptions::new().read(true).write(true).open(path)?;
    let len = file.metadata()?.len();
    if len == 0 {
        return Ok(());
    }
    let window = len.min(1 << 20);
    file.seek(SeekFrom::Start(len - window))?;
    let mut tail = Vec::new();
    Read::by_ref(&mut file)
        .take(window)
        .read_to_end(&mut tail)?;
    if tail.last() == Some(&b'\n') {
        return Ok(());
    }
    let keep = match tail.iter().rposition(|&b| b == b'\n') {
        Some(newline) => len - window + u64::try_from(newline).map_err(io::Error::other)? + 1,
        None => len - window,
    };
    warn!(
        "truncating torn final line in {} ({} bytes)",
        path.display(),
        len - keep
    );
    file.set_len(keep)?;
    file.sync_data()
}

#[cfg(test)]
mod tests {
    use super::*;
    use pileon_core::{Event, map_event};

    fn like_row(seq: u64, time: &str) -> Row {
        let frame = format!(
            r#"{{"$type":"message","payload":{{
            "$type":"network.bsky.jetstream.subscribeEvents#commit",
            "did":"did:plc:a","seq":{seq},"time":"{time}","operation":"create",
            "collection":"app.bsky.feed.like","rkey":"r{seq}",
            "record":{{"subject":{{"uri":"at://s","cid":"c"}},"createdAt":"{time}"}}}}}}"#
        );
        map_event(&Event::from_live_frame(&frame).unwrap()).unwrap()
    }

    fn read_zst(path: &Path) -> String {
        let mut text = String::new();
        zstd::stream::Decoder::new(File::open(path).unwrap())
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        text
    }

    #[test]
    fn rows_rotate_by_hour_and_compress_on_finish() {
        let dir = tempfile::tempdir().unwrap();
        let mut writer = PartitionedWriter::open(dir.path()).unwrap();
        writer.write(&like_row(1, "2026-10-17T00:10:00Z")).unwrap();
        writer.write(&like_row(2, "2026-10-17T00:50:00Z")).unwrap();
        writer.write(&like_row(3, "2026-10-17T01:05:00Z")).unwrap();
        writer.finish().unwrap();

        let day = dir.path().join("likes/2026-10-17");
        let first = read_zst(&day.join("00-1.jsonl.zst"));
        assert_eq!(first.lines().count(), 2);
        assert!(first.starts_with(r#"{"seq":1,"#));
        let second = read_zst(&day.join("01-3.jsonl.zst"));
        assert_eq!(second.lines().count(), 1);
        assert!(!day.join("00-1.jsonl").exists());
    }

    #[test]
    fn reopening_recovers_and_repairs_plain_leftovers() {
        let dir = tempfile::tempdir().unwrap();
        let day = dir.path().join("posts/2026-10-17");
        fs::create_dir_all(&day).unwrap();
        fs::write(day.join("05-9.jsonl"), "{\"seq\":9}\n{\"seq\":10,\"tor").unwrap();
        fs::write(day.join("05-1.jsonl.zst.tmp"), "half a frame").unwrap();

        let writer = PartitionedWriter::open(dir.path()).unwrap();
        writer.finish().unwrap();

        assert_eq!(read_zst(&day.join("05-9.jsonl.zst")), "{\"seq\":9}\n");
        assert!(!day.join("05-9.jsonl").exists());
        assert!(!day.join("05-1.jsonl.zst.tmp").exists());
    }

    #[test]
    fn sync_makes_open_partitions_readable() {
        let dir = tempfile::tempdir().unwrap();
        let mut writer = PartitionedWriter::open(dir.path()).unwrap();
        writer.write(&like_row(7, "2026-10-17T03:00:00Z")).unwrap();
        writer.sync().unwrap();
        let plain = fs::read_to_string(dir.path().join("likes/2026-10-17/03-7.jsonl")).unwrap();
        assert_eq!(plain.lines().count(), 1);
        writer.finish().unwrap();
    }
}

//! Recorded partitions, read back as one stream in sequence order.
//!
//! The recorder writes each table as hourly files, `<root>/<table>/<date>/
//! <HH>-<first seq>.jsonl[.zst]`. Within one root and table, sorting files by
//! date, hour and first sequence number yields rows in sequence order; a
//! k-way merge then interleaves tables and roots. Rows from the same host
//! carry the same sequence numbers in every root, so a repeated sequence
//! number (the live/replay overlap) is the same event and is skipped.

use std::cmp::Reverse;
use std::collections::BinaryHeap;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader};
use std::path::{Path, PathBuf};

use pileon_core::Row as CoreRow;
use pileon_core::rows::{Header, PostRow, SubjectRow};
use serde::Deserialize;

/// A row from one of the tables the detector reads.
#[derive(Debug, Clone, PartialEq)]
pub enum Row {
    /// A post edge.
    Post(PostLine),
    /// A like.
    Like(SubjectLine),
    /// A repost.
    Repost(SubjectLine),
}

impl Row {
    /// Returns the row the recorder would have written for a live event's
    /// core row, for the tables the detector reads.
    pub fn from_core(row: pileon_core::Row) -> Option<Self> {
        let subject = |SubjectRow {
                           header: Header { seq, ts, .. },
                           created_at,
                           subject,
                           ..
                       }: SubjectRow| SubjectLine {
            seq,
            ts,
            created_at,
            subject,
        };
        match row {
            CoreRow::Post(PostRow {
                header: Header { seq, ts, did },
                rkey,
                created_at,
                parent,
                quote,
                mentions,
                ..
            }) => Some(Self::Post(PostLine {
                seq,
                ts,
                did,
                rkey,
                created_at,
                parent,
                quote,
                mentions,
            })),
            CoreRow::Like(like) => Some(Self::Like(subject(like))),
            CoreRow::Repost(repost) => Some(Self::Repost(subject(repost))),
            CoreRow::Block(_)
            | CoreRow::Follow(_)
            | CoreRow::Postgate(_)
            | CoreRow::Threadgate(_)
            | CoreRow::ListItem(_)
            | CoreRow::ListBlock(_)
            | CoreRow::Delete(_)
            | CoreRow::Account(_)
            | CoreRow::Reject(_) => None,
        }
    }

    /// Returns the event's sequence number on its Jetstream host.
    pub fn seq(&self) -> u64 {
        match self {
            Self::Post(PostLine { seq, .. })
            | Self::Like(SubjectLine { seq, .. })
            | Self::Repost(SubjectLine { seq, .. }) => *seq,
        }
    }
}

/// A `posts` row, as the recorder writes it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct PostLine {
    /// The event's sequence number.
    pub seq: u64,
    /// Witness time, microseconds.
    pub ts: i64,
    /// The author.
    pub did: String,
    /// The record key.
    pub rkey: String,
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The post replied to.
    pub parent: Option<String>,
    /// The record quoted.
    pub quote: Option<String>,
    /// Mentioned accounts.
    #[serde(default)]
    pub mentions: Vec<String>,
}

/// A `likes` or `reposts` row.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SubjectLine {
    /// The event's sequence number.
    pub seq: u64,
    /// Witness time, microseconds.
    pub ts: i64,
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The liked or reposted record.
    pub subject: String,
}

/// Which table a stream reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Table {
    /// `posts`.
    Posts,
    /// `likes`.
    Likes,
    /// `reposts`.
    Reposts,
}

impl Table {
    fn dir(self) -> &'static str {
        match self {
            Self::Posts => "posts",
            Self::Likes => "likes",
            Self::Reposts => "reposts",
        }
    }

    fn parse(self, line: &str) -> Option<Row> {
        match self {
            Self::Posts => serde_json::from_str(line).ok().map(Row::Post),
            Self::Likes => serde_json::from_str(line).ok().map(Row::Like),
            Self::Reposts => serde_json::from_str(line).ok().map(Row::Repost),
        }
    }
}

/// Lists one table's partitions under `root` in sequence order, keeping only
/// days within `[since, until]` (inclusive, `YYYY-MM-DD`) when given.
///
/// # Errors
///
/// Returns an I/O error when a directory cannot be read.
pub fn partitions(
    root: &Path,
    table: Table,
    since: Option<&str>,
    until: Option<&str>,
) -> io::Result<Vec<PathBuf>> {
    let dir = root.join(table.dir());
    if !dir.exists() {
        return Ok(Vec::new());
    }
    let mut days: Vec<(String, PathBuf)> = fs::read_dir(&dir)?
        .filter_map(Result::ok)
        .filter(|e| e.path().is_dir())
        .map(|e| (e.file_name().to_string_lossy().into_owned(), e.path()))
        .filter(|(day, _)| since.is_none_or(|s| day.as_str() >= s))
        .filter(|(day, _)| until.is_none_or(|u| day.as_str() <= u))
        .collect();
    days.sort();
    let mut files = Vec::new();
    for (_, day) in days {
        let mut hours: Vec<(u32, u64, String, PathBuf)> = fs::read_dir(&day)?
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let path = entry.path();
                let name = path.file_name()?.to_str()?.to_owned();
                let stem = name
                    .strip_suffix(".jsonl.zst")
                    .or_else(|| name.strip_suffix(".jsonl"))?;
                let (hour, rest) = stem.split_once('-')?;
                let first_seq = rest.split('.').next()?.parse().ok()?;
                Some((hour.parse().ok()?, first_seq, name, path))
            })
            .collect();
        hours.sort();
        files.extend(hours.into_iter().map(|(_, _, _, path)| path));
    }
    Ok(files)
}

/// Rows of one table under one root, in sequence order.
pub struct TableStream {
    table: Table,
    files: std::vec::IntoIter<PathBuf>,
    reader: Option<Box<dyn BufRead>>,
    line: String,
}

impl TableStream {
    /// Streams `files` (already in order) as rows of `table`.
    pub fn new(table: Table, files: Vec<PathBuf>) -> Self {
        Self {
            table,
            files: files.into_iter(),
            reader: None,
            line: String::new(),
        }
    }

    /// Returns the next row, skipping lines that do not parse (a torn final
    /// line in a partition still being written).
    ///
    /// # Errors
    ///
    /// Returns an I/O error when a partition cannot be opened or decoded.
    pub fn next_row(&mut self) -> io::Result<Option<Row>> {
        loop {
            let Some(reader) = self.reader.as_mut() else {
                let Some(path) = self.files.next() else {
                    return Ok(None);
                };
                self.reader = Some(open(&path)?);
                continue;
            };
            self.line.clear();
            if reader.read_line(&mut self.line)? == 0 {
                self.reader = None;
                continue;
            }
            if let Some(row) = self.table.parse(self.line.trim_end()) {
                return Ok(Some(row));
            }
        }
    }
}

/// Opens a partition. An open partition the recorder sealed (compressed)
/// since it was listed is read from its sealed file instead.
fn open(path: &Path) -> io::Result<Box<dyn BufRead>> {
    let sealed = path.with_extension("jsonl.zst");
    let (file, path) = match File::open(path) {
        Ok(file) => (file, path),
        Err(err)
            if err.kind() == io::ErrorKind::NotFound
                && path.extension() == Some("jsonl".as_ref()) =>
        {
            (File::open(&sealed)?, sealed.as_path())
        }
        Err(err) => return Err(err),
    };
    let compressed = path.extension().and_then(|e| e.to_str()) == Some("zst");
    if compressed {
        Ok(Box::new(BufReader::with_capacity(
            1 << 20,
            zstd::stream::Decoder::new(file)?,
        )))
    } else {
        Ok(Box::new(BufReader::with_capacity(1 << 20, file)))
    }
}

/// Several table streams merged into one, in sequence order, without repeats.
pub struct Merged {
    streams: Vec<TableStream>,
    heap: BinaryHeap<Reverse<(u64, usize)>>,
    pending: Vec<Option<Row>>,
    last_seq: Option<u64>,
    /// Rows skipped because an earlier stream already yielded their seq.
    pub duplicates: u64,
}

impl Merged {
    /// Starts merging `streams`.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when a stream's first row cannot be read.
    pub fn new(mut streams: Vec<TableStream>) -> io::Result<Self> {
        let mut heap = BinaryHeap::new();
        let mut pending = Vec::with_capacity(streams.len());
        for (index, stream) in streams.iter_mut().enumerate() {
            let row = stream.next_row()?;
            if let Some(row) = &row {
                heap.push(Reverse((row.seq(), index)));
            }
            pending.push(row);
        }
        Ok(Self {
            streams,
            heap,
            pending,
            last_seq: None,
            duplicates: 0,
        })
    }

    /// Returns the next row in sequence order.
    ///
    /// # Errors
    ///
    /// Returns an I/O error when a stream cannot be read.
    pub fn next_row(&mut self) -> io::Result<Option<Row>> {
        while let Some(Reverse((seq, index))) = self.heap.pop() {
            let row = self.pending[index].take();
            let next = self.streams[index].next_row()?;
            if let Some(next_row) = &next {
                self.heap.push(Reverse((next_row.seq(), index)));
            }
            self.pending[index] = next;
            let Some(row) = row else { continue };
            if self.last_seq == Some(seq) {
                self.duplicates += 1;
                continue;
            }
            self.last_seq = Some(seq);
            return Ok(Some(row));
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_zst(path: &Path, lines: &[String]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let mut encoder = zstd::stream::Encoder::new(File::create(path).unwrap(), 3).unwrap();
        for line in lines {
            writeln!(encoder, "{line}").unwrap();
        }
        encoder.finish().unwrap();
    }

    fn post(seq: u64) -> String {
        format!(
            r#"{{"seq":{seq},"ts":{seq},"did":"did:plc:a","rkey":"r{seq}","op":"create","parent":"at://did:plc:t/app.bsky.feed.post/1"}}"#
        )
    }

    fn like(seq: u64) -> String {
        format!(
            r#"{{"seq":{seq},"ts":{seq},"did":"did:plc:b","rkey":"l{seq}","subject":"at://x"}}"#
        )
    }

    #[test]
    fn merges_tables_and_roots_in_order_and_drops_overlap() {
        let dir = tempfile::tempdir().unwrap();
        let replay = dir.path().join("replay");
        let live = dir.path().join("live");
        write_zst(
            &replay.join("posts/2026-08-05/15-1.jsonl.zst"),
            &[post(1), post(4)],
        );
        write_zst(
            &replay.join("posts/2026-08-05/16-6.jsonl.zst"),
            &[post(6), post(9)],
        );
        write_zst(
            &replay.join("likes/2026-08-05/15-2.jsonl.zst"),
            &[like(2), like(7)],
        );
        write_zst(
            &live.join("posts/2026-08-05/16-9.jsonl.zst"),
            &[post(9), post(11)],
        );
        fs::write(
            live.join("posts/2026-08-05/17-12.jsonl"),
            format!("{}\n{{\"seq\":13,\"ts\":", post(12)),
        )
        .unwrap();

        let mut streams = Vec::new();
        for root in [&replay, &live] {
            for table in [Table::Posts, Table::Likes] {
                streams.push(TableStream::new(
                    table,
                    partitions(root, table, None, None).unwrap(),
                ));
            }
        }
        let mut merged = Merged::new(streams).unwrap();
        let mut seqs = Vec::new();
        while let Some(row) = merged.next_row().unwrap() {
            seqs.push(row.seq());
        }
        assert_eq!(seqs, vec![1, 2, 4, 6, 7, 9, 11, 12]);
        assert_eq!(merged.duplicates, 1);
    }

    #[test]
    fn a_partition_sealed_after_listing_is_read_sealed() {
        let dir = tempfile::tempdir().unwrap();
        let open = dir.path().join("posts/2026-08-05/17-1.jsonl");
        fs::create_dir_all(open.parent().unwrap()).unwrap();
        fs::write(&open, format!("{}\n", post(1))).unwrap();
        let files = partitions(dir.path(), Table::Posts, None, None).unwrap();
        fs::remove_file(&open).unwrap();
        write_zst(&open.with_extension("jsonl.zst"), &[post(1), post(2)]);
        let mut stream = TableStream::new(Table::Posts, files);
        let mut seqs = Vec::new();
        while let Some(row) = stream.next_row().unwrap() {
            seqs.push(row.seq());
        }
        assert_eq!(seqs, vec![1, 2]);
    }

    #[test]
    fn partitions_honor_the_day_range() {
        let dir = tempfile::tempdir().unwrap();
        for day in ["2026-08-05", "2026-08-06", "2026-08-07"] {
            write_zst(
                &dir.path().join(format!("posts/{day}/00-1.jsonl.zst")),
                &[post(1)],
            );
        }
        let files = partitions(
            dir.path(),
            Table::Posts,
            Some("2026-08-06"),
            Some("2026-08-06"),
        )
        .unwrap();
        assert_eq!(files.len(), 1);
        assert!(files[0].to_string_lossy().contains("2026-08-06"));
    }
}

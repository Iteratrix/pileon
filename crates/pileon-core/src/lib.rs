//! Core data model for the pileon project.
//!
//! The crate is pure: it parses Jetstream events and maps them to the compact
//! rows the recorder stores, with no I/O. The same parsing serves the live
//! WebSocket tail and the archive replay, so the two sources can never drift
//! apart in what they record.
//!
//! Rows never carry post text. A pile-on is a graph event (who replied to,
//! quoted, liked, reposted, or blocked whom, and when), and keeping text
//! network-wide would make honoring deletions an ongoing obligation. Text for
//! candidate threads is hydrated on demand from the public app view instead.

pub mod bloom;
pub mod event;
pub mod poisson;
pub mod records;
pub mod rows;
pub mod surge;

pub use event::{Collection, Event, EventKind, Operation, ParseError};
pub use rows::{Row, Table, map_event};

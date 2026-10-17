//! Jetstream v2 events in one canonical shape.
//!
//! Jetstream delivers the same events in two layouts. The live WebSocket wraps
//! a flat payload in a message envelope; the replay SDK yields a nested object
//! with the commit fields under `commit`. Both parse into [`Event`] here, at the
//! boundary, so nothing downstream knows which source an event came from.

use serde::Deserialize;
use serde_json::value::RawValue;

/// A Jetstream event, as delivered by the live tail or the replay SDK.
#[derive(Debug)]
pub struct Event {
    /// The repository (account) the event belongs to.
    pub did: String,
    /// The host's sequence number; only comparable within one Jetstream host.
    pub seq: u64,
    /// When Jetstream witnessed the event, in microseconds since the Unix epoch.
    pub witnessed_us: i64,
    /// What kind of event this is.
    pub kind: EventKind,
}

/// The payload of an [`Event`].
#[derive(Debug)]
pub enum EventKind {
    /// A record was created, updated, or deleted.
    Commit(Commit),
    /// An account's hosting status changed.
    Account(AccountStatus),
    /// A handle or DID document changed.
    Identity,
    /// A repository was re-synchronized.
    Sync,
    /// An event type this crate does not recognize.
    Unknown(String),
}

/// A record-level change in one repository.
#[derive(Debug)]
pub struct Commit {
    /// Whether the record was created, updated, or deleted.
    pub operation: Operation,
    /// The record's collection.
    pub collection: Collection,
    /// The record key within the collection.
    pub rkey: String,
    /// The record body; absent on deletes.
    pub record: Option<Box<RawValue>>,
}

/// A record operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operation {
    /// A new record.
    Create,
    /// A replacement of an existing record.
    Update,
    /// A removal of an existing record.
    Delete,
}

/// The collections the recorder stores, plus everything else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Collection {
    /// `app.bsky.feed.post`.
    Post,
    /// `app.bsky.feed.like`.
    Like,
    /// `app.bsky.feed.repost`.
    Repost,
    /// `app.bsky.graph.block`.
    Block,
    /// `app.bsky.graph.follow`.
    Follow,
    /// `app.bsky.feed.postgate`.
    Postgate,
    /// `app.bsky.feed.threadgate`.
    Threadgate,
    /// `app.bsky.graph.listitem`.
    ListItem,
    /// `app.bsky.graph.listblock`.
    ListBlock,
    /// Any other collection NSID.
    Other(String),
}

impl Collection {
    /// Every collection the recorder subscribes to.
    pub const RECORDED: [Collection; 9] = [
        Collection::Post,
        Collection::Like,
        Collection::Repost,
        Collection::Block,
        Collection::Follow,
        Collection::Postgate,
        Collection::Threadgate,
        Collection::ListItem,
        Collection::ListBlock,
    ];

    /// Parses a collection NSID.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::Collection;
    /// assert_eq!(Collection::from_nsid("app.bsky.feed.like"), Collection::Like);
    /// assert_eq!(
    ///     Collection::from_nsid("xyz.example.thing"),
    ///     Collection::Other("xyz.example.thing".to_owned()),
    /// );
    /// ```
    pub fn from_nsid(nsid: &str) -> Self {
        match nsid {
            "app.bsky.feed.post" => Self::Post,
            "app.bsky.feed.like" => Self::Like,
            "app.bsky.feed.repost" => Self::Repost,
            "app.bsky.graph.block" => Self::Block,
            "app.bsky.graph.follow" => Self::Follow,
            "app.bsky.feed.postgate" => Self::Postgate,
            "app.bsky.feed.threadgate" => Self::Threadgate,
            "app.bsky.graph.listitem" => Self::ListItem,
            "app.bsky.graph.listblock" => Self::ListBlock,
            other => Self::Other(other.to_owned()),
        }
    }

    /// Returns the collection NSID.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::Collection;
    /// assert_eq!(Collection::Post.nsid(), "app.bsky.feed.post");
    /// ```
    pub fn nsid(&self) -> &str {
        match self {
            Self::Post => "app.bsky.feed.post",
            Self::Like => "app.bsky.feed.like",
            Self::Repost => "app.bsky.feed.repost",
            Self::Block => "app.bsky.graph.block",
            Self::Follow => "app.bsky.graph.follow",
            Self::Postgate => "app.bsky.feed.postgate",
            Self::Threadgate => "app.bsky.feed.threadgate",
            Self::ListItem => "app.bsky.graph.listitem",
            Self::ListBlock => "app.bsky.graph.listblock",
            Self::Other(nsid) => nsid,
        }
    }
}

/// An account's hosting status, from an `account` event.
#[derive(Debug, Clone, Deserialize)]
pub struct AccountStatus {
    /// Whether the account is currently active.
    pub active: bool,
    /// Why the account is inactive (`deleted`, `deactivated`, `takendown`, ...).
    pub status: Option<String>,
}

/// An event that could not be parsed.
#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    /// The input was not valid JSON for either event layout.
    #[error("malformed event JSON: {0}")]
    Json(#[from] serde_json::Error),
    /// The event timestamp was not an RFC 3339 instant.
    #[error("bad event time {time:?}: {source}")]
    Time {
        /// The offending timestamp.
        time: String,
        /// Why it failed to parse.
        source: jiff::Error,
    },
    /// A commit event lacked a field every commit carries.
    #[error("commit event missing {0}")]
    MissingField(&'static str),
    /// A commit carried an operation this crate does not know.
    #[error("unknown commit operation {0:?}")]
    UnknownOperation(String),
    /// An account event lacked its status object.
    #[error("account event missing its status")]
    MissingAccount,
}

#[derive(Deserialize)]
struct LiveFrame {
    payload: LivePayload,
}

#[derive(Deserialize)]
struct LivePayload {
    #[serde(rename = "$type")]
    kind: String,
    did: String,
    seq: u64,
    time: String,
    operation: Option<String>,
    collection: Option<String>,
    rkey: Option<String>,
    record: Option<Box<RawValue>>,
    account: Option<AccountStatus>,
}

#[derive(Deserialize)]
struct SdkEvent {
    did: String,
    seq: u64,
    time: String,
    kind: String,
    commit: Option<SdkCommit>,
    account: Option<AccountStatus>,
}

#[derive(Deserialize)]
struct SdkCommit {
    operation: String,
    collection: String,
    rkey: String,
    record: Option<Box<RawValue>>,
}

impl Event {
    /// Parses one live WebSocket text frame.
    ///
    /// # Errors
    ///
    /// Returns [`ParseError`] when the frame is not a well-formed Jetstream v2
    /// message.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::{Event, EventKind};
    /// let frame = r#"{"$type":"message","payload":{
    ///     "$type":"network.bsky.jetstream.subscribeEvents#commit",
    ///     "did":"did:plc:example","seq":7,"time":"2026-10-17T00:30:30.5Z",
    ///     "operation":"delete","collection":"app.bsky.feed.like","rkey":"3abc"}}"#;
    /// let event = Event::from_live_frame(frame).unwrap();
    /// assert_eq!(event.seq, 7);
    /// let EventKind::Commit(commit) = event.kind else { panic!("expected a commit") };
    /// assert_eq!(commit.rkey, "3abc");
    /// ```
    pub fn from_live_frame(frame: &str) -> Result<Self, ParseError> {
        let LiveFrame { payload } = serde_json::from_str(frame)?;
        let LivePayload {
            kind,
            did,
            seq,
            time,
            operation,
            collection,
            rkey,
            record,
            account,
        } = payload;
        let witnessed_us = parse_time(time)?;
        let kind = match kind.rsplit('#').next().unwrap_or_default() {
            "commit" => EventKind::Commit(commit(operation, collection, rkey, record)?),
            "account" => EventKind::Account(account.ok_or(ParseError::MissingAccount)?),
            "identity" => EventKind::Identity,
            "sync" => EventKind::Sync,
            _ => EventKind::Unknown(kind),
        };
        Ok(Self {
            did,
            seq,
            witnessed_us,
            kind,
        })
    }

    /// Parses one JSON line written by the replay script from an SDK event.
    ///
    /// # Errors
    ///
    /// Returns [`ParseError`] when the line is not a well-formed SDK event.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::{Event, EventKind};
    /// let line = r#"{"did":"did:plc:example","seq":9,
    ///     "time":"2026-08-15T01:15:12.169822Z","kind":"account",
    ///     "account":{"active":false,"status":"deleted"}}"#;
    /// let event = Event::from_sdk_line(line).unwrap();
    /// let EventKind::Account(status) = event.kind else { panic!("expected an account event") };
    /// assert!(!status.active);
    /// ```
    pub fn from_sdk_line(line: &str) -> Result<Self, ParseError> {
        let SdkEvent {
            did,
            seq,
            time,
            kind,
            commit: sdk_commit,
            account,
        } = serde_json::from_str(line)?;
        let witnessed_us = parse_time(time)?;
        let kind = match kind.as_str() {
            "commit" => {
                let Some(SdkCommit {
                    operation,
                    collection,
                    rkey,
                    record,
                }) = sdk_commit
                else {
                    return Err(ParseError::MissingField("commit"));
                };
                EventKind::Commit(commit(
                    Some(operation),
                    Some(collection),
                    Some(rkey),
                    record,
                )?)
            }
            "account" => EventKind::Account(account.ok_or(ParseError::MissingAccount)?),
            "identity" => EventKind::Identity,
            "sync" => EventKind::Sync,
            _ => EventKind::Unknown(kind),
        };
        Ok(Self {
            did,
            seq,
            witnessed_us,
            kind,
        })
    }
}

fn commit(
    operation: Option<String>,
    collection: Option<String>,
    rkey: Option<String>,
    record: Option<Box<RawValue>>,
) -> Result<Commit, ParseError> {
    let operation = operation.ok_or(ParseError::MissingField("operation"))?;
    let operation = match operation.as_str() {
        "create" => Operation::Create,
        "update" => Operation::Update,
        "delete" => Operation::Delete,
        _ => return Err(ParseError::UnknownOperation(operation)),
    };
    let collection = collection.ok_or(ParseError::MissingField("collection"))?;
    let rkey = rkey.ok_or(ParseError::MissingField("rkey"))?;
    Ok(Commit {
        operation,
        collection: Collection::from_nsid(&collection),
        rkey,
        record,
    })
}

fn parse_time(time: String) -> Result<i64, ParseError> {
    match time.parse::<jiff::Timestamp>() {
        Ok(ts) => Ok(ts.as_microsecond()),
        Err(source) => Err(ParseError::Time { time, source }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_commit_frame_parses_flat_payload() {
        let frame = r#"{"$type":"message","payload":{
            "$type":"network.bsky.jetstream.subscribeEvents#commit",
            "cid":"bafyexample","collection":"app.bsky.feed.postgate",
            "did":"did:plc:aaa","operation":"create",
            "record":{"$type":"app.bsky.feed.postgate","post":"at://did:plc:aaa/app.bsky.feed.post/1"},
            "rev":"3mx","rkey":"1","seq":27068063100,
            "time":"2026-10-17T00:30:30.700000Z","witnessedAt":"2026-10-17T00:30:30.700000Z"}}"#;
        let Event {
            did,
            seq,
            witnessed_us,
            kind,
        } = Event::from_live_frame(frame).unwrap();
        assert_eq!(did, "did:plc:aaa");
        assert_eq!(seq, 27_068_063_100);
        assert_eq!(witnessed_us, 1_792_197_030_700_000);
        let EventKind::Commit(Commit {
            operation,
            collection,
            rkey,
            record,
        }) = kind
        else {
            panic!("expected a commit");
        };
        assert_eq!(operation, Operation::Create);
        assert_eq!(collection, Collection::Postgate);
        assert_eq!(rkey, "1");
        assert!(record.is_some());
    }

    #[test]
    fn live_account_frame_parses_status() {
        let frame = r#"{"$type":"message","payload":{
            "$type":"network.bsky.jetstream.subscribeEvents#account",
            "account":{"active":false,"did":"did:plc:bbb","seq":1,"status":"deactivated","time":"2026-10-17T00:30:39.517Z"},
            "did":"did:plc:bbb","seq":27068066165,"time":"2026-10-17T00:30:39.733028Z"}}"#;
        let event = Event::from_live_frame(frame).unwrap();
        let EventKind::Account(AccountStatus { active, status }) = event.kind else {
            panic!("expected an account event");
        };
        assert!(!active);
        assert_eq!(status.as_deref(), Some("deactivated"));
    }

    #[test]
    fn identity_and_unknown_frames_are_recognized() {
        let identity = r#"{"$type":"message","payload":{
            "$type":"network.bsky.jetstream.subscribeEvents#identity",
            "did":"did:plc:ccc","seq":5,"time":"2026-10-17T00:30:39.658306Z"}}"#;
        let EventKind::Identity = Event::from_live_frame(identity).unwrap().kind else {
            panic!("expected an identity event");
        };
        let future = r#"{"$type":"message","payload":{
            "$type":"network.bsky.jetstream.subscribeEvents#novel",
            "did":"did:plc:ccc","seq":6,"time":"2026-10-17T00:30:39Z"}}"#;
        let EventKind::Unknown(kind) = Event::from_live_frame(future).unwrap().kind else {
            panic!("expected an unknown event");
        };
        assert_eq!(kind, "network.bsky.jetstream.subscribeEvents#novel");
    }

    #[test]
    fn sdk_commit_line_parses_nested_commit() {
        let line = r#"{"did":"did:plc:ddd","seq":24728124799,"time":"2026-08-15T01:15:12.170911Z",
            "kind":"commit","commit":{"operation":"delete","collection":"app.bsky.feed.repost",
            "rkey":"3mjs","rev":"3mt"}}"#;
        let event = Event::from_sdk_line(line).unwrap();
        let EventKind::Commit(Commit {
            operation,
            collection,
            record,
            ..
        }) = event.kind
        else {
            panic!("expected a commit");
        };
        assert_eq!(operation, Operation::Delete);
        assert_eq!(collection, Collection::Repost);
        assert!(record.is_none());
    }

    #[test]
    fn malformed_inputs_are_errors_not_panics() {
        let Err(ParseError::Json(_)) = Event::from_live_frame("not json") else {
            panic!("expected a JSON error");
        };
        let bad_time = r#"{"$type":"message","payload":{
            "$type":"network.bsky.jetstream.subscribeEvents#identity",
            "did":"did:plc:x","seq":1,"time":"yesterday"}}"#;
        let Err(ParseError::Time { time, .. }) = Event::from_live_frame(bad_time) else {
            panic!("expected a time error");
        };
        assert_eq!(time, "yesterday");
        let bad_op = r#"{"did":"did:plc:x","seq":1,"time":"2026-08-15T01:15:12Z","kind":"commit",
            "commit":{"operation":"upsert","collection":"app.bsky.feed.like","rkey":"1"}}"#;
        let Err(ParseError::UnknownOperation(op)) = Event::from_sdk_line(bad_op) else {
            panic!("expected an unknown-operation error");
        };
        assert_eq!(op, "upsert");
        let no_commit =
            r#"{"did":"did:plc:x","seq":1,"time":"2026-08-15T01:15:12Z","kind":"commit"}"#;
        let Err(ParseError::MissingField(field)) = Event::from_sdk_line(no_commit) else {
            panic!("expected a missing-field error");
        };
        assert_eq!(field, "commit");
    }
}

//! Compact rows, one table per record type.
//!
//! [`map_event`] turns an [`Event`] into at most one [`Row`]. Rows share the
//! event's `seq` (the dedup key: delivery is at-least-once) and `ts` (witness
//! time, used only for partitioning; analysis uses `created_at`).

use serde::Serialize;

use crate::event::{Collection, Commit, Event, EventKind, Operation};
use crate::records::{
    Block, Embed, Facet, FacetFeature, ListBlock, ListItem, Post, PostMinimal, Postgate,
    RecordEmbed, ReplyRef, StrongRef, SubjectRecord, Threadgate, TypedRule,
};

/// The table a [`Row`] belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Table {
    /// Post edges (reply, quote, mentions); no text.
    Posts,
    /// Likes.
    Likes,
    /// Reposts.
    Reposts,
    /// Account blocks.
    Blocks,
    /// Quote controls, including detached quotes.
    Postgates,
    /// Reply controls, including hidden replies.
    Threadgates,
    /// List memberships.
    ListItems,
    /// Moderation-list subscriptions used as blocks.
    ListBlocks,
    /// Deletions of records in the recorded collections.
    Deletes,
    /// Account status changes (deactivation, deletion, takedown).
    Accounts,
    /// Events that could not be mapped, without their content.
    Rejects,
}

impl Table {
    /// Every table, in a stable order.
    pub const ALL: [Table; 11] = [
        Table::Posts,
        Table::Likes,
        Table::Reposts,
        Table::Blocks,
        Table::Postgates,
        Table::Threadgates,
        Table::ListItems,
        Table::ListBlocks,
        Table::Deletes,
        Table::Accounts,
        Table::Rejects,
    ];

    /// Returns the table's directory name.
    ///
    /// # Examples
    ///
    /// ```
    /// assert_eq!(pileon_core::Table::ListBlocks.name(), "listblocks");
    /// ```
    pub fn name(self) -> &'static str {
        match self {
            Self::Posts => "posts",
            Self::Likes => "likes",
            Self::Reposts => "reposts",
            Self::Blocks => "blocks",
            Self::Postgates => "postgates",
            Self::Threadgates => "threadgates",
            Self::ListItems => "listitems",
            Self::ListBlocks => "listblocks",
            Self::Deletes => "deletes",
            Self::Accounts => "accounts",
            Self::Rejects => "rejects",
        }
    }
}

/// A row in one of the recorder's tables.
#[derive(Debug, Serialize, PartialEq)]
#[serde(untagged)]
pub enum Row {
    /// See [`Table::Posts`].
    Post(PostRow),
    /// See [`Table::Likes`].
    Like(SubjectRow),
    /// See [`Table::Reposts`].
    Repost(SubjectRow),
    /// See [`Table::Blocks`].
    Block(BlockRow),
    /// See [`Table::Postgates`].
    Postgate(PostgateRow),
    /// See [`Table::Threadgates`].
    Threadgate(ThreadgateRow),
    /// See [`Table::ListItems`].
    ListItem(ListItemRow),
    /// See [`Table::ListBlocks`].
    ListBlock(ListBlockRow),
    /// See [`Table::Deletes`].
    Delete(DeleteRow),
    /// See [`Table::Accounts`].
    Account(AccountRow),
    /// See [`Table::Rejects`].
    Reject(RejectRow),
}

impl Row {
    /// Returns the table this row belongs to.
    pub fn table(&self) -> Table {
        match self {
            Self::Post(_) => Table::Posts,
            Self::Like(_) => Table::Likes,
            Self::Repost(_) => Table::Reposts,
            Self::Block(_) => Table::Blocks,
            Self::Postgate(_) => Table::Postgates,
            Self::Threadgate(_) => Table::Threadgates,
            Self::ListItem(_) => Table::ListItems,
            Self::ListBlock(_) => Table::ListBlocks,
            Self::Delete(_) => Table::Deletes,
            Self::Account(_) => Table::Accounts,
            Self::Reject(_) => Table::Rejects,
        }
    }

    /// Returns the shared header every row carries.
    pub fn header(&self) -> &Header {
        match self {
            Self::Post(PostRow { header, .. })
            | Self::Like(SubjectRow { header, .. })
            | Self::Repost(SubjectRow { header, .. })
            | Self::Block(BlockRow { header, .. })
            | Self::Postgate(PostgateRow { header, .. })
            | Self::Threadgate(ThreadgateRow { header, .. })
            | Self::ListItem(ListItemRow { header, .. })
            | Self::ListBlock(ListBlockRow { header, .. })
            | Self::Delete(DeleteRow { header, .. })
            | Self::Account(AccountRow { header, .. })
            | Self::Reject(RejectRow { header, .. }) => header,
        }
    }
}

/// Fields every row carries.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Header {
    /// The event's sequence number on its Jetstream host.
    pub seq: u64,
    /// Witness time in microseconds since the Unix epoch.
    pub ts: i64,
    /// The repository the record lives in.
    pub did: String,
}

/// Whether a record row came from a create or an update.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RecordOp {
    /// A new record.
    Create,
    /// A replaced record.
    Update,
}

/// A post edge.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct PostRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// Create or update.
    pub op: RecordOp,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The post replied to.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent: Option<String>,
    /// The thread root.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub root: Option<String>,
    /// The quoted record.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quote: Option<String>,
    /// Mentioned accounts, deduplicated.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub mentions: Vec<String>,
    /// Declared languages.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub langs: Vec<String>,
}

/// A like or repost.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct SubjectRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The liked or reposted record.
    pub subject: String,
    /// The repost it was reached through, when given.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub via: Option<String>,
}

/// An account block.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct BlockRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The blocked account.
    pub subject: String,
}

/// A postgate: who may quote a post, and which quotes were detached.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct PostgateRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// Create or update.
    pub op: RecordOp,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The gated post.
    pub post: String,
    /// Detached quote posts.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub detached: Vec<String>,
    /// Whether new quotes are disabled.
    pub quotes_disabled: bool,
}

/// A threadgate: who may reply, and which replies are hidden.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ThreadgateRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// Create or update.
    pub op: RecordOp,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The gated thread root.
    pub post: String,
    /// Allowed repliers by rule name (`followingRule`, `listRule`, ...);
    /// `None` means anyone may reply, an empty list means no one may.
    pub allow: Option<Vec<String>>,
    /// Lists named by `listRule` entries.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub allow_lists: Vec<String>,
    /// Hidden replies.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub hidden: Vec<String>,
}

/// A list membership.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ListItemRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The list.
    pub list: String,
    /// The listed account.
    pub subject: String,
}

/// A moderation-list subscription used as a block.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ListBlockRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record key.
    pub rkey: String,
    /// The client-reported creation time.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    /// The subscribed list.
    pub subject: String,
}

/// A deletion in a recorded collection.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct DeleteRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The collection the deleted record lived in.
    pub collection: String,
    /// The deleted record's key.
    pub rkey: String,
}

/// An account status change.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct AccountRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// Whether the account is active.
    pub active: bool,
    /// Why it is inactive, when it is.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
}

/// An event that could not be mapped. Carries no record content: serde's
/// messages can quote the offending value, so only the error class and
/// position are kept.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct RejectRow {
    /// Shared fields.
    #[serde(flatten)]
    pub header: Header,
    /// The record's collection.
    pub collection: String,
    /// The record key.
    pub rkey: String,
    /// What went wrong, without content.
    pub error: String,
}

/// Maps an event to the row the recorder stores for it.
///
/// Returns `None` for events outside the recorded collections and for
/// identity, sync, and unknown events. A record that fails to parse becomes a
/// [`Row::Reject`] instead of being dropped silently.
///
/// # Examples
///
/// ```
/// use pileon_core::{Event, Row, map_event};
/// let frame = r#"{"$type":"message","payload":{
///     "$type":"network.bsky.jetstream.subscribeEvents#commit",
///     "did":"did:plc:liker","seq":42,"time":"2026-10-17T00:00:00Z",
///     "operation":"create","collection":"app.bsky.feed.like","rkey":"3abc",
///     "record":{"$type":"app.bsky.feed.like","createdAt":"2026-10-17T00:00:00Z",
///               "subject":{"uri":"at://did:plc:op/app.bsky.feed.post/1","cid":"bafy"}}}}"#;
/// let event = Event::from_live_frame(frame).unwrap();
/// let Some(Row::Like(like)) = map_event(&event) else { panic!("expected a like") };
/// assert_eq!(like.subject, "at://did:plc:op/app.bsky.feed.post/1");
/// ```
pub fn map_event(event: &Event) -> Option<Row> {
    let Event {
        did,
        seq,
        witnessed_us,
        kind,
    } = event;
    let header = Header {
        seq: *seq,
        ts: *witnessed_us,
        did: did.clone(),
    };
    match kind {
        EventKind::Commit(commit) => map_commit(header, commit),
        EventKind::Account(status) => Some(Row::Account(AccountRow {
            header,
            active: status.active,
            status: status.status.clone(),
        })),
        EventKind::Identity | EventKind::Sync | EventKind::Unknown(_) => None,
    }
}

enum Failure {
    MissingRecord,
    Json(serde_json::Error),
}

impl Failure {
    fn describe(&self) -> String {
        match self {
            Self::MissingRecord => "missing record".to_owned(),
            Self::Json(error) => format!(
                "{:?} error at line {} column {}",
                error.classify(),
                error.line(),
                error.column()
            ),
        }
    }
}

fn map_commit(header: Header, commit: &Commit) -> Option<Row> {
    let Commit {
        operation,
        collection,
        rkey,
        record,
    } = commit;
    if !Collection::RECORDED.contains(collection) {
        return None;
    }
    let op = match operation {
        Operation::Delete => {
            return Some(Row::Delete(DeleteRow {
                header,
                collection: collection.nsid().to_owned(),
                rkey: rkey.clone(),
            }));
        }
        Operation::Create => RecordOp::Create,
        Operation::Update => RecordOp::Update,
    };
    let Some(record) = record else {
        return Some(reject(header, collection, rkey, &Failure::MissingRecord));
    };
    let Header { seq, ts, did } = &header;
    let copy = Header {
        seq: *seq,
        ts: *ts,
        did: did.clone(),
    };
    match map_record(collection, record.get(), op, rkey.clone(), copy) {
        Ok(row) => Some(row),
        Err(failure) => Some(reject(header, collection, rkey, &failure)),
    }
}

fn reject(header: Header, collection: &Collection, rkey: &str, failure: &Failure) -> Row {
    Row::Reject(RejectRow {
        header,
        collection: collection.nsid().to_owned(),
        rkey: rkey.to_owned(),
        error: failure.describe(),
    })
}

fn map_record(
    collection: &Collection,
    json: &str,
    op: RecordOp,
    rkey: String,
    header: Header,
) -> Result<Row, Failure> {
    let row = match collection {
        Collection::Post => Row::Post(post_row(json, op, rkey, header)?),
        Collection::Like | Collection::Repost => {
            let SubjectRecord {
                created_at,
                subject: StrongRef { uri: subject },
                via,
            } = parse(json)?;
            let row = SubjectRow {
                header,
                rkey,
                created_at,
                subject,
                via: via.map(|StrongRef { uri }| uri),
            };
            if *collection == Collection::Like {
                Row::Like(row)
            } else {
                Row::Repost(row)
            }
        }
        Collection::Block => {
            let Block {
                created_at,
                subject,
            } = parse(json)?;
            Row::Block(BlockRow {
                header,
                rkey,
                created_at,
                subject,
            })
        }
        Collection::Postgate => Row::Postgate(postgate_row(parse(json)?, op, rkey, header)),
        Collection::Threadgate => Row::Threadgate(threadgate_row(parse(json)?, op, rkey, header)),
        Collection::ListItem => {
            let ListItem {
                created_at,
                list,
                subject,
            } = parse(json)?;
            Row::ListItem(ListItemRow {
                header,
                rkey,
                created_at,
                list,
                subject,
            })
        }
        Collection::ListBlock => {
            let ListBlock {
                created_at,
                subject,
            } = parse(json)?;
            Row::ListBlock(ListBlockRow {
                header,
                rkey,
                created_at,
                subject,
            })
        }
        Collection::Other(_) => return Err(Failure::MissingRecord),
    };
    Ok(row)
}

fn parse<'a, T: serde::Deserialize<'a>>(json: &'a str) -> Result<T, Failure> {
    serde_json::from_str(json).map_err(Failure::Json)
}

fn postgate_row(record: Postgate, op: RecordOp, rkey: String, header: Header) -> PostgateRow {
    let Postgate {
        created_at,
        post,
        detached_embedding_uris,
        embedding_rules,
    } = record;
    let quotes_disabled =
        embedding_rules
            .unwrap_or_default()
            .iter()
            .any(|TypedRule { kind, .. }| {
                kind.as_deref()
                    .is_some_and(|kind| kind.ends_with("#disableRule"))
            });
    PostgateRow {
        header,
        rkey,
        op,
        created_at,
        post,
        detached: detached_embedding_uris.unwrap_or_default(),
        quotes_disabled,
    }
}

fn threadgate_row(record: Threadgate, op: RecordOp, rkey: String, header: Header) -> ThreadgateRow {
    let Threadgate {
        created_at,
        post,
        allow,
        hidden_replies,
    } = record;
    let allow_lists = allow
        .iter()
        .flatten()
        .filter_map(|TypedRule { list, .. }| list.clone())
        .collect();
    let allow = allow.map(|rules| {
        rules
            .into_iter()
            .map(|TypedRule { kind, .. }| {
                let kind = kind.unwrap_or_default();
                kind.rsplit('#').next().unwrap_or_default().to_owned()
            })
            .collect()
    });
    ThreadgateRow {
        header,
        rkey,
        op,
        created_at,
        post,
        allow,
        allow_lists,
        hidden: hidden_replies.unwrap_or_default(),
    }
}

fn post_row(json: &str, op: RecordOp, rkey: String, header: Header) -> Result<PostRow, Failure> {
    let Ok(Post {
        created_at,
        reply,
        embed,
        facets,
        langs,
    }) = serde_json::from_str::<Post>(json)
    else {
        let PostMinimal { created_at, reply } = parse(json)?;
        let (parent, root) = reply_uris(reply);
        return Ok(PostRow {
            header,
            rkey,
            op,
            created_at,
            parent,
            root,
            quote: None,
            mentions: Vec::new(),
            langs: Vec::new(),
        });
    };
    let (parent, root) = reply_uris(reply);
    Ok(PostRow {
        header,
        rkey,
        op,
        created_at,
        parent,
        root,
        quote: embed.and_then(quoted_uri),
        mentions: mentions(facets.unwrap_or_default()),
        langs: langs.unwrap_or_default(),
    })
}

fn reply_uris(reply: Option<ReplyRef>) -> (Option<String>, Option<String>) {
    let Some(ReplyRef {
        parent: StrongRef { uri: parent },
        root: StrongRef { uri: root },
    }) = reply
    else {
        return (None, None);
    };
    (Some(parent), Some(root))
}

fn quoted_uri(embed: Embed) -> Option<String> {
    match embed {
        Embed::Record {
            record: StrongRef { uri },
        }
        | Embed::RecordWithMedia {
            record: RecordEmbed {
                record: StrongRef { uri },
            },
        } => Some(uri),
        Embed::Other => None,
    }
}

fn mentions(facets: Vec<Facet>) -> Vec<String> {
    let mut dids: Vec<String> = facets
        .into_iter()
        .flat_map(|Facet { features }| features)
        .filter_map(|FacetFeature { kind, did }| {
            let is_mention = kind.as_deref() == Some("app.bsky.richtext.facet#mention");
            if is_mention { did } else { None }
        })
        .collect();
    dids.sort_unstable();
    dids.dedup();
    dids
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commit_frame(collection: &str, operation: &str, record: Option<&str>) -> String {
        let record = record.map_or(String::new(), |r| format!(r#","record":{r}"#));
        format!(
            r#"{{"$type":"message","payload":{{
            "$type":"network.bsky.jetstream.subscribeEvents#commit",
            "did":"did:plc:author","seq":100,"time":"2026-10-17T00:00:00.000001Z",
            "operation":"{operation}","collection":"{collection}","rkey":"3rk"{record}}}}}"#
        )
    }

    fn map(collection: &str, operation: &str, record: Option<&str>) -> Option<Row> {
        let frame = commit_frame(collection, operation, record);
        map_event(&Event::from_live_frame(&frame).unwrap())
    }

    #[test]
    fn reply_quote_post_keeps_edges_and_drops_text() {
        let record = r#"{"$type":"app.bsky.feed.post","text":"secret words",
            "createdAt":"2026-10-17T00:00:00Z","langs":["en"],
            "reply":{"parent":{"uri":"at://did:plc:p/app.bsky.feed.post/2","cid":"c"},
                     "root":{"uri":"at://did:plc:r/app.bsky.feed.post/1","cid":"c"}},
            "embed":{"$type":"app.bsky.embed.recordWithMedia",
                     "media":{"$type":"app.bsky.embed.images","images":[]},
                     "record":{"$type":"app.bsky.embed.record",
                               "record":{"uri":"at://did:plc:q/app.bsky.feed.post/9","cid":"c"}}},
            "facets":[{"index":{"byteStart":0,"byteEnd":4},"features":[
                {"$type":"app.bsky.richtext.facet#mention","did":"did:plc:m2"},
                {"$type":"app.bsky.richtext.facet#link","uri":"https://example.com"},
                {"$type":"app.bsky.richtext.facet#mention","did":"did:plc:m1"},
                {"$type":"app.bsky.richtext.facet#mention","did":"did:plc:m2"}]}]}"#;
        let Some(row) = map("app.bsky.feed.post", "create", Some(record)) else {
            panic!("expected a row");
        };
        let json = serde_json::to_string(&row).unwrap();
        assert!(!json.contains("secret"), "post text leaked: {json}");
        let Row::Post(PostRow {
            header,
            parent,
            root,
            quote,
            mentions,
            langs,
            op,
            ..
        }) = row
        else {
            panic!("expected a post row");
        };
        assert_eq!(header.seq, 100);
        assert_eq!(header.did, "did:plc:author");
        assert_eq!(op, RecordOp::Create);
        assert_eq!(
            parent.as_deref(),
            Some("at://did:plc:p/app.bsky.feed.post/2")
        );
        assert_eq!(root.as_deref(), Some("at://did:plc:r/app.bsky.feed.post/1"));
        assert_eq!(
            quote.as_deref(),
            Some("at://did:plc:q/app.bsky.feed.post/9")
        );
        assert_eq!(mentions, vec!["did:plc:m1", "did:plc:m2"]);
        assert_eq!(langs, vec!["en"]);
    }

    #[test]
    fn malformed_embed_still_yields_the_reply_edge() {
        let record = r#"{"text":"x","createdAt":"2026-10-17T00:00:00Z",
            "reply":{"parent":{"uri":"at://p","cid":"c"},"root":{"uri":"at://r","cid":"c"}},
            "embed":{"record":{"uri":"at://q"}}}"#;
        let Some(Row::Post(PostRow { parent, quote, .. })) =
            map("app.bsky.feed.post", "create", Some(record))
        else {
            panic!("expected a post row");
        };
        assert_eq!(parent.as_deref(), Some("at://p"));
        assert_eq!(quote, None);
    }

    #[test]
    fn unparseable_record_becomes_a_reject_without_content() {
        let record = r#"{"subject":"a string where a strongRef belongs","createdAt":"x"}"#;
        let Some(Row::Reject(RejectRow {
            collection, error, ..
        })) = map("app.bsky.feed.like", "create", Some(record))
        else {
            panic!("expected a reject");
        };
        assert_eq!(collection, "app.bsky.feed.like");
        assert!(
            !error.contains("strongRef belongs"),
            "content leaked: {error}"
        );
    }

    #[test]
    fn deletes_are_recorded_only_for_recorded_collections() {
        let Some(Row::Delete(DeleteRow {
            collection, rkey, ..
        })) = map("app.bsky.feed.post", "delete", None)
        else {
            panic!("expected a delete");
        };
        assert_eq!(collection, "app.bsky.feed.post");
        assert_eq!(rkey, "3rk");
        assert_eq!(map("app.bsky.graph.follow", "delete", None), None);
        assert_eq!(
            map(
                "app.bsky.actor.profile",
                "create",
                Some(r#"{"displayName":"x"}"#)
            ),
            None
        );
    }

    #[test]
    fn like_and_repost_keep_subject_and_via() {
        let record = r#"{"$type":"app.bsky.feed.repost","createdAt":"2026-10-17T00:00:00Z",
            "subject":{"uri":"at://s","cid":"c"},"via":{"uri":"at://v","cid":"c"}}"#;
        let Some(Row::Repost(SubjectRow { subject, via, .. })) =
            map("app.bsky.feed.repost", "create", Some(record))
        else {
            panic!("expected a repost");
        };
        assert_eq!(subject, "at://s");
        assert_eq!(via.as_deref(), Some("at://v"));
    }

    #[test]
    fn postgate_detach_and_disable_are_captured() {
        let record = r#"{"$type":"app.bsky.feed.postgate","createdAt":"2026-10-17T00:00:00Z",
            "post":"at://did:plc:a/app.bsky.feed.post/1",
            "detachedEmbeddingUris":["at://did:plc:dunker/app.bsky.feed.post/7"],
            "embeddingRules":[{"$type":"app.bsky.feed.postgate#disableRule"}]}"#;
        let Some(Row::Postgate(PostgateRow {
            detached,
            quotes_disabled,
            op,
            ..
        })) = map("app.bsky.feed.postgate", "update", Some(record))
        else {
            panic!("expected a postgate");
        };
        assert_eq!(detached, vec!["at://did:plc:dunker/app.bsky.feed.post/7"]);
        assert!(quotes_disabled);
        assert_eq!(op, RecordOp::Update);
    }

    #[test]
    fn threadgate_distinguishes_anyone_from_no_one() {
        let anyone = r#"{"post":"at://p","createdAt":"x","hiddenReplies":["at://h"]}"#;
        let Some(Row::Threadgate(ThreadgateRow { allow, hidden, .. })) =
            map("app.bsky.feed.threadgate", "create", Some(anyone))
        else {
            panic!("expected a threadgate");
        };
        assert_eq!(allow, None);
        assert_eq!(hidden, vec!["at://h"]);

        let rules = r#"{"post":"at://p","createdAt":"x","allow":[
            {"$type":"app.bsky.feed.threadgate#followerRule"},
            {"$type":"app.bsky.feed.threadgate#listRule","list":"at://l"}]}"#;
        let Some(Row::Threadgate(ThreadgateRow {
            allow, allow_lists, ..
        })) = map("app.bsky.feed.threadgate", "create", Some(rules))
        else {
            panic!("expected a threadgate");
        };
        assert_eq!(
            allow,
            Some(vec!["followerRule".to_owned(), "listRule".to_owned()])
        );
        assert_eq!(allow_lists, vec!["at://l"]);

        let nobody = r#"{"post":"at://p","createdAt":"x","allow":[]}"#;
        let Some(Row::Threadgate(ThreadgateRow { allow, .. })) =
            map("app.bsky.feed.threadgate", "create", Some(nobody))
        else {
            panic!("expected a threadgate");
        };
        assert_eq!(allow, Some(Vec::new()));
    }

    #[test]
    fn block_list_item_and_list_block_rows() {
        let Some(Row::Block(BlockRow { subject, .. })) = map(
            "app.bsky.graph.block",
            "create",
            Some(r#"{"subject":"did:plc:b","createdAt":"x"}"#),
        ) else {
            panic!("expected a block");
        };
        assert_eq!(subject, "did:plc:b");
        let Some(Row::ListItem(ListItemRow { list, subject, .. })) = map(
            "app.bsky.graph.listitem",
            "create",
            Some(r#"{"list":"at://l","subject":"did:plc:s","createdAt":"x"}"#),
        ) else {
            panic!("expected a list item");
        };
        assert_eq!((list.as_str(), subject.as_str()), ("at://l", "did:plc:s"));
        let Some(Row::ListBlock(ListBlockRow { subject, .. })) = map(
            "app.bsky.graph.listblock",
            "create",
            Some(r#"{"subject":"at://modlist","createdAt":"x"}"#),
        ) else {
            panic!("expected a list block");
        };
        assert_eq!(subject, "at://modlist");
    }

    #[test]
    fn account_events_map_and_identity_events_do_not() {
        let account = r#"{"did":"did:plc:gone","seq":3,"time":"2026-08-15T01:15:12Z",
            "kind":"account","account":{"active":false,"status":"deleted"}}"#;
        let Some(Row::Account(AccountRow { active, status, .. })) =
            map_event(&Event::from_sdk_line(account).unwrap())
        else {
            panic!("expected an account row");
        };
        assert!(!active);
        assert_eq!(status.as_deref(), Some("deleted"));
        let identity =
            r#"{"did":"did:plc:x","seq":4,"time":"2026-08-15T01:15:12Z","kind":"identity"}"#;
        assert_eq!(map_event(&Event::from_sdk_line(identity).unwrap()), None);
    }

    #[test]
    fn rows_serialize_flat_and_compact() {
        let Some(row) = map(
            "app.bsky.feed.like",
            "create",
            Some(r#"{"subject":{"uri":"at://s","cid":"c"},"createdAt":"2026-10-17T00:00:00Z"}"#),
        ) else {
            panic!("expected a row");
        };
        assert_eq!(row.table(), Table::Likes);
        assert_eq!(row.header().ts, 1_792_195_200_000_001);
        assert_eq!(
            serde_json::to_string(&row).unwrap(),
            r#"{"seq":100,"ts":1792195200000001,"did":"did:plc:author","rkey":"3rk","created_at":"2026-10-17T00:00:00Z","subject":"at://s"}"#
        );
    }
}

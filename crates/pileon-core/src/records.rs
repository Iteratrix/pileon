//! The subset of each Bluesky lexicon record the recorder keeps.
//!
//! Records are user-generated and only loosely validated by the network, so
//! every field here is optional unless the row cannot exist without it, and
//! unknown fields are ignored. Post text is deliberately absent.

use serde::Deserialize;

/// A `com.atproto.repo.strongRef`, reduced to its URI.
#[derive(Debug, Deserialize)]
pub struct StrongRef {
    /// The referenced record's AT URI.
    pub uri: String,
}

/// An `app.bsky.feed.post` record, without its text.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Post {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The thread position, when the post is a reply.
    pub reply: Option<ReplyRef>,
    /// The embed, when the post has one.
    pub embed: Option<Embed>,
    /// Rich-text annotations; mentions are kept.
    pub facets: Option<Vec<Facet>>,
    /// The author-declared languages.
    pub langs: Option<Vec<String>>,
}

/// An `app.bsky.feed.post` record reduced to what a reply edge needs.
///
/// Used when the full [`Post`] fails to parse (usually a malformed embed or
/// facet), so a broken decoration never costs the reply edge itself.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PostMinimal {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The thread position, when the post is a reply.
    pub reply: Option<ReplyRef>,
}

/// A post's position in a thread.
#[derive(Debug, Deserialize)]
pub struct ReplyRef {
    /// The post being replied to.
    pub parent: StrongRef,
    /// The thread's root post.
    pub root: StrongRef,
}

/// The embed types that make a post a quote; everything else is ignored.
#[derive(Debug, Deserialize)]
#[serde(tag = "$type")]
pub enum Embed {
    /// `app.bsky.embed.record`: a bare quote.
    #[serde(rename = "app.bsky.embed.record")]
    Record {
        /// The quoted record.
        record: StrongRef,
    },
    /// `app.bsky.embed.recordWithMedia`: a quote with images or video.
    #[serde(rename = "app.bsky.embed.recordWithMedia")]
    RecordWithMedia {
        /// The wrapper holding the quoted record.
        record: RecordEmbed,
    },
    /// Images, video, external links, and anything newer.
    #[serde(other)]
    Other,
}

/// The `record` half of an `app.bsky.embed.recordWithMedia`.
#[derive(Debug, Deserialize)]
pub struct RecordEmbed {
    /// The quoted record.
    pub record: StrongRef,
}

/// An `app.bsky.richtext.facet`.
#[derive(Debug, Deserialize)]
pub struct Facet {
    /// The facet's features (mentions, links, tags).
    pub features: Vec<FacetFeature>,
}

/// One facet feature; only mentions carry data the recorder keeps.
#[derive(Debug, Deserialize)]
pub struct FacetFeature {
    /// The feature type, e.g. `app.bsky.richtext.facet#mention`.
    #[serde(rename = "$type")]
    pub kind: Option<String>,
    /// The mentioned account, for mention features.
    pub did: Option<String>,
}

/// An `app.bsky.feed.like` or `app.bsky.feed.repost` record.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubjectRecord {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The liked or reposted record.
    pub subject: StrongRef,
    /// The repost through which the subject was reached, when given.
    pub via: Option<StrongRef>,
}

/// An `app.bsky.graph.block` or `app.bsky.graph.follow` record.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AccountEdge {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The blocked or followed account.
    pub subject: String,
}

/// An `app.bsky.feed.postgate` record.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Postgate {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The gated post.
    pub post: String,
    /// Quote posts the author has detached from this post.
    pub detached_embedding_uris: Option<Vec<String>>,
    /// Embedding rules; a `#disableRule` forbids new quotes.
    pub embedding_rules: Option<Vec<TypedRule>>,
}

/// An `app.bsky.feed.threadgate` record.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Threadgate {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The gated thread's root post.
    pub post: String,
    /// Who may reply; absent means anyone, empty means no one.
    pub allow: Option<Vec<TypedRule>>,
    /// Replies the author has hidden.
    pub hidden_replies: Option<Vec<String>>,
}

/// A gate rule, identified by its `$type`.
#[derive(Debug, Deserialize)]
pub struct TypedRule {
    /// The rule type, e.g. `app.bsky.feed.threadgate#followerRule`.
    #[serde(rename = "$type")]
    pub kind: Option<String>,
    /// The list a `#listRule` refers to.
    pub list: Option<String>,
}

/// An `app.bsky.graph.listitem` record.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListItem {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The list the account was added to.
    pub list: String,
    /// The added account.
    pub subject: String,
}

/// An `app.bsky.graph.listblock` record (subscribing to a list as a block).
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListBlock {
    /// The client-reported creation time.
    pub created_at: Option<String>,
    /// The subscribed moderation list.
    pub subject: String,
}

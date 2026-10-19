//! Typed reads from the Bluesky `AppView` and PDSes, behind a [`Fetch`] trait.
//!
//! These mirror the lab's `sources.py` request for request: the same
//! endpoints, parameters, batch sizes and URL encoding. So a [`Fetch`] backed
//! by the lab's response cache serves exactly the responses the lab graded
//! on, and the live HTTP one asks the network the same questions.

use std::collections::{BTreeSet, HashMap};
use std::fmt::Write;

use serde_json::{Map, Value};

/// The public `AppView`.
pub const APPVIEW: &str = "https://public.api.bsky.app";
/// The PLC directory, which resolves `did:plc` identities.
pub const PLC: &str = "https://plc.directory";

const POSTS_BATCH: usize = 25;
const PROFILES_BATCH: usize = 25;
const RELATIONSHIPS_BATCH: usize = 30;
const LIST_PAGE: usize = 100;

/// One GET: `base + path`, with query parameters in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    /// Scheme and host, e.g. [`APPVIEW`].
    pub base: String,
    /// The path, starting with `/`.
    pub path: String,
    /// Query parameters, repeated keys allowed.
    pub params: Vec<(String, String)>,
}

impl Request {
    /// Creates an XRPC call of `method` on `base`.
    pub fn xrpc(base: &str, method: &str, params: Vec<(String, String)>) -> Self {
        Self {
            base: base.to_owned(),
            path: format!("/xrpc/{method}"),
            params,
        }
    }

    /// Returns the URL exactly as the lab's fetcher forms it (and keys its
    /// cache by): parameters encoded like Python's `urlencode`.
    ///
    /// # Examples
    ///
    /// ```
    /// use pileon_core::appview::Request;
    /// let request = Request::xrpc(
    ///     "https://h",
    ///     "m",
    ///     vec![("uri".into(), "at://did:plc:a/x y".into())],
    /// );
    /// assert_eq!(request.url(), "https://h/xrpc/m?uri=at%3A%2F%2Fdid%3Aplc%3Aa%2Fx+y");
    /// ```
    pub fn url(&self) -> String {
        let query = encode_query(&self.params);
        if query.is_empty() {
            format!("{}{}", self.base, self.path)
        } else {
            format!("{}{}?{query}", self.base, self.path)
        }
    }
}

fn encode_query(params: &[(String, String)]) -> String {
    let mut out = String::new();
    for (i, (key, value)) in params.iter().enumerate() {
        if i > 0 {
            out.push('&');
        }
        quote_plus(key, &mut out);
        out.push('=');
        quote_plus(value, &mut out);
    }
    out
}

fn quote_plus(text: &str, out: &mut String) {
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            b' ' => out.push('+'),
            _ => {
                let _ = write!(out, "%{byte:02X}");
            }
        }
    }
}

/// A definitive response: its status and, when it was a JSON object, its body.
#[derive(Debug, Clone, PartialEq)]
pub struct Reply {
    /// HTTP status; 0 for a cache miss that was never fetched.
    pub status: u16,
    /// The JSON body, when it was an object.
    pub body: Option<Map<String, Value>>,
}

impl Reply {
    /// Returns whether the request succeeded with a body.
    pub fn ok(&self) -> bool {
        self.status == 200 && self.body.is_some()
    }

    fn field(&self, name: &str) -> Option<&Value> {
        self.body.as_ref()?.get(name)
    }

    fn items(&self, name: &str) -> &[Value] {
        self.field(name)
            .and_then(Value::as_array)
            .map_or(&[], Vec::as_slice)
    }
}

/// A request that failed after every retry.
#[derive(Debug, thiserror::Error)]
#[error("GET {url} failed: {reason}")]
pub struct FetchError {
    /// The URL requested.
    pub url: String,
    /// What went wrong last.
    pub reason: String,
}

/// Where reads come from: the network, or a cache of earlier responses.
pub trait Fetch {
    /// Returns the definitive response to `request`. Transient failures are
    /// retried by the implementation; an error means it gave up.
    fn get(&self, request: &Request) -> impl Future<Output = Result<Reply, FetchError>>;

    /// Returns post views for up to 25 URIs; URIs that do not resolve are
    /// absent. The default asks `app.bsky.feed.getPosts`.
    fn get_posts(
        &self,
        uris: &[String],
    ) -> impl Future<Output = Result<Vec<Map<String, Value>>, FetchError>> {
        async move {
            let params = uris
                .iter()
                .map(|u| ("uris".to_owned(), u.clone()))
                .collect();
            let reply = self
                .get(&Request::xrpc(APPVIEW, "app.bsky.feed.getPosts", params))
                .await?;
            if !reply.ok() {
                return Ok(Vec::new());
            }
            Ok(reply
                .items("posts")
                .iter()
                .filter_map(|p| p.as_object().cloned())
                .collect())
        }
    }
}

/// A post's author, kind and engagement counts as the `AppView` reports them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostCounts {
    /// The post's AT URI.
    pub uri: String,
    /// Its author's DID.
    pub author: String,
    /// Whether the post is a reply.
    pub is_reply: bool,
    /// Replies to it.
    pub replies: u64,
    /// Likes on it.
    pub likes: u64,
    /// Quotes of it.
    pub quotes: u64,
    /// Reposts of it.
    pub reposts: u64,
}

fn count(post: &Map<String, Value>, name: &str) -> u64 {
    post.get(name).and_then(Value::as_u64).unwrap_or(0)
}

fn sorted_unique(items: &[String]) -> Vec<String> {
    items
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// Returns current counts for each URI that still resolves.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn post_counts<F: Fetch>(
    fetch: &F,
    uris: &[String],
) -> Result<HashMap<String, PostCounts>, FetchError> {
    let mut out = HashMap::new();
    for batch in sorted_unique(uris).chunks(POSTS_BATCH) {
        for post in fetch.get_posts(batch).await? {
            let Some(uri) = post.get("uri").and_then(Value::as_str) else {
                continue;
            };
            let record = post.get("record").and_then(Value::as_object);
            let counts = PostCounts {
                uri: uri.to_owned(),
                author: post
                    .get("author")
                    .and_then(|a| a.get("did"))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                is_reply: record.is_some_and(|r| r.contains_key("reply")),
                replies: count(&post, "replyCount"),
                likes: count(&post, "likeCount"),
                quotes: count(&post, "quoteCount"),
                reposts: count(&post, "repostCount"),
            };
            out.insert(counts.uri.clone(), counts);
        }
    }
    Ok(out)
}

/// Who liked a post and when (client-reported `createdAt`, unparsed).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Likers {
    /// False when the post had more likes than the page cap, or a page
    /// failed, so absence from `liked_at` means nothing.
    pub complete: bool,
    /// Liker DID to the like's `createdAt`, first seen wins.
    pub liked_at: HashMap<String, String>,
}

/// Returns the accounts that like `uri`, up to `max_pages` pages of 100.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn likers<F: Fetch>(
    fetch: &F,
    uri: &str,
    max_pages: usize,
) -> Result<Likers, FetchError> {
    let mut liked_at = HashMap::new();
    let mut cursor: Option<String> = None;
    for _ in 0..max_pages {
        let mut params = vec![
            ("uri".to_owned(), uri.to_owned()),
            ("limit".to_owned(), "100".to_owned()),
        ];
        if let Some(c) = &cursor {
            params.push(("cursor".to_owned(), c.clone()));
        }
        let reply = fetch
            .get(&Request::xrpc(APPVIEW, "app.bsky.feed.getLikes", params))
            .await?;
        if !reply.ok() {
            return Ok(Likers {
                complete: false,
                liked_at,
            });
        }
        let page = reply.items("likes");
        for like in page {
            let did = like
                .get("actor")
                .and_then(|a| a.get("did"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            if !did.is_empty() {
                let at = like
                    .get("createdAt")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                liked_at
                    .entry(did.to_owned())
                    .or_insert_with(|| at.to_owned());
            }
        }
        cursor = reply
            .field("cursor")
            .and_then(Value::as_str)
            .filter(|c| !c.is_empty())
            .map(str::to_owned);
        if page.is_empty() || cursor.is_none() {
            return Ok(Likers {
                complete: true,
                liked_at,
            });
        }
    }
    Ok(Likers {
        complete: false,
        liked_at,
    })
}

/// An actor's relationship with one other account, with follow record URIs.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Relationship {
    /// Whether the `AppView` answered for this account.
    pub resolved: bool,
    /// The actor's follow of the other account.
    pub actor_follows: Option<String>,
    /// The other account's follow of the actor.
    pub follows_actor: Option<String>,
}

/// Returns `actor`'s relationship with each of `others` (never itself);
/// accounts the `AppView` did not answer for are unresolved.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn relationships<F: Fetch>(
    fetch: &F,
    actor: &str,
    others: &[String],
) -> Result<HashMap<String, Relationship>, FetchError> {
    let unique: Vec<String> = sorted_unique(others)
        .into_iter()
        .filter(|o| o != actor)
        .collect();
    let mut out: HashMap<String, Relationship> = unique
        .iter()
        .map(|o| (o.clone(), Relationship::default()))
        .collect();
    for batch in unique.chunks(RELATIONSHIPS_BATCH) {
        let mut params = vec![("actor".to_owned(), actor.to_owned())];
        params.extend(batch.iter().map(|o| ("others".to_owned(), o.clone())));
        let reply = fetch
            .get(&Request::xrpc(
                APPVIEW,
                "app.bsky.graph.getRelationships",
                params,
            ))
            .await?;
        for rel in reply.items("relationships") {
            let kind = rel.get("$type").and_then(Value::as_str).unwrap_or_default();
            let Some(did) = rel.get("did").and_then(Value::as_str) else {
                continue;
            };
            if !kind.ends_with("#relationship") {
                continue;
            }
            let uri = |name: &str| rel.get(name).and_then(Value::as_str).map(str::to_owned);
            out.insert(
                did.to_owned(),
                Relationship {
                    resolved: true,
                    actor_follows: uri("following"),
                    follows_actor: uri("followedBy"),
                },
            );
        }
    }
    Ok(out)
}

/// Returns current follower counts for each DID that still resolves.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn follower_counts<F: Fetch>(
    fetch: &F,
    dids: &[String],
) -> Result<HashMap<String, u64>, FetchError> {
    let mut out = HashMap::new();
    for batch in sorted_unique(dids).chunks(PROFILES_BATCH) {
        let params = batch
            .iter()
            .map(|d| ("actors".to_owned(), d.clone()))
            .collect();
        let reply = fetch
            .get(&Request::xrpc(
                APPVIEW,
                "app.bsky.actor.getProfiles",
                params,
            ))
            .await?;
        for profile in reply.items("profiles") {
            if let Some(did) = profile.get("did").and_then(Value::as_str) {
                out.insert(did.to_owned(), count_of(profile, "followersCount"));
            }
        }
    }
    Ok(out)
}

fn count_of(value: &Value, name: &str) -> u64 {
    value.get(name).and_then(Value::as_u64).unwrap_or(0)
}

/// Returns the PDS endpoint for `did`, or `None` if it does not resolve.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn resolve_pds<F: Fetch>(fetch: &F, did: &str) -> Result<Option<String>, FetchError> {
    let request = if did.starts_with("did:plc:") {
        Request {
            base: PLC.to_owned(),
            path: format!("/{did}"),
            params: Vec::new(),
        }
    } else if let Some(domain) = did.strip_prefix("did:web:") {
        Request {
            base: format!("https://{domain}"),
            path: "/.well-known/did.json".to_owned(),
            params: Vec::new(),
        }
    } else {
        return Ok(None);
    };
    let reply = fetch.get(&request).await?;
    if !reply.ok() {
        return Ok(None);
    }
    for service in reply.items("service") {
        let id = service
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if id.ends_with("#atproto_pds") {
            return Ok(service
                .get("serviceEndpoint")
                .and_then(Value::as_str)
                .map(str::to_owned));
        }
    }
    Ok(None)
}

/// Returns up to `limit` of `did`'s post URIs created before `before_us`,
/// newest first, using the repo's TID order as the cursor. `None` when the
/// PDS does not resolve or the first page fails.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn post_uris_before<F: Fetch>(
    fetch: &F,
    did: &str,
    before_us: i64,
    limit: usize,
) -> Result<Option<Vec<String>>, FetchError> {
    let Some(pds) = resolve_pds(fetch, did).await? else {
        return Ok(None);
    };
    let Ok(mut cursor) = crate::tid::encode_us(before_us) else {
        return Ok(None);
    };
    let base = pds.trim_end_matches('/');
    let mut uris: Vec<String> = Vec::new();
    while uris.len() < limit {
        let params = vec![
            ("repo".to_owned(), did.to_owned()),
            ("collection".to_owned(), "app.bsky.feed.post".to_owned()),
            ("limit".to_owned(), LIST_PAGE.to_string()),
            ("cursor".to_owned(), cursor.clone()),
        ];
        let reply = fetch
            .get(&Request::xrpc(base, "com.atproto.repo.listRecords", params))
            .await?;
        if !reply.ok() {
            return Ok((!uris.is_empty()).then_some(uris));
        }
        let records = reply.items("records");
        uris.extend(
            records
                .iter()
                .filter_map(|r| r.get("uri").and_then(Value::as_str))
                .map(str::to_owned),
        );
        let next = reply
            .field("cursor")
            .and_then(Value::as_str)
            .filter(|c| !c.is_empty());
        let (false, Some(next)) = (records.is_empty(), next) else {
            break;
        };
        next.clone_into(&mut cursor);
    }
    uris.truncate(limit);
    Ok(Some(uris))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_match_pythons_urlencode() {
        let request = Request::xrpc(
            APPVIEW,
            "app.bsky.graph.getRelationships",
            vec![
                ("actor".into(), "did:plc:abc".into()),
                ("others".into(), "did:web:x.example".into()),
                ("others".into(), "é~_.-".into()),
            ],
        );
        assert_eq!(
            request.url(),
            "https://public.api.bsky.app/xrpc/app.bsky.graph.getRelationships\
             ?actor=did%3Aplc%3Aabc&others=did%3Aweb%3Ax.example&others=%C3%A9~_.-"
        );
    }
}

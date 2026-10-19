//! Live alerts: the frozen structural detector, on what a surge showed when
//! it triggered.
//!
//! This is the lab's `live` variant (`lab/PREREGISTRATION.md`, amendment 2),
//! ported step for step so that what was validated is what runs. A
//! [`Snapshot`] holds the arrivals, target post and quote visible at the
//! trigger. [`gather`] reads the facts the signals need through [`Fetch`],
//! [`signals`] computes the seven signals, and [`Reference::score`] ranks
//! each against the frozen reference: the mean of four equally weighted
//! families, each the mean of its signals' signed percentiles.

use std::collections::{BTreeMap, HashMap};

use az::Az;
use serde::{Deserialize, Serialize};

use crate::appview::{
    Fetch, FetchError, Likers, PostCounts, Relationship, follower_counts, likers, post_counts,
    post_uris_before, relationships,
};
use crate::surge::Surge;
use crate::tid::{decode_us, rkey_of};

const LIKER_PAGES: usize = 50;
const BASELINE_POSTS: usize = 200;
const MIN_MATCHED_BASELINE: usize = 10;

/// The quote of the target's posts leading at the trigger.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Quoter {
    /// The quote post.
    pub uri: String,
    /// Its author, the amplifier.
    pub author: String,
}

/// One response seen by the trigger.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Seen {
    /// The responding account.
    pub author: String,
    /// When it responded, in microseconds since the Unix epoch.
    pub at_us: i64,
}

/// What a surge showed when it triggered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    /// The account surged at.
    pub target: String,
    /// The surge's onset: the first stranger arrival in the opening window.
    pub onset_us: i64,
    /// When the surge crossed the detector's thresholds.
    pub triggered_us: i64,
    /// The target's post drawing the most arrivals by the trigger.
    pub target_post: Option<String>,
    /// The most engaged quote by the trigger.
    pub quoter: Option<Quoter>,
    /// Responses from onset to trigger, except the target's own and the
    /// quote itself.
    pub responses: Vec<Seen>,
}

impl Snapshot {
    /// Builds a snapshot from arrivals given as `(author, at_us, post uri)`,
    /// keeping those from onset to trigger that are neither the target's own
    /// posts nor the quote.
    pub fn new(
        target: String,
        onset_us: i64,
        triggered_us: i64,
        target_post: Option<String>,
        quoter: Option<Quoter>,
        arrivals: impl IntoIterator<Item = (String, i64, String)>,
    ) -> Self {
        let responses = arrivals
            .into_iter()
            .filter(|(author, at_us, post)| {
                (onset_us..=triggered_us).contains(at_us)
                    && *author != target
                    && quoter.as_ref().is_none_or(|q| *post != q.uri)
            })
            .map(|(author, at_us, _)| Seen { author, at_us })
            .collect();
        Self {
            target,
            onset_us,
            triggered_us,
            target_post,
            quoter,
            responses,
        }
    }

    /// Builds the snapshot of a surge reported as it opened (or closed: only
    /// arrivals up to the trigger count either way).
    pub fn from_surge(surge: &Surge) -> Self {
        let Surge {
            target,
            onset_us,
            triggered_us,
            trigger_target_post,
            trigger_quote,
            recorded,
            ..
        } = surge;
        let quoter = trigger_quote.as_ref().map(|q| Quoter {
            uri: q.uri.clone(),
            author: q.author.clone(),
        });
        Self::new(
            target.clone(),
            *onset_us,
            *triggered_us,
            trigger_target_post.clone(),
            quoter,
            recorded
                .iter()
                .map(|a| (a.author.clone(), a.at_us, a.post.clone())),
        )
    }

    /// Returns each responding account once, sorted.
    pub fn seen(&self) -> Vec<String> {
        let mut seen: Vec<String> = self.responses.iter().map(|r| r.author.clone()).collect();
        seen.sort_unstable();
        seen.dedup();
        seen
    }
}

/// What the signals need beyond the snapshot, read at the trigger.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Facts {
    /// The trigger target post, when it still resolves.
    pub target_post: Option<PostCounts>,
    /// Its likers; `None` without a target post.
    pub likers: Option<Likers>,
    /// The target's relationship with each responder.
    pub to_target: HashMap<String, Relationship>,
    /// The quoter's relationship with each responder.
    pub to_quoter: HashMap<String, Relationship>,
    /// Follower counts of the target and the quoter.
    pub followers: HashMap<String, u64>,
    /// The target's median replies per post before onset.
    pub median_replies: Option<f64>,
}

/// Reads the facts for `snapshot`, as the lab's `detector.live_row` does.
///
/// # Errors
///
/// Returns [`FetchError`] when a request gives up.
pub async fn gather<F: Fetch>(fetch: &F, snapshot: &Snapshot) -> Result<Facts, FetchError> {
    let target_post = match &snapshot.target_post {
        Some(uri) => post_counts(fetch, std::slice::from_ref(uri))
            .await?
            .remove(uri),
        None => None,
    };
    let likers = match &target_post {
        Some(post) if post.likes > 0 => Some(likers(fetch, &post.uri, LIKER_PAGES).await?),
        Some(_) => Some(Likers {
            complete: true,
            liked_at: HashMap::new(),
        }),
        None => None,
    };
    let seen = snapshot.seen();
    let to_target = relationships(fetch, &snapshot.target, &seen).await?;
    let mut people = vec![snapshot.target.clone()];
    let mut to_quoter = HashMap::new();
    if let Some(quoter) = &snapshot.quoter {
        to_quoter = relationships(fetch, &quoter.author, &seen).await?;
        people.push(quoter.author.clone());
    }
    let followers = follower_counts(fetch, &people).await?;
    let median_replies = median_replies(fetch, snapshot, target_post.as_ref()).await?;
    Ok(Facts {
        target_post,
        likers,
        to_target,
        to_quoter,
        followers,
        median_replies,
    })
}

async fn median_replies<F: Fetch>(
    fetch: &F,
    snapshot: &Snapshot,
    target_post: Option<&PostCounts>,
) -> Result<Option<f64>, FetchError> {
    let Some(uris) =
        post_uris_before(fetch, &snapshot.target, snapshot.onset_us, BASELINE_POSTS).await?
    else {
        return Ok(None);
    };
    if uris.is_empty() {
        return Ok(None);
    }
    let excluded = snapshot.target_post.as_deref().unwrap_or_default();
    let history: Vec<PostCounts> = post_counts(fetch, &uris)
        .await?
        .into_values()
        .filter(|p| p.uri != excluded && p.author == snapshot.target)
        .collect();
    Ok(baseline_median(&history, target_post.map(|p| p.is_reply)))
}

fn baseline_median(history: &[PostCounts], is_reply: Option<bool>) -> Option<f64> {
    if history.is_empty() {
        return None;
    }
    let matched: Vec<&PostCounts> = history
        .iter()
        .filter(|p| is_reply == Some(p.is_reply))
        .collect();
    let posts: Vec<&PostCounts> = if matched.len() >= MIN_MATCHED_BASELINE {
        matched
    } else {
        history.iter().collect()
    };
    let mut replies: Vec<u64> = posts.iter().map(|p| p.replies).collect();
    replies.sort_unstable();
    let middle = replies.len() / 2;
    Some(if replies.len() % 2 == 1 {
        replies[middle].az::<f64>()
    } else {
        (replies[middle - 1] + replies[middle]).az::<f64>() / 2.0
    })
}

/// A signal of the live detector, named as in the lab's feature rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Signal {
    /// Arrivals by the trigger against the target's median replies.
    #[serde(rename = "live_lift")]
    Lift,
    /// Likes on the target post per response, by the trigger.
    #[serde(rename = "live_likes_per_reply")]
    LikesPerReply,
    /// Share of arrivals liking the target post by the trigger.
    #[serde(rename = "live_liked_share")]
    LikedShare,
    /// `log10` of quoter over target followers.
    #[serde(rename = "live_follower_ratio_log10")]
    FollowerRatioLog10,
    /// The target's followers.
    #[serde(rename = "target_followers")]
    TargetFollowers,
    /// Share of arrivals following the quoter before onset.
    #[serde(rename = "live_follow_amplifier")]
    FollowAmplifier,
    /// Share of arrivals following the target before onset.
    #[serde(rename = "live_follow_target")]
    FollowTarget,
}

impl Signal {
    /// Every signal, in the lab's family order.
    pub const ALL: [Self; 7] = [
        Self::Lift,
        Self::LikesPerReply,
        Self::LikedShare,
        Self::FollowerRatioLog10,
        Self::TargetFollowers,
        Self::FollowAmplifier,
        Self::FollowTarget,
    ];

    /// Returns the signal's column name in the lab's feature rows.
    pub fn name(self) -> &'static str {
        match self {
            Self::Lift => "live_lift",
            Self::LikesPerReply => "live_likes_per_reply",
            Self::LikedShare => "live_liked_share",
            Self::FollowerRatioLog10 => "live_follower_ratio_log10",
            Self::TargetFollowers => "target_followers",
            Self::FollowAmplifier => "live_follow_amplifier",
            Self::FollowTarget => "live_follow_target",
        }
    }
}

/// A family of signals; families weigh equally.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Family {
    /// How many arrived, against the target's own baseline.
    Volume,
    /// Whether arrivals approve of the target post.
    Approval,
    /// The quoter's audience against the target's.
    Reach,
    /// Who arrives, and whom they follow.
    Audience,
}

/// Whether high or low values point to a pile-on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Direction {
    High,
    Low,
}

const FAMILIES: [(Family, &[(Signal, Direction)]); 4] = [
    (Family::Volume, &[(Signal::Lift, Direction::High)]),
    (
        Family::Approval,
        &[
            (Signal::LikesPerReply, Direction::Low),
            (Signal::LikedShare, Direction::Low),
        ],
    ),
    (
        Family::Reach,
        &[
            (Signal::FollowerRatioLog10, Direction::High),
            (Signal::TargetFollowers, Direction::Low),
        ],
    ),
    (
        Family::Audience,
        &[
            (Signal::FollowAmplifier, Direction::High),
            (Signal::FollowTarget, Direction::Low),
        ],
    ),
];

/// The seven signals of one snapshot; `None` where not measurable.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct Signals {
    /// Distinct accounts responding by the trigger.
    pub arrivals: usize,
    /// See [`Signal::Lift`].
    pub lift: Option<f64>,
    /// See [`Signal::LikesPerReply`].
    pub likes_per_reply: Option<f64>,
    /// See [`Signal::LikedShare`].
    pub liked_share: Option<f64>,
    /// See [`Signal::FollowerRatioLog10`].
    pub follower_ratio_log10: Option<f64>,
    /// See [`Signal::TargetFollowers`].
    pub target_followers: Option<f64>,
    /// See [`Signal::FollowAmplifier`].
    pub follow_amplifier: Option<f64>,
    /// See [`Signal::FollowTarget`].
    pub follow_target: Option<f64>,
}

impl Signals {
    /// Returns the value of one signal.
    pub fn value(&self, signal: Signal) -> Option<f64> {
        match signal {
            Signal::Lift => self.lift,
            Signal::LikesPerReply => self.likes_per_reply,
            Signal::LikedShare => self.liked_share,
            Signal::FollowerRatioLog10 => self.follower_ratio_log10,
            Signal::TargetFollowers => self.target_followers,
            Signal::FollowAmplifier => self.follow_amplifier,
            Signal::FollowTarget => self.follow_target,
        }
    }
}

fn share(count: usize, total: usize) -> Option<f64> {
    (total > 0).then(|| count.az::<f64>() / total.az::<f64>())
}

/// Parses a client `createdAt` to microseconds, truncating finer digits.
fn created_us(text: &str) -> Option<i64> {
    text.parse::<jiff::Timestamp>()
        .ok()
        .map(jiff::Timestamp::as_microsecond)
}

fn followed_before(uri: Option<&str>, moment_us: i64) -> Option<bool> {
    let at = decode_us(rkey_of(uri?)).ok()?;
    Some(at < moment_us)
}

/// Computes the live signals of `snapshot` from its `facts`.
pub fn signals(snapshot: &Snapshot, facts: &Facts) -> Signals {
    let mut first: HashMap<&str, i64> = HashMap::new();
    for response in &snapshot.responses {
        first
            .entry(response.author.as_str())
            .and_modify(|at| *at = (*at).min(response.at_us))
            .or_insert(response.at_us);
    }
    let arrivals = first.len();
    let cut = snapshot.triggered_us;
    let mut out = Signals {
        arrivals,
        ..Signals::default()
    };
    if let Some(liked) = facts.likers.as_ref().filter(|l| l.complete) {
        let by_cut = |at: Option<&String>| at.and_then(|t| created_us(t)).is_some_and(|t| t <= cut);
        let likes = liked.liked_at.values().filter(|t| by_cut(Some(t))).count();
        let liked_arrivals = first
            .keys()
            .filter(|a| by_cut(liked.liked_at.get(**a)))
            .count();
        out.liked_share = share(liked_arrivals, arrivals);
        out.likes_per_reply =
            Some((likes + 1).az::<f64>() / (snapshot.responses.len() + 1).az::<f64>());
    }
    let has_quoter = snapshot.quoter.is_some();
    let (mut resolved, mut fans, mut audience) = (0_usize, 0_usize, 0_usize);
    for author in first.keys() {
        let Some(to_target) = facts.to_target.get(*author).filter(|r| r.resolved) else {
            continue;
        };
        let to_quoter = facts.to_quoter.get(*author);
        if has_quoter && to_quoter.is_none() {
            continue;
        }
        resolved += 1;
        if followed_before(to_target.follows_actor.as_deref(), snapshot.onset_us) == Some(true) {
            fans += 1;
        }
        if to_quoter.and_then(|r| followed_before(r.follows_actor.as_deref(), snapshot.onset_us))
            == Some(true)
        {
            audience += 1;
        }
    }
    out.follow_target = share(fans, resolved);
    out.follow_amplifier = if has_quoter {
        share(audience, resolved)
    } else {
        None
    };
    out.lift = facts
        .median_replies
        .map(|median| arrivals.az::<f64>() / median.max(1.0));
    let target_followers = facts.followers.get(&snapshot.target).copied();
    let quoter_followers = snapshot
        .quoter
        .as_ref()
        .and_then(|q| facts.followers.get(&q.author).copied());
    out.target_followers = target_followers.map(Az::az::<f64>);
    if let (Some(quoter), Some(target)) = (quoter_followers, target_followers) {
        out.follower_ratio_log10 =
            Some(((quoter + 1).az::<f64>() / (target + 1).az::<f64>()).log10());
    }
    out
}

/// The alert rates the frozen cutoffs were set for, on reference surges.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Budget {
    /// Alerts on 5% of reference surges.
    #[serde(rename = "0.05")]
    Five,
    /// Alerts on 10% of reference surges.
    #[serde(rename = "0.10")]
    Ten,
    /// Alerts on 20% of reference surges.
    #[serde(rename = "0.20")]
    Twenty,
}

/// A reference file that cannot drive the live detector.
#[derive(Debug, thiserror::Error)]
pub enum ReferenceError {
    /// Not valid reference JSON (including unknown signal names).
    #[error("bad reference: {0}")]
    Json(#[from] serde_json::Error),
    /// A reference frozen for another variant.
    #[error("reference is for variant {0:?}, not live")]
    Variant(String),
}

#[derive(Deserialize)]
struct ReferenceFile {
    variant: String,
    signals: BTreeMap<Signal, Vec<f64>>,
    cutoffs: BTreeMap<Budget, f64>,
}

/// Frozen order statistics per signal and alert cutoffs per budget.
#[derive(Debug, Clone, PartialEq)]
pub struct Reference {
    signals: BTreeMap<Signal, Vec<f64>>,
    cutoffs: BTreeMap<Budget, f64>,
}

/// A snapshot's score and the family scores it averages.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Score {
    /// The detector score, in `[0, 1]`; `None` with fewer than two families.
    pub value: Option<f64>,
    /// Each measurable family's mean signed percentile.
    pub families: Vec<(Family, f64)>,
}

impl Reference {
    /// Parses `lab/detector/live-reference.json`.
    ///
    /// # Errors
    ///
    /// Returns [`ReferenceError`] for malformed JSON, an unknown signal or
    /// budget, or a reference frozen for another variant.
    pub fn from_json(text: &str) -> Result<Self, ReferenceError> {
        let ReferenceFile {
            variant,
            signals,
            cutoffs,
        } = serde_json::from_str(text)?;
        if variant != "live" {
            return Err(ReferenceError::Variant(variant));
        }
        Ok(Self { signals, cutoffs })
    }

    /// Returns the frozen cutoff for `budget`, if the reference has one.
    pub fn cutoff(&self, budget: Budget) -> Option<f64> {
        self.cutoffs.get(&budget).copied()
    }

    /// Scores `signals`: the mean over measurable families of each family's
    /// mean signed percentile, summed in the lab's order.
    pub fn score(&self, signals: &Signals) -> Score {
        let mut families = Vec::new();
        for (family, members) in FAMILIES {
            let mut total = 0.0;
            let mut counted = 0_usize;
            for &(signal, direction) in members {
                let Some(value) = signals.value(signal).filter(|v| v.is_finite()) else {
                    continue;
                };
                let Some(reference) = self.signals.get(&signal).filter(|r| !r.is_empty()) else {
                    continue;
                };
                let rank = percentile(reference, value);
                total += match direction {
                    Direction::High => rank,
                    Direction::Low => 1.0 - rank,
                };
                counted += 1;
            }
            if counted > 0 {
                families.push((family, total / counted.az::<f64>()));
            }
        }
        let value = (families.len() >= 2).then(|| {
            families.iter().map(|(_, v)| v).fold(0.0, |sum, v| sum + v) / families.len().az::<f64>()
        });
        Score { value, families }
    }
}

/// Returns the mid-rank of `value` among sorted `reference` values, in `[0, 1]`.
///
/// # Examples
///
/// ```
/// use pileon_core::alert::percentile;
/// assert_eq!(percentile(&[1.0, 2.0, 3.0, 4.0], 2.0), 0.375);
/// assert_eq!(percentile(&[1.0, 2.0], 0.0), 0.0);
/// ```
pub fn percentile(reference: &[f64], value: f64) -> f64 {
    let below = reference.partition_point(|&r| r < value);
    let upto = reference.partition_point(|&r| r <= value);
    (below + upto).az::<f64>() / (2 * reference.len()).az::<f64>()
}

#[cfg(test)]
mod tests {
    use super::*;

    const ONSET: i64 = 1_786_000_000_000_000;
    const MINUTE: i64 = 60_000_000;

    fn snapshot() -> Snapshot {
        let arrival = |author: &str, minute: i64, post: &str| {
            (author.to_owned(), ONSET + minute * MINUTE, post.to_owned())
        };
        Snapshot::new(
            "did:plc:t".to_owned(),
            ONSET,
            ONSET + 30 * MINUTE,
            Some("at://did:plc:t/app.bsky.feed.post/p".to_owned()),
            Some(Quoter {
                uri: "at://did:plc:q/app.bsky.feed.post/d".to_owned(),
                author: "did:plc:q".to_owned(),
            }),
            vec![
                arrival("did:plc:a", -5, "at://did:plc:a/app.bsky.feed.post/0"),
                arrival("did:plc:q", 0, "at://did:plc:q/app.bsky.feed.post/d"),
                arrival("did:plc:a", 1, "at://did:plc:a/app.bsky.feed.post/1"),
                arrival("did:plc:a", 2, "at://did:plc:a/app.bsky.feed.post/2"),
                arrival("did:plc:b", 3, "at://did:plc:b/app.bsky.feed.post/1"),
                arrival("did:plc:t", 4, "at://did:plc:t/app.bsky.feed.post/9"),
                arrival("did:plc:c", 31, "at://did:plc:c/app.bsky.feed.post/1"),
            ],
        )
    }

    fn follow(minute: i64) -> Option<String> {
        let tid = crate::tid::encode_us(ONSET + minute * MINUTE).ok()?;
        Some(format!("at://did:plc:x/app.bsky.graph.follow/{tid}"))
    }

    #[test]
    fn snapshot_keeps_responses_from_onset_to_trigger() {
        let snap = snapshot();
        assert_eq!(snap.responses.len(), 3);
        assert_eq!(snap.seen(), vec!["did:plc:a", "did:plc:b"]);
    }

    #[test]
    fn signals_follow_the_lab_definitions() {
        let snap = snapshot();
        let resolved = |follows_actor| Relationship {
            resolved: true,
            actor_follows: None,
            follows_actor,
        };
        let facts = Facts {
            likers: Some(Likers {
                complete: true,
                liked_at: HashMap::from([
                    ("did:plc:a".to_owned(), "2026-08-06T07:00:00Z".to_owned()),
                    ("did:plc:z".to_owned(), "2002-01-01T00:00:00Z".to_owned()),
                    ("did:plc:b".to_owned(), "2099-01-01T00:00:00Z".to_owned()),
                ]),
            }),
            to_target: HashMap::from([
                ("did:plc:a".to_owned(), resolved(follow(-60))),
                ("did:plc:b".to_owned(), resolved(follow(10))),
            ]),
            to_quoter: HashMap::from([
                ("did:plc:a".to_owned(), resolved(None)),
                ("did:plc:b".to_owned(), resolved(follow(-1))),
            ]),
            followers: HashMap::from([
                ("did:plc:t".to_owned(), 99),
                ("did:plc:q".to_owned(), 9_999),
            ]),
            median_replies: Some(0.5),
            target_post: None,
        };
        let found = signals(&snap, &facts);
        assert_eq!(found.arrivals, 2);
        assert_eq!(found.lift, Some(2.0));
        assert_eq!(found.likes_per_reply, Some(3.0 / 4.0));
        assert_eq!(found.liked_share, Some(0.5));
        assert_eq!(found.follow_target, Some(0.5));
        assert_eq!(found.follow_amplifier, Some(0.5));
        assert_eq!(found.follower_ratio_log10, Some(2.0));
        assert_eq!(found.target_followers, Some(99.0));
        for signal in Signal::ALL {
            let named = serde_json::to_value(signal).expect("serializes");
            assert_eq!(named, signal.name());
        }
    }

    fn block_on<F: Future>(future: F) -> F::Output {
        let mut future = std::pin::pin!(future);
        let mut cx = std::task::Context::from_waker(std::task::Waker::noop());
        loop {
            if let std::task::Poll::Ready(value) = future.as_mut().poll(&mut cx) {
                return value;
            }
        }
    }

    struct FakeNetwork;

    fn reply(body: &serde_json::Value) -> crate::appview::Reply {
        crate::appview::Reply {
            status: 200,
            body: body.as_object().cloned(),
        }
    }

    fn iso(minutes: i64) -> String {
        jiff::Timestamp::from_microsecond(ONSET + minutes * MINUTE)
            .map(|t| t.to_string())
            .unwrap_or_default()
    }

    impl Fetch for FakeNetwork {
        fn get(
            &self,
            request: &crate::appview::Request,
        ) -> impl Future<Output = Result<crate::appview::Reply, FetchError>> {
            let values = |key: &str| -> Vec<String> {
                request
                    .params
                    .iter()
                    .filter(|(k, _)| k == key)
                    .map(|(_, v)| v.clone())
                    .collect()
            };
            let target = "did:plc:t";
            let path = request.path.as_str();
            let found = if request.base == crate::appview::PLC {
                reply(&serde_json::json!({"service": [
                    {"id": "#atproto_pds", "serviceEndpoint": "https://pds.example/"}]}))
            } else if path.ends_with("listRecords") {
                let records: Vec<_> = [1, 3, 5]
                    .iter()
                    .map(|n| serde_json::json!({"uri": format!("at://{target}/app.bsky.feed.post/h{n}")}))
                    .collect();
                reply(&serde_json::json!({ "records": records }))
            } else if path.ends_with("getPosts") {
                let posts: Vec<_> = values("uris")
                    .iter()
                    .filter_map(|uri| {
                        let rkey = uri.rsplit('/').next()?;
                        let (replies, likes) = match rkey {
                            "p" => (9, 2),
                            "h1" => (1, 0),
                            "h3" => (3, 0),
                            "h5" => (5, 0),
                            _ => return None,
                        };
                        Some(serde_json::json!({"uri": uri, "author": {"did": target},
                            "record": {"text": ""}, "replyCount": replies, "likeCount": likes}))
                    })
                    .collect();
                reply(&serde_json::json!({ "posts": posts }))
            } else if path.ends_with("getLikes") {
                reply(&serde_json::json!({"likes": [
                    {"actor": {"did": "did:plc:a"}, "createdAt": iso(1)},
                    {"actor": {"did": "did:plc:z"}, "createdAt": iso(60 * 24)}]}))
            } else if path.ends_with("getRelationships") {
                let actor = values("actor").pop().unwrap_or_default();
                let rels: Vec<_> = values("others")
                    .iter()
                    .map(|other| {
                        let followed = match (actor.as_str(), other.as_str()) {
                            ("did:plc:t", "did:plc:a") => follow(-60),
                            ("did:plc:q", "did:plc:b") => follow(-1),
                            _ => None,
                        };
                        serde_json::json!({"$type": "app.bsky.graph.defs#relationship",
                            "did": other, "followedBy": followed})
                    })
                    .collect();
                reply(&serde_json::json!({ "relationships": rels }))
            } else if path.ends_with("getProfiles") {
                let profiles: Vec<_> = values("actors")
                    .iter()
                    .map(|did| {
                        let count = if did == target { 99 } else { 9_999 };
                        serde_json::json!({"did": did, "followersCount": count})
                    })
                    .collect();
                reply(&serde_json::json!({ "profiles": profiles }))
            } else {
                crate::appview::Reply {
                    status: 404,
                    body: None,
                }
            };
            async move { Ok(found) }
        }
    }

    #[test]
    fn gather_and_signals_match_the_lab_fixture() {
        let snap = Snapshot::new(
            "did:plc:t".to_owned(),
            ONSET,
            ONSET + 30 * MINUTE,
            Some("at://did:plc:t/app.bsky.feed.post/p".to_owned()),
            Some(Quoter {
                uri: "at://did:plc:q/app.bsky.feed.post/q".to_owned(),
                author: "did:plc:q".to_owned(),
            }),
            [
                ("did:plc:q", 0, "q"),
                ("did:plc:a", 1, "a1"),
                ("did:plc:a", 2, "a2"),
                ("did:plc:b", 3, "b1"),
                ("did:plc:c", 31, "c1"),
            ]
            .map(|(author, minute, rkey)| {
                (
                    author.to_owned(),
                    ONSET + minute * MINUTE,
                    format!("at://{author}/app.bsky.feed.post/{rkey}"),
                )
            }),
        );
        let facts = block_on(gather(&FakeNetwork, &snap)).expect("the fake never fails");
        let found = signals(&snap, &facts);
        assert_eq!(found.arrivals, 2);
        assert_eq!(found.lift, Some(2.0 / 3.0));
        assert_eq!(found.likes_per_reply, Some(0.5));
        assert_eq!(found.liked_share, Some(0.5));
        assert_eq!(found.follow_target, Some(0.5));
        assert_eq!(found.follow_amplifier, Some(0.5));
        assert_eq!(found.follower_ratio_log10, Some(2.0));
        assert_eq!(found.target_followers, Some(99.0));
    }

    #[test]
    fn baseline_prefers_the_target_posts_kind() {
        let post = |n: u64, is_reply: bool| PostCounts {
            uri: format!("at://t/p/{n}"),
            author: "t".to_owned(),
            is_reply,
            replies: n,
            likes: 0,
            quotes: 0,
            reposts: 0,
        };
        let mut history: Vec<PostCounts> = (0..10).map(|n| post(n, true)).collect();
        history.push(post(100, false));
        assert_eq!(baseline_median(&history, Some(true)), Some(4.5));
        assert_eq!(baseline_median(&history, Some(false)), Some(5.0));
        assert_eq!(baseline_median(&history, None), Some(5.0));
        assert_eq!(baseline_median(&[], None), None);
    }

    #[test]
    fn score_averages_families_and_needs_two() {
        let reference = Reference::from_json(
            r#"{"variant": "live", "cutoffs": {"0.10": 0.6},
                "signals": {"live_lift": [1.0, 2.0, 3.0, 4.0],
                            "target_followers": [10.0, 20.0]}}"#,
        )
        .expect("valid reference");
        let mut found = Signals {
            lift: Some(4.0),
            ..Signals::default()
        };
        assert_eq!(reference.score(&found).value, None);
        found.target_followers = Some(15.0);
        let score = reference.score(&found);
        assert_eq!(
            score.families,
            vec![(Family::Volume, 0.875), (Family::Reach, 0.5)]
        );
        assert_eq!(score.value, Some(0.6875));
        assert_eq!(reference.cutoff(Budget::Ten), Some(0.6));
        assert!(
            Reference::from_json(r#"{"variant": "early", "signals": {}, "cutoffs": {}}"#).is_err()
        );
        assert!(
            Reference::from_json(r#"{"variant": "live", "signals": {"x": []}, "cutoffs": {}}"#)
                .is_err()
        );
    }
}

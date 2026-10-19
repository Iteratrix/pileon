//! Surge detection: bursts of strangers arriving at a target.
//!
//! An *arrival* is a reply to, quote of, or mention of an account (the target)
//! by someone else. It is a *stranger* arrival when the two accounts have not
//! interacted, in either direction, within the stranger memory (30 days by
//! default). Each target keeps a baseline rate of stranger arrivals as an
//! exponentially decayed count, so "a lot" is always relative to that target.
//!
//! A surge opens when, within the trailing window, at least `min_strangers`
//! distinct strangers arrived and that count is at least `min_surprise`
//! (`-log10` of the Poisson tail) above the baseline. It stays open, recording
//! every arrival and the engagement on quotes of the target's posts (the
//! amplifier candidates), until no stranger has arrived for `quiet_hours` or
//! it reaches `max_hours`.
//!
//! When a surge opens it also keeps what could be seen at that moment: the
//! target's post drawing the most arrivals so far and the most engaged quote
//! of the target's posts so far, its engagement counted from when it was
//! posted. A live alert can only act on these.
//!
//! The detector is pure and driven only by event times, so replaying history
//! and following the live stream give identical results.

use std::collections::{HashMap, VecDeque};
use std::hash::{DefaultHasher, Hash, Hasher};

use az::Az;
use serde::Serialize;

use crate::bloom::DayBlooms;
use crate::poisson::surprise;

const US_PER_MINUTE: i64 = 60_000_000;
const US_PER_HOUR: i64 = 3_600_000_000;
const US_PER_DAY: i64 = 86_400_000_000;
const SWEEP_EVERY_US: i64 = 5 * US_PER_MINUTE;
const EVICT_EVERY_US: i64 = 6 * US_PER_HOUR;
const EVICT_BELOW_LEVEL: f64 = 0.05;
const RECENT_CAP: usize = 2_000;
const TOP_TARGET_POSTS: usize = 20;
const TOP_QUOTE_POSTS: usize = 50;

/// Every dial of the detector.
#[derive(Debug, Clone, PartialEq)]
pub struct SurgeConfig {
    /// The trailing window arrivals are counted in, in minutes.
    pub window_minutes: i64,
    /// Fewest distinct strangers in the window that can open a surge.
    pub min_strangers: u32,
    /// Least `-log10 P` of that count under the baseline that opens a surge.
    pub min_surprise: f64,
    /// Half-life of the baseline's memory, in hours.
    pub baseline_half_life_hours: f64,
    /// Baseline assumed for a target with no history, in strangers per day.
    pub prior_per_day: f64,
    /// How long an interaction keeps two accounts from being strangers.
    pub stranger_days: u32,
    /// Expected interacting pairs per day (sizes the stranger memory).
    pub pairs_per_day: u64,
    /// How far back arrivals are kept for backfilling a newly opened surge.
    pub lookback_minutes: i64,
    /// A surge closes after this long without a stranger arrival.
    pub quiet_hours: i64,
    /// A surge closes after this long regardless.
    pub max_hours: i64,
    /// Surges opening this soon after the first event are flagged as warm-up.
    pub warmup_days: i64,
    /// Most arrivals kept per surge record.
    pub max_recorded: usize,
}

impl Default for SurgeConfig {
    fn default() -> Self {
        Self {
            window_minutes: 60,
            min_strangers: 8,
            min_surprise: 6.0,
            baseline_half_life_hours: 168.0,
            prior_per_day: 0.5,
            stranger_days: 30,
            pairs_per_day: 6_000_000,
            lookback_minutes: 180,
            quiet_hours: 6,
            max_hours: 48,
            warmup_days: 14,
            max_recorded: 5_000,
        }
    }
}

/// How an arrival reached the target.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ArrivalKind {
    /// A reply to one of the target's posts.
    Reply,
    /// A quote of one of the target's posts.
    Quote,
    /// A post mentioning the target.
    Mention,
}

/// A post, reduced to what surge detection needs.
#[derive(Debug, Clone, Copy)]
pub struct PostEvent<'a> {
    /// When the post was made, in microseconds since the Unix epoch.
    pub at_us: i64,
    /// The posting account.
    pub author: &'a str,
    /// The post's AT URI.
    pub uri: &'a str,
    /// The post it replies to.
    pub parent: Option<&'a str>,
    /// The record it quotes.
    pub quote: Option<&'a str>,
    /// Accounts it mentions.
    pub mentions: &'a [String],
}

/// Approval that is not a post.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EngagementKind {
    /// A like.
    Like,
    /// A repost.
    Repost,
}

/// One arrival kept in a surge record.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RecordedArrival {
    /// When it happened, in microseconds since the Unix epoch.
    pub at_us: i64,
    /// The arriving account.
    pub author: String,
    /// How it arrived.
    pub kind: ArrivalKind,
    /// The arriving post.
    pub post: String,
    /// The target's post it replied to or quoted.
    pub target_post: Option<String>,
    /// Whether the two accounts had not interacted recently.
    pub stranger: bool,
}

/// Engagement collected by a quote of the target's post during a surge.
#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct Engagement {
    /// Replies to the quote post.
    pub replies: u32,
    /// Quotes of the quote post.
    pub quotes: u32,
    /// Reposts of the quote post.
    pub reposts: u32,
    /// Likes on the quote post.
    pub likes: u32,
}

impl Engagement {
    fn total(&self) -> u32 {
        self.replies + self.quotes + self.reposts + self.likes
    }
}

/// A quote of one of the target's posts: an amplifier candidate.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct QuotePost {
    /// The quote post.
    pub uri: String,
    /// Its author.
    pub author: String,
    /// When it was posted.
    pub at_us: i64,
    /// What it drew while the surge was open.
    pub engagement: Engagement,
}

/// A closed surge.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Surge {
    /// The account surged at.
    pub target: String,
    /// The first stranger arrival in the window that opened the surge.
    pub onset_us: i64,
    /// When the surge crossed both thresholds.
    pub triggered_us: i64,
    /// The target's post drawing the most arrivals from onset to trigger.
    pub trigger_target_post: Option<String>,
    /// The most engaged quote of the target's posts at trigger, with its
    /// engagement from posting to trigger.
    pub trigger_quote: Option<QuotePost>,
    /// The last arrival recorded.
    pub last_us: i64,
    /// Whether it opened during warm-up, when baselines were still forming.
    pub warmup: bool,
    /// The target's baseline when the surge opened, in strangers per hour.
    pub baseline_per_hour: f64,
    /// Distinct strangers in the window when the surge opened.
    pub trigger_strangers: u32,
    /// Surprise when the surge opened.
    pub trigger_surprise: f64,
    /// Most distinct strangers in any window while open.
    pub peak_strangers: u32,
    /// Highest surprise while open.
    pub peak_surprise: f64,
    /// Distinct arriving accounts from onset on.
    pub arrivals: u32,
    /// Distinct stranger accounts from onset on.
    pub strangers: u32,
    /// Reply arrivals from onset on.
    pub replies: u32,
    /// Quote arrivals from onset on.
    pub quotes: u32,
    /// Mention arrivals from onset on.
    pub mentions: u32,
    /// The target's posts drawing the most arrivals, with counts.
    pub target_posts: Vec<(String, u32)>,
    /// Quotes of the target's posts, most engaged first.
    pub quote_posts: Vec<QuotePost>,
    /// Arrivals, including the lookback before onset, oldest first.
    pub recorded: Vec<RecordedArrival>,
    /// Whether arrivals beyond `max_recorded` were dropped.
    pub truncated: bool,
}

#[derive(Default)]
struct TargetState {
    level: f64,
    level_at_us: i64,
    window: VecDeque<(i64, u64)>,
    counts: HashMap<u64, u32>,
    recent: VecDeque<RecordedArrival>,
}

impl TargetState {
    fn expire(&mut self, now_us: i64, config: &SurgeConfig) {
        let window_start = now_us - config.window_minutes * US_PER_MINUTE;
        while let Some(&(at, author)) = self.window.front() {
            if at >= window_start {
                break;
            }
            self.window.pop_front();
            self.fold(at, config);
            if let Some(count) = self.counts.get_mut(&author) {
                *count -= 1;
                if *count == 0 {
                    self.counts.remove(&author);
                }
            }
        }
        let lookback_start = now_us - config.lookback_minutes * US_PER_MINUTE;
        while self
            .recent
            .front()
            .is_some_and(|a| a.at_us < lookback_start)
        {
            self.recent.pop_front();
        }
    }

    fn fold(&mut self, at_us: i64, config: &SurgeConfig) {
        if at_us > self.level_at_us {
            self.level *= decay(at_us - self.level_at_us, config);
            self.level_at_us = at_us;
        }
        self.level += 1.0;
    }

    /// Estimates strangers per hour from the decayed count. Dividing by the
    /// decayed length of time actually observed (not the steady-state mean
    /// life) keeps the estimate unbiased while history is still short.
    fn rate_per_hour(&self, at_us: i64, observed_us: i64, config: &SurgeConfig) -> f64 {
        let level = self.level * decay((at_us - self.level_at_us).max(0), config);
        let mean_life_hours = config.baseline_half_life_hours / std::f64::consts::LN_2;
        let window_hours = config.window_minutes.az::<f64>() / 60.0;
        let observed_hours = (observed_us.az::<f64>() / US_PER_HOUR.az::<f64>()).max(window_hours);
        let effective_hours = mean_life_hours * (1.0 - (-observed_hours / mean_life_hours).exp());
        level / effective_hours + config.prior_per_day / 24.0
    }

    fn score(&self, at_us: i64, observed_us: i64, config: &SurgeConfig) -> (u32, f64, f64) {
        let strangers = self.counts.len().az::<u32>();
        let rate = self.rate_per_hour(at_us, observed_us, config);
        let mean = rate * config.window_minutes.az::<f64>() / 60.0;
        (strangers, surprise(strangers, mean), rate)
    }
}

fn decay(elapsed_us: i64, config: &SurgeConfig) -> f64 {
    let hours = elapsed_us.az::<f64>() / US_PER_HOUR.az::<f64>();
    (-hours / config.baseline_half_life_hours).exp2()
}

/// Engagement on every quote of anyone's post within the lookback, so a
/// surge that opens later knows what its quotes drew before it opened.
#[derive(Default)]
struct RecentQuotes {
    engagement: HashMap<String, Engagement>,
    order: VecDeque<(i64, String)>,
}

impl RecentQuotes {
    fn add(&mut self, uri: &str, at_us: i64) {
        if self.engagement.contains_key(uri) {
            return;
        }
        self.engagement
            .insert(uri.to_owned(), Engagement::default());
        self.order.push_back((at_us, uri.to_owned()));
    }

    fn expire(&mut self, before_us: i64) {
        while self.order.front().is_some_and(|(at, _)| *at < before_us) {
            if let Some((_, uri)) = self.order.pop_front() {
                self.engagement.remove(&uri);
            }
        }
    }
}

struct OpenSurge {
    surge: Surge,
    last_stranger_us: i64,
    authors: HashMap<u64, bool>,
    target_posts: HashMap<String, u32>,
    quote_posts: HashMap<String, QuotePost>,
}

impl OpenSurge {
    fn record(&mut self, arrival: &RecordedArrival, author: u64, max_recorded: usize) {
        if arrival.at_us >= self.surge.onset_us {
            let stranger = self.authors.entry(author).or_insert(false);
            *stranger |= arrival.stranger;
            match arrival.kind {
                ArrivalKind::Reply => self.surge.replies += 1,
                ArrivalKind::Quote => self.surge.quotes += 1,
                ArrivalKind::Mention => self.surge.mentions += 1,
            }
            if let Some(post) = &arrival.target_post {
                *self.target_posts.entry(post.clone()).or_default() += 1;
            }
            if arrival.stranger {
                self.last_stranger_us = self.last_stranger_us.max(arrival.at_us);
            }
        }
        self.surge.last_us = self.surge.last_us.max(arrival.at_us);
        if arrival.kind == ArrivalKind::Quote {
            self.quote_posts
                .entry(arrival.post.clone())
                .or_insert_with(|| QuotePost {
                    uri: arrival.post.clone(),
                    author: arrival.author.clone(),
                    at_us: arrival.at_us,
                    engagement: Engagement::default(),
                });
        }
        if self.surge.recorded.len() < max_recorded {
            self.surge.recorded.push(arrival.clone());
        } else {
            self.surge.truncated = true;
        }
    }

    /// Keeps the leading target post and quote as they stood at trigger.
    fn snapshot_trigger(&mut self, quotes: &RecentQuotes) {
        self.surge.trigger_target_post = self
            .target_posts
            .iter()
            .max_by(|a, b| a.1.cmp(b.1).then_with(|| b.0.cmp(a.0)))
            .map(|(uri, _)| uri.clone());
        self.surge.trigger_quote = self
            .quote_posts
            .values()
            .map(|quote| QuotePost {
                engagement: quotes
                    .engagement
                    .get(&quote.uri)
                    .cloned()
                    .unwrap_or_default(),
                ..quote.clone()
            })
            .max_by(|a, b| {
                a.engagement
                    .total()
                    .cmp(&b.engagement.total())
                    .then_with(|| b.at_us.cmp(&a.at_us))
                    .then_with(|| b.uri.cmp(&a.uri))
            });
    }

    fn finish(self) -> Surge {
        let Self {
            mut surge,
            authors,
            target_posts,
            quote_posts,
            ..
        } = self;
        surge.arrivals = authors.len().az::<u32>();
        surge.strangers = authors.values().filter(|s| **s).count().az::<u32>();
        let mut posts: Vec<(String, u32)> = target_posts.into_iter().collect();
        posts.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        posts.truncate(TOP_TARGET_POSTS);
        surge.target_posts = posts;
        let mut quotes: Vec<QuotePost> = quote_posts.into_values().collect();
        quotes.sort_by(|a, b| {
            b.engagement
                .total()
                .cmp(&a.engagement.total())
                .then_with(|| a.at_us.cmp(&b.at_us))
        });
        quotes.truncate(TOP_QUOTE_POSTS);
        surge.quote_posts = quotes;
        surge.recorded.sort_by_key(|a| a.at_us);
        surge
    }
}

/// The streaming surge detector.
pub struct SurgeDetector {
    config: SurgeConfig,
    pairs: DayBlooms,
    targets: HashMap<u64, TargetState>,
    open: HashMap<u64, OpenSurge>,
    quote_owner: HashMap<String, u64>,
    recent_quotes: RecentQuotes,
    now_us: i64,
    started_us: Option<i64>,
    last_sweep_us: i64,
    last_evict_us: i64,
}

impl SurgeDetector {
    /// Creates a detector with the given dials.
    pub fn new(config: SurgeConfig) -> Self {
        let pairs = DayBlooms::new(config.stranger_days, config.pairs_per_day, 0.01);
        Self {
            config,
            pairs,
            targets: HashMap::new(),
            open: HashMap::new(),
            quote_owner: HashMap::new(),
            recent_quotes: RecentQuotes::default(),
            now_us: i64::MIN,
            started_us: None,
            last_sweep_us: 0,
            last_evict_us: 0,
        }
    }

    /// Returns how many targets are tracked and how many surges are open.
    pub fn load(&self) -> (usize, usize) {
        (self.targets.len(), self.open.len())
    }

    /// Feeds one post; returns surges that closed as time moved forward.
    pub fn observe_post(&mut self, post: &PostEvent<'_>) -> Vec<Surge> {
        let closed = self.advance(post.at_us);
        if let Some(parent) = post.parent {
            self.engage(parent, |e| e.replies += 1);
        }
        if let Some(quote) = post.quote {
            self.engage(quote, |e| e.quotes += 1);
        }
        for (target, kind, target_post) in arrivals_of(post) {
            self.arrive(target, kind, target_post, post);
        }
        closed
    }

    /// Feeds one like or repost of `subject`.
    pub fn observe_engagement(
        &mut self,
        subject: &str,
        kind: EngagementKind,
        at_us: i64,
    ) -> Vec<Surge> {
        let closed = self.advance(at_us);
        match kind {
            EngagementKind::Like => self.engage(subject, |e| e.likes += 1),
            EngagementKind::Repost => self.engage(subject, |e| e.reposts += 1),
        }
        closed
    }

    /// Closes and returns every open surge (end of input).
    pub fn finish(&mut self) -> Vec<Surge> {
        let keys: Vec<u64> = self.open.keys().copied().collect();
        keys.into_iter().filter_map(|key| self.close(key)).collect()
    }

    fn engage(&mut self, uri: &str, bump: impl Fn(&mut Engagement)) {
        if let Some(engagement) = self.recent_quotes.engagement.get_mut(uri) {
            bump(engagement);
        }
        let Some(owner) = self.quote_owner.get(uri) else {
            return;
        };
        let Some(open) = self.open.get_mut(owner) else {
            return;
        };
        if let Some(quote) = open.quote_posts.get_mut(uri) {
            bump(&mut quote.engagement);
        }
    }

    fn advance(&mut self, at_us: i64) -> Vec<Surge> {
        self.started_us.get_or_insert(at_us);
        if at_us <= self.now_us {
            return Vec::new();
        }
        self.now_us = at_us;
        let mut closed = Vec::new();
        if self.now_us - self.last_sweep_us >= SWEEP_EVERY_US {
            self.last_sweep_us = self.now_us;
            self.recent_quotes
                .expire(self.now_us - self.config.lookback_minutes * US_PER_MINUTE);
            let quiet = self.config.quiet_hours * US_PER_HOUR;
            let longest = self.config.max_hours * US_PER_HOUR;
            let done: Vec<u64> = self
                .open
                .iter()
                .filter(|(_, o)| {
                    self.now_us - o.last_stranger_us > quiet
                        || self.now_us - o.surge.onset_us > longest
                })
                .map(|(key, _)| *key)
                .collect();
            closed.extend(done.into_iter().filter_map(|key| self.close(key)));
        }
        if self.now_us - self.last_evict_us >= EVICT_EVERY_US {
            self.last_evict_us = self.now_us;
            self.evict();
        }
        closed
    }

    fn evict(&mut self) {
        let now = self.now_us;
        let config = &self.config;
        let open = &self.open;
        self.targets.retain(|key, state| {
            state.expire(now, config);
            open.contains_key(key)
                || !state.window.is_empty()
                || !state.recent.is_empty()
                || state.level * decay((now - state.level_at_us).max(0), config)
                    >= EVICT_BELOW_LEVEL
        });
    }

    fn close(&mut self, key: u64) -> Option<Surge> {
        let open = self.open.remove(&key)?;
        for uri in open.quote_posts.keys() {
            self.quote_owner.remove(uri);
        }
        Some(open.finish())
    }

    fn arrive(
        &mut self,
        target: &str,
        kind: ArrivalKind,
        target_post: Option<&str>,
        post: &PostEvent<'_>,
    ) {
        let config = &self.config;
        let target_key = key_of(target);
        let author_key = key_of(post.author);
        let pair = pair_key(author_key, target_key);
        let day = post.at_us.div_euclid(US_PER_DAY);
        let stranger = !self.pairs.contains(pair, day);
        self.pairs.insert(pair, day);
        let arrival = RecordedArrival {
            at_us: post.at_us,
            author: post.author.to_owned(),
            kind,
            post: post.uri.to_owned(),
            target_post: target_post.map(str::to_owned),
            stranger,
        };
        let observed_us = post.at_us - self.started_us.unwrap_or(post.at_us);
        let state = self
            .targets
            .entry(target_key)
            .or_insert_with(|| TargetState {
                level_at_us: post.at_us,
                ..TargetState::default()
            });
        state.expire(self.now_us, config);
        if kind == ArrivalKind::Quote {
            self.recent_quotes.add(post.uri, post.at_us);
        }
        state.recent.push_back(arrival.clone());
        if state.recent.len() > RECENT_CAP {
            state.recent.pop_front();
        }
        let window_start = self.now_us - config.window_minutes * US_PER_MINUTE;
        if stranger {
            if post.at_us >= window_start {
                state.window.push_back((post.at_us, author_key));
                *state.counts.entry(author_key).or_default() += 1;
            } else {
                state.fold(post.at_us, config);
            }
        }

        if let Some(open) = self.open.get_mut(&target_key) {
            open.record(&arrival, author_key, config.max_recorded);
            if kind == ArrivalKind::Quote {
                self.quote_owner.insert(arrival.post.clone(), target_key);
            }
            if stranger {
                let (strangers, score, _) = state.score(post.at_us, observed_us, config);
                open.surge.peak_strangers = open.surge.peak_strangers.max(strangers);
                open.surge.peak_surprise = open.surge.peak_surprise.max(score);
            }
            return;
        }
        if !stranger {
            return;
        }
        let (strangers, score, rate) = state.score(post.at_us, observed_us, config);
        if strangers < config.min_strangers || score < config.min_surprise {
            return;
        }
        let onset_us = state.window.front().map_or(post.at_us, |&(at, _)| at);
        let warmup = self
            .started_us
            .is_some_and(|start| onset_us < start + config.warmup_days * US_PER_DAY);
        let mut open = OpenSurge {
            surge: Surge {
                target: target.to_owned(),
                onset_us,
                triggered_us: post.at_us,
                trigger_target_post: None,
                trigger_quote: None,
                last_us: post.at_us,
                warmup,
                baseline_per_hour: rate,
                trigger_strangers: strangers,
                trigger_surprise: score,
                peak_strangers: strangers,
                peak_surprise: score,
                arrivals: 0,
                strangers: 0,
                replies: 0,
                quotes: 0,
                mentions: 0,
                target_posts: Vec::new(),
                quote_posts: Vec::new(),
                recorded: Vec::new(),
                truncated: false,
            },
            last_stranger_us: post.at_us,
            authors: HashMap::new(),
            target_posts: HashMap::new(),
            quote_posts: HashMap::new(),
        };
        for earlier in &state.recent {
            open.record(earlier, key_of(&earlier.author), config.max_recorded);
        }
        for uri in open.quote_posts.keys() {
            self.quote_owner.insert(uri.clone(), target_key);
        }
        open.snapshot_trigger(&self.recent_quotes);
        self.open.insert(target_key, open);
    }
}

/// The accounts a post arrives at, each once: replies before quotes before
/// mentions. Self-replies, self-quotes and quotes of non-post records are not
/// arrivals.
fn arrivals_of<'a>(post: &PostEvent<'a>) -> Vec<(&'a str, ArrivalKind, Option<&'a str>)> {
    let mut out: Vec<(&'a str, ArrivalKind, Option<&'a str>)> = Vec::new();
    if let Some(parent) = post.parent {
        out.push((repo_of(parent), ArrivalKind::Reply, Some(parent)));
    }
    if let Some(quote) = post.quote
        && collection_of(quote) == "app.bsky.feed.post"
    {
        out.push((repo_of(quote), ArrivalKind::Quote, Some(quote)));
    }
    for mention in post.mentions {
        out.push((mention.as_str(), ArrivalKind::Mention, None));
    }
    let mut seen: Vec<&str> = Vec::new();
    out.retain(|(target, _, _)| {
        if *target == post.author || target.is_empty() || seen.contains(target) {
            return false;
        }
        seen.push(target);
        true
    });
    out
}

/// Returns the repository (account) of an AT URI.
///
/// # Examples
///
/// ```
/// use pileon_core::surge::repo_of;
/// assert_eq!(repo_of("at://did:plc:abc/app.bsky.feed.post/3x"), "did:plc:abc");
/// ```
pub fn repo_of(uri: &str) -> &str {
    let rest = uri.strip_prefix("at://").unwrap_or(uri);
    rest.split('/').next().unwrap_or_default()
}

fn collection_of(uri: &str) -> &str {
    let rest = uri.strip_prefix("at://").unwrap_or(uri);
    rest.split('/').nth(1).unwrap_or_default()
}

fn key_of(did: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    did.hash(&mut hasher);
    hasher.finish()
}

fn pair_key(a: u64, b: u64) -> u64 {
    let (low, high) = if a <= b { (a, b) } else { (b, a) };
    let mut hasher = DefaultHasher::new();
    (low, high).hash(&mut hasher);
    hasher.finish()
}

/// Picks a post's event time: the client's `createdAt` when it is plausible
/// (at most 72 hours before, or 5 minutes after, the witness time), else the
/// witness time. Witness time is late on relay catch-up days; `createdAt` is
/// client-controlled and sometimes wild.
///
/// # Examples
///
/// ```
/// use pileon_core::surge::event_time_us;
/// let witnessed = 1_792_195_200_000_000; // 2026-10-17T00:00:00Z
/// assert_eq!(event_time_us(witnessed, Some("2026-10-16T23:59:00Z")), witnessed - 60_000_000);
/// assert_eq!(event_time_us(witnessed, Some("2002-01-01T00:00:00Z")), witnessed);
/// assert_eq!(event_time_us(witnessed, None), witnessed);
/// ```
pub fn event_time_us(witnessed_us: i64, created_at: Option<&str>) -> i64 {
    let Some(created) = created_at.and_then(|c| c.parse::<jiff::Timestamp>().ok()) else {
        return witnessed_us;
    };
    let created_us = created.as_microsecond();
    let earliest = witnessed_us - 72 * US_PER_HOUR;
    let latest = witnessed_us + 5 * US_PER_MINUTE;
    if (earliest..=latest).contains(&created_us) {
        created_us
    } else {
        witnessed_us
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T0: i64 = 1_786_000_000_000_000;
    const TARGET: &str = "did:plc:target";

    fn post_uri(author: &str, n: u32) -> String {
        format!("at://{author}/app.bsky.feed.post/{n}")
    }

    fn reply(
        detector: &mut SurgeDetector,
        author: &str,
        minute: i64,
        target_post: &str,
    ) -> Vec<Surge> {
        let uri = post_uri(author, 1);
        detector.observe_post(&PostEvent {
            at_us: T0 + minute * US_PER_MINUTE,
            author,
            uri: &uri,
            parent: Some(target_post),
            quote: None,
            mentions: &[],
        })
    }

    fn quick_config() -> SurgeConfig {
        SurgeConfig {
            pairs_per_day: 10_000,
            warmup_days: 0,
            ..SurgeConfig::default()
        }
    }

    #[test]
    fn a_quiet_account_drawing_strangers_surges() {
        let mut detector = SurgeDetector::new(quick_config());
        let target_post = post_uri(TARGET, 7);
        for i in 0..12 {
            let closed = reply(&mut detector, &format!("did:plc:s{i}"), i * 2, &target_post);
            assert_eq!(closed, Vec::new());
        }
        assert_eq!(detector.load().1, 1, "one surge should be open");
        let closed = detector.finish();
        assert_eq!(closed.len(), 1);
        let Surge {
            target,
            onset_us,
            arrivals,
            strangers,
            replies,
            target_posts,
            trigger_strangers,
            ..
        } = &closed[0];
        assert_eq!(target, TARGET);
        assert_eq!(*onset_us, T0);
        assert_eq!(*trigger_strangers, 8);
        assert_eq!((*arrivals, *strangers, *replies), (12, 12, 12));
        assert_eq!(target_posts, &vec![(target_post.clone(), 12)]);
    }

    #[test]
    fn regulars_returning_together_do_not_surge() {
        let mut detector = SurgeDetector::new(quick_config());
        let target_post = post_uri(TARGET, 7);
        for i in 0..10 {
            reply(
                &mut detector,
                &format!("did:plc:r{i}"),
                i * 120,
                &target_post,
            );
        }
        for i in 0..10 {
            reply(
                &mut detector,
                &format!("did:plc:r{i}"),
                2 * 24 * 60 + i,
                &target_post,
            );
        }
        assert_eq!(detector.finish(), Vec::new());
    }

    #[test]
    fn a_steady_busy_baseline_absorbs_its_own_volume() {
        let mut busy = SurgeDetector::new(quick_config());
        let target_post = post_uri(TARGET, 7);
        let mut n = 0;
        let mut closed = Vec::new();
        for hour in 0..(14 * 24) {
            for k in 0..10 {
                n += 1;
                let author = format!("did:plc:b{n}");
                closed.extend(reply(&mut busy, &author, hour * 60 + k * 6, &target_post));
            }
        }
        closed.extend(busy.finish());
        let after_first_week = closed
            .iter()
            .filter(|s| s.onset_us > T0 + 7 * US_PER_DAY)
            .count();
        assert_eq!(
            after_first_week, 0,
            "a steady 10/hour target should stop surging"
        );
        let after_first_two_days = closed
            .iter()
            .filter(|s| s.onset_us > T0 + 2 * US_PER_DAY)
            .count();
        assert_eq!(
            after_first_two_days, 0,
            "the baseline should settle within days"
        );
    }

    #[test]
    fn surges_close_after_quiet_hours() {
        let mut detector = SurgeDetector::new(quick_config());
        let target_post = post_uri(TARGET, 7);
        for i in 0..10 {
            reply(&mut detector, &format!("did:plc:s{i}"), i, &target_post);
        }
        let closed = reply(
            &mut detector,
            "did:plc:late",
            7 * 60,
            &post_uri("did:plc:other", 1),
        );
        assert_eq!(closed.len(), 1);
        assert_eq!(closed[0].strangers, 10);
    }

    #[test]
    fn quote_posts_collect_engagement_as_amplifier_candidates() {
        let mut detector = SurgeDetector::new(quick_config());
        let target_post = post_uri(TARGET, 7);
        let dunk = post_uri("did:plc:amp", 99);
        detector.observe_post(&PostEvent {
            at_us: T0,
            author: "did:plc:amp",
            uri: &dunk,
            parent: None,
            quote: Some(&target_post),
            mentions: &[],
        });
        for i in 0..10 {
            reply(&mut detector, &format!("did:plc:s{i}"), 1 + i, &target_post);
        }
        detector.observe_engagement(&dunk, EngagementKind::Like, T0 + 20 * US_PER_MINUTE);
        detector.observe_engagement(&dunk, EngagementKind::Repost, T0 + 21 * US_PER_MINUTE);
        let replier_uri = post_uri("did:plc:fan", 5);
        detector.observe_post(&PostEvent {
            at_us: T0 + 22 * US_PER_MINUTE,
            author: "did:plc:fan",
            uri: &replier_uri,
            parent: Some(&dunk),
            quote: None,
            mentions: &[],
        });
        let closed = detector.finish();
        assert_eq!(closed.len(), 1);
        let quote = &closed[0].quote_posts[0];
        assert_eq!(quote.author, "did:plc:amp");
        assert_eq!(
            quote.engagement,
            Engagement {
                replies: 1,
                quotes: 0,
                reposts: 1,
                likes: 1
            }
        );
        assert_eq!(closed[0].quotes, 1);
    }

    fn quote(detector: &mut SurgeDetector, author: &str, minute: i64, quoted: &str) -> String {
        let uri = post_uri(author, 1);
        detector.observe_post(&PostEvent {
            at_us: T0 + minute * US_PER_MINUTE,
            author,
            uri: &uri,
            parent: None,
            quote: Some(quoted),
            mentions: &[],
        });
        uri
    }

    #[test]
    fn trigger_keeps_what_was_visible_when_the_surge_opened() {
        let mut detector = SurgeDetector::new(quick_config());
        let first_post = post_uri(TARGET, 7);
        let later_post = post_uri(TARGET, 8);
        let early = quote(&mut detector, "did:plc:early", 0, &first_post);
        detector.observe_engagement(&early, EngagementKind::Like, T0 + 30_000_000);
        for i in 0..7 {
            reply(&mut detector, &format!("did:plc:s{i}"), 1 + i, &first_post);
        }
        let late = quote(&mut detector, "did:plc:late", 10, &later_post);
        for i in 0..5 {
            detector.observe_engagement(&late, EngagementKind::Like, T0 + (11 + i) * US_PER_MINUTE);
        }
        for i in 0..20 {
            reply(&mut detector, &format!("did:plc:r{i}"), 20 + i, &later_post);
        }
        let closed = detector.finish();
        assert_eq!(closed.len(), 1);
        let surge = &closed[0];
        assert_eq!(surge.triggered_us, T0 + 7 * US_PER_MINUTE);
        assert_eq!(
            surge.trigger_target_post.as_deref(),
            Some(first_post.as_str())
        );
        let trigger_quote = surge.trigger_quote.as_ref().expect("a quote was visible");
        assert_eq!(trigger_quote.uri, early);
        assert_eq!(trigger_quote.engagement.likes, 1);
        assert_eq!(surge.quote_posts[0].uri, late);
        assert_eq!(surge.target_posts[0].0, later_post);
    }

    #[test]
    fn recent_quotes_forget_engagement_past_the_lookback() {
        let mut quotes = RecentQuotes::default();
        quotes.add("at://a/app.bsky.feed.post/1", T0);
        quotes.add("at://b/app.bsky.feed.post/1", T0 + US_PER_HOUR);
        quotes.add("at://a/app.bsky.feed.post/1", T0 + 2 * US_PER_HOUR);
        quotes.expire(T0 + 1);
        assert!(
            !quotes
                .engagement
                .contains_key("at://a/app.bsky.feed.post/1")
        );
        assert!(
            quotes
                .engagement
                .contains_key("at://b/app.bsky.feed.post/1")
        );
        assert_eq!(quotes.order.len(), 1);
    }

    #[test]
    fn arrivals_skip_self_and_dedupe_targets() {
        let mentions = vec![
            "did:plc:t".to_owned(),
            "did:plc:m".to_owned(),
            "did:plc:a".to_owned(),
        ];
        let parent = post_uri("did:plc:t", 1);
        let feed = "at://did:plc:x/app.bsky.feed.generator/f".to_owned();
        let post = PostEvent {
            at_us: T0,
            author: "did:plc:a",
            uri: "at://did:plc:a/app.bsky.feed.post/1",
            parent: Some(&parent),
            quote: Some(&feed),
            mentions: &mentions,
        };
        let found: Vec<(&str, ArrivalKind)> = arrivals_of(&post)
            .into_iter()
            .map(|(target, kind, _)| (target, kind))
            .collect();
        assert_eq!(
            found,
            vec![
                ("did:plc:t", ArrivalKind::Reply),
                ("did:plc:m", ArrivalKind::Mention)
            ]
        );
    }

    #[test]
    fn warmup_flags_early_surges() {
        let mut detector = SurgeDetector::new(SurgeConfig {
            pairs_per_day: 10_000,
            ..SurgeConfig::default()
        });
        let target_post = post_uri(TARGET, 7);
        for i in 0..10 {
            reply(&mut detector, &format!("did:plc:s{i}"), i, &target_post);
        }
        assert!(detector.finish()[0].warmup);
    }
}

"""Relative incident features: everything measured against the target's normal.

One row per incident. Absolute counts mislead (a 100k-follower account gets
more strangers in an hour than a 300-follower account gets in a year), so
every volume is a ratio to the target's own baseline: their posts from just
before the incident, matched by kind (reply vs top-level). Ratios use add-one
smoothing so a zero median does not divide by zero, and a missing input
yields `None` rather than a made-up number.

Follow relationships are dated by their record's TID, so "follows the quoter"
means "followed the quoter before the incident", not "followed by the time we
looked".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pileon_lab.enrich import BASELINE_POSTS, arrivals, incident_posts, usable
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import (
    PostCounts,
    Relationship,
    post_counts,
    post_uris_before,
    profile_counts,
    relationships,
)
from pileon_lab.study import Incident, Response, load_incidents
from pileon_lab.tid import TidError, rkey_of, time_of

if TYPE_CHECKING:
    from collections.abc import Iterable

MIN_MATCHED_BASELINE = 10
"""Below this many kind-matched baseline posts, fall back to all posts."""

PRE_WINDOW_CHANNELS = (
    "replies",
    "quotes",
    "replies_c13n",
    "quotes_c13n",
    "ss_replies",
    "ss_quotes",
)


@dataclass(frozen=True, slots=True)
class Baseline:
    posts: int
    matched_kind: bool
    median_replies: float
    p90_replies: float
    max_replies: int
    median_quotes: float
    median_mix: float


def engagement_mix(post: PostCounts) -> float:
    """Return (replies + quotes + 1) / (likes + reposts + 1).

    Viral posts gain likes and reposts roughly in step with replies; posts
    being dunked on draw replies and quotes far out of proportion to likes.
    """
    return (post.replies + post.quotes + 1) / (post.likes + post.reposts + 1)


def baseline(history: list[PostCounts], *, is_reply: bool | None) -> Baseline | None:
    """Summarize the target's prior posts, preferring the incident post's kind.

    `is_reply=None` (the incident post is gone, so its kind is unknown) uses
    every prior post.
    """
    if not history:
        return None
    matched = [p for p in history if p.is_reply == is_reply]
    use_matched = is_reply is not None and len(matched) >= MIN_MATCHED_BASELINE
    posts = matched if use_matched else history
    replies = sorted(p.replies for p in posts)
    return Baseline(
        posts=len(posts),
        matched_kind=use_matched,
        median_replies=statistics.median(replies),
        p90_replies=replies[min(len(replies) - 1, math.floor(0.9 * len(replies)))],
        max_replies=replies[-1],
        median_quotes=statistics.median(p.quotes for p in posts),
        median_mix=statistics.median(engagement_mix(p) for p in posts),
    )


def ratio(numerator: float, denominator: float) -> float:
    """Return (numerator + 1) / (denominator + 1)."""
    return (numerator + 1) / (denominator + 1)


def followed_before(uri: str | None, moment: datetime) -> bool | None:
    """Whether the follow record at `uri` predates `moment` (`None`: no follow)."""
    if uri is None:
        return None
    try:
        return time_of(rkey_of(uri)) < moment
    except TidError:
        return None


def first_arrivals(responses: Iterable[Response]) -> dict[str, datetime]:
    """Return each author's earliest response time."""
    first: dict[str, datetime] = {}
    for response in responses:
        if response.author not in first or response.at < first[response.author]:
            first[response.author] = response.at
    return first


def share(count: int, total: int) -> float | None:
    return count / total if total else None


@dataclass(frozen=True, slots=True)
class Composition:
    arrivals: int
    resolved: int
    follow_target: float | None
    target_follows: float | None
    follow_amplifier: float | None
    follow_amplifier_after: float | None
    strangers_to_both: float | None
    within_15m: float | None
    within_1h: float | None
    contempt_share_amplifier_followers: float | None
    contempt_share_target_followers: float | None
    contempt_share_strangers: float | None
    group_labeled: dict[str, int]
    group_contempt: dict[str, int]


GROUPS = ("amplifier", "target", "stranger")


def composition(
    incident: Incident,
    to_target: dict[str, Relationship],
    to_amplifier: dict[str, Relationship],
) -> Composition:
    """Describe who showed up: target allies, the quoter's audience, or neither."""
    first = first_arrivals(incident.responses)
    anchor = incident.anchor
    resolved = follow_target = target_follows = 0
    follow_amp = follow_amp_after = strangers = 0
    group_of: dict[str, str] = {}
    for author in first:
        target_rel = to_target.get(author)
        amp_rel = to_amplifier.get(author)
        if target_rel is None or amp_rel is None or not target_rel.resolved:
            continue
        resolved += 1
        fan = followed_before(target_rel.follows_actor, anchor) is True
        friend = target_rel.actor_follows is not None
        audience = followed_before(amp_rel.follows_actor, anchor)
        follow_target += fan
        target_follows += friend
        follow_amp += audience is True
        follow_amp_after += audience is False
        if audience is True and not fan:
            group_of[author] = "amplifier"
        elif fan or friend:
            group_of[author] = "target"
        else:
            group_of[author] = "stranger"
            strangers += 1
    within = [at - anchor for at in first.values()]

    group_labeled = dict.fromkeys(GROUPS, 0)
    group_contempt = dict.fromkeys(GROUPS, 0)
    for response in incident.responses:
        group = group_of.get(response.author)
        if group is None or response.valence is None:
            continue
        group_labeled[group] += 1
        group_contempt[group] += response.contempt

    def contempt_share(group: str) -> float | None:
        return share(group_contempt[group], group_labeled[group])

    return Composition(
        arrivals=len(first),
        resolved=resolved,
        follow_target=share(follow_target, resolved),
        target_follows=share(target_follows, resolved),
        follow_amplifier=share(follow_amp, resolved),
        follow_amplifier_after=share(follow_amp_after, resolved),
        strangers_to_both=share(strangers, resolved),
        within_15m=share(sum(d <= timedelta(minutes=15) for d in within), len(within)),
        within_1h=share(sum(d <= timedelta(hours=1) for d in within), len(within)),
        contempt_share_amplifier_followers=contempt_share("amplifier"),
        contempt_share_target_followers=contempt_share("target"),
        contempt_share_strangers=contempt_share("stranger"),
        group_labeled=group_labeled,
        group_contempt=group_contempt,
    )


@dataclass(frozen=True, slots=True)
class Lifts:
    """The incident post's engagement relative to the target's baseline."""

    reply_lift_median: float | None
    reply_lift_p90: float | None
    reply_vs_record: float | None
    mix_shift: float | None


NO_LIFTS = Lifts(None, None, None, None)


def lifts(post: PostCounts | None, base: Baseline | None) -> Lifts:
    if post is None or base is None:
        return NO_LIFTS
    return Lifts(
        reply_lift_median=ratio(post.replies, base.median_replies),
        reply_lift_p90=ratio(post.replies, base.p90_replies),
        reply_vs_record=ratio(post.replies, base.max_replies),
        mix_shift=engagement_mix(post) / base.median_mix,
    )


@dataclass(frozen=True, slots=True)
class Row:
    incident_id: str
    source: str
    anchor: str
    target_followers: int | None
    amplifier_followers: int | None
    follower_ratio_log10: float | None
    amplifier_follows_target: bool | None
    target_follows_amplifier: bool | None
    target_post_found: bool
    target_post_is_reply: bool | None
    replies: int | None
    quotes: int | None
    likes: int | None
    reposts: int | None
    baseline_posts: int | None
    baseline_matched_kind: bool | None
    baseline_median_replies: float | None
    baseline_max_replies: int | None
    reply_lift_median: float | None
    reply_lift_p90: float | None
    reply_vs_record: float | None
    mix: float | None
    mix_shift: float | None
    pre_arrivals_48h: int | None
    window_lift_median: float | None
    burst_ratio: float | None
    arrivals: int
    arrivals_resolved: int
    follow_target: float | None
    target_follows: float | None
    follow_amplifier: float | None
    follow_amplifier_after: float | None
    strangers_to_both: float | None
    within_15m: float | None
    within_1h: float | None
    contempt_share_amplifier_followers: float | None
    contempt_share_target_followers: float | None
    contempt_share_strangers: float | None
    group_labeled: dict[str, int]
    group_contempt: dict[str, int]
    labeled: int
    contempt: int
    critical_or_worse: int
    supportive: int
    contempt_share: float | None


def build_row(  # noqa: PLR0913 - one row joins every source
    incident: Incident,
    *,
    target_post: PostCounts | None,
    history: list[PostCounts],
    ally: Relationship | None,
    to_target: dict[str, Relationship],
    to_amplifier: dict[str, Relationship],
    amplifier_followers: int | None,
    pre_arrivals: int | None,
) -> Row:
    """Join one incident's facts into a feature row."""
    is_reply = target_post.is_reply if target_post else None
    base = baseline(history, is_reply=is_reply)
    lift = lifts(target_post, base)
    labeled = [r for r in incident.responses if r.valence is not None]
    contempt = sum(r.contempt for r in labeled)
    comp = composition(incident, to_target, to_amplifier)
    window = len(first_arrivals(incident.responses))
    target_followers = incident.target_followers

    return Row(
        incident_id=incident.incident_id,
        source=incident.source,
        anchor=incident.anchor.isoformat(),
        target_followers=target_followers,
        amplifier_followers=amplifier_followers,
        follower_ratio_log10=(
            math.log10((amplifier_followers + 1) / (target_followers + 1))
            if amplifier_followers is not None and target_followers is not None
            else None
        ),
        amplifier_follows_target=(
            followed_before(ally.actor_follows, incident.anchor) is True
            if ally and ally.resolved
            else None
        ),
        target_follows_amplifier=(
            followed_before(ally.follows_actor, incident.anchor) is True
            if ally and ally.resolved
            else None
        ),
        target_post_found=target_post is not None,
        target_post_is_reply=is_reply,
        replies=target_post.replies if target_post else None,
        quotes=target_post.quotes if target_post else None,
        likes=target_post.likes if target_post else None,
        reposts=target_post.reposts if target_post else None,
        baseline_posts=base.posts if base else None,
        baseline_matched_kind=base.matched_kind if base else None,
        baseline_median_replies=base.median_replies if base else None,
        baseline_max_replies=base.max_replies if base else None,
        reply_lift_median=lift.reply_lift_median,
        reply_lift_p90=lift.reply_lift_p90,
        reply_vs_record=lift.reply_vs_record,
        mix=engagement_mix(target_post) if target_post else None,
        mix_shift=lift.mix_shift,
        pre_arrivals_48h=pre_arrivals,
        window_lift_median=(
            ratio(window, base.median_replies + base.median_quotes) if base else None
        ),
        burst_ratio=ratio(window, pre_arrivals) if pre_arrivals is not None else None,
        arrivals=comp.arrivals,
        arrivals_resolved=comp.resolved,
        follow_target=comp.follow_target,
        target_follows=comp.target_follows,
        follow_amplifier=comp.follow_amplifier,
        follow_amplifier_after=comp.follow_amplifier_after,
        strangers_to_both=comp.strangers_to_both,
        within_15m=comp.within_15m,
        within_1h=comp.within_1h,
        contempt_share_amplifier_followers=comp.contempt_share_amplifier_followers,
        contempt_share_target_followers=comp.contempt_share_target_followers,
        contempt_share_strangers=comp.contempt_share_strangers,
        group_labeled=comp.group_labeled,
        group_contempt=comp.group_contempt,
        labeled=len(labeled),
        contempt=contempt,
        critical_or_worse=sum(r.critical_or_worse for r in labeled),
        supportive=sum(r.valence == "supportive" for r in labeled),
        contempt_share=share(contempt, len(labeled)),
    )


def pre_window_arrivals(db: Path) -> dict[str, int]:
    """Distinct responders in the 48 h before each cluster's anchor, per the study."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        totals: dict[str, int] = {}
        for cluster_id, channel, before in conn.execute(
            "SELECT cluster_id, channel, before_unique_authors FROM measurements"
        ):
            if channel in PRE_WINDOW_CHANNELS:
                totals[cluster_id] = totals.get(cluster_id, 0) + before
        return totals
    finally:
        conn.close()


async def assemble(
    incidents: list[Incident], fetcher: Fetcher, pre: dict[str, int]
) -> list[Row]:
    """Build every feature row from cached facts."""
    targets = await post_counts(fetcher, incident_posts(incidents))
    amplifiers = sorted({i.amplifier_did for i in incidents})
    followers = await profile_counts(fetcher, amplifiers)
    allies: dict[str, dict[str, Relationship]] = {}
    audiences: dict[str, dict[str, Relationship]] = {}
    for amplifier in amplifiers:
        mine = [i for i in incidents if i.amplifier_did == amplifier]
        allies[amplifier] = await relationships(
            fetcher, amplifier, sorted({i.target_did for i in mine})
        )
        audiences[amplifier] = await relationships(
            fetcher, amplifier, sorted({a for i in mine for a in arrivals(i)})
        )
    rows = []
    for incident in incidents:
        uris = await post_uris_before(
            fetcher, incident.target_did, incident.anchor, limit=BASELINE_POSTS
        )
        history = (
            [
                p
                for p in (await post_counts(fetcher, uris)).values()
                if p.uri != incident.target_post_uri and p.author == incident.target_did
            ]
            if uris
            else []
        )
        to_target = await relationships(
            fetcher, incident.target_did, arrivals(incident)
        )
        rows.append(
            build_row(
                incident,
                target_post=targets.get(incident.target_post_uri),
                history=history,
                ally=allies[incident.amplifier_did].get(incident.target_did),
                to_target=to_target,
                to_amplifier=audiences[incident.amplifier_did],
                amplifier_followers=followers.get(incident.amplifier_did),
                pre_arrivals=pre.get(incident.incident_id),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write relative incident features")
    parser.add_argument("--study-db", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    incidents = usable(load_incidents(args.study_db))
    pre = pre_window_arrivals(args.study_db)

    async def run() -> list[Row]:
        fetcher = Fetcher(Cache(args.cache), offline=True)
        try:
            return await assemble(incidents, fetcher, pre)
        finally:
            await fetcher.close()
            print(f"cache: {fetcher.stats}", file=sys.stderr)

    rows = asyncio.run(run())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as out:
        for row in rows:
            out.write(json.dumps(asdict(row)) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

from datetime import UTC, datetime, timedelta

import pytest

from pileon_lab.features import (
    baseline,
    build_row,
    composition,
    engagement_mix,
    followed_before,
    lifts,
    like_reply,
    like_signals,
)
from pileon_lab.sources import UNRESOLVED, Likers, PostCounts, Relationship
from pileon_lab.study import Incident, Response
from pileon_lab.tid import for_time

ANCHOR = datetime(2026, 3, 30, 18, 0, tzinfo=UTC)
TARGET = "did:plc:target"
AMP = "did:plc:amp"


def post(replies, *, likes=10, quotes=0, reposts=0, is_reply=False, uri="at://p"):
    return PostCounts(uri, TARGET, "", is_reply, replies, likes, quotes, reposts)


def rel(*, actor_follows=None, follows_actor=None):
    return Relationship(
        resolved=True, actor_follows=actor_follows, follows_actor=follows_actor
    )


def follow_uri(who, moment):
    return f"at://{who}/app.bsky.graph.follow/{for_time(moment)}"


def incident(responses):
    return Incident(
        incident_id="i1",
        source="qp",
        target_did=TARGET,
        target_type="individual",
        target_followers=300,
        anchor=ANCHOR,
        amplifier_did=AMP,
        target_post_uri="at://did:plc:target/app.bsky.feed.post/x",
        dunk_uris=("at://did:plc:amp/app.bsky.feed.post/d",),
        responses=tuple(responses),
    )


def reply(author, minutes, valence="neutral", direction=None):
    return Response(
        f"at://{author}/app.bsky.feed.post/{minutes}",
        author,
        ANCHOR + timedelta(minutes=minutes),
        "replies",
        valence,
        direction,
    )


def test_engagement_mix_rises_when_replies_outrun_likes():
    viral = post(20, likes=400, reposts=100)
    dunked = post(200, likes=15, quotes=40)
    assert engagement_mix(dunked) > 10 * engagement_mix(viral)


def test_baseline_prefers_matching_kind_and_falls_back():
    history = [post(i, is_reply=True) for i in range(12)] + [post(50), post(70)]
    matched = baseline(history, is_reply=True)
    assert matched is not None
    assert matched.matched_kind
    assert matched.posts == 12
    assert matched.max_replies == 11
    fallback = baseline(history, is_reply=False)
    assert fallback is not None
    assert not fallback.matched_kind
    assert fallback.posts == 14
    unknown = baseline(history, is_reply=None)
    assert unknown is not None
    assert not unknown.matched_kind
    assert baseline([], is_reply=False) is None


def test_lifts_are_smoothed_ratios_to_the_baseline():
    history = [post(0) for _ in range(20)]
    base = baseline(history, is_reply=False)
    lift = lifts(post(99), base)
    assert lift.reply_lift_median == pytest.approx(100.0)
    assert lift.reply_vs_record == pytest.approx(100.0)
    assert lifts(None, base).reply_lift_median is None


def test_followed_before_dates_the_follow_by_its_tid():
    assert followed_before(follow_uri("a", ANCHOR - timedelta(days=30)), ANCHOR) is True
    assert (
        followed_before(follow_uri("a", ANCHOR + timedelta(minutes=5)), ANCHOR) is False
    )
    assert followed_before(None, ANCHOR) is None
    assert followed_before("at://a/app.bsky.graph.follow/notatid", ANCHOR) is None


def test_composition_groups_arrivals_and_scores_each_group():
    long_ago = ANCHOR - timedelta(days=90)
    responses = [
        reply("fan", 2, "supportive"),
        reply("audience", 3, "hostile", "author"),
        reply("audience", 4, "hostile", "author"),
        reply("stranger", 30, "critical"),
        reply("recruit", 120, "hostile", "author"),
        reply("ghost", 5),
    ]
    to_target = {
        "fan": rel(follows_actor=follow_uri("fan", long_ago)),
        "audience": rel(),
        "stranger": rel(),
        "recruit": rel(),
        "ghost": UNRESOLVED,
    }
    to_amp = {
        "fan": rel(),
        "audience": rel(follows_actor=follow_uri("audience", long_ago)),
        "stranger": rel(),
        "recruit": rel(
            follows_actor=follow_uri("recruit", ANCHOR + timedelta(hours=1))
        ),
        "ghost": UNRESOLVED,
    }
    comp = composition(incident(responses), to_target, to_amp)
    assert comp.arrivals == 5
    assert comp.resolved == 4
    assert comp.follow_target == pytest.approx(1 / 4)
    assert comp.follow_amplifier == pytest.approx(1 / 4)
    assert comp.follow_amplifier_after == pytest.approx(1 / 4)
    assert comp.strangers_to_both == pytest.approx(2 / 4)
    assert comp.within_15m == pytest.approx(3 / 5)
    assert comp.contempt_share_amplifier_followers == pytest.approx(1.0)
    assert comp.contempt_share_target_followers == pytest.approx(0.0)
    assert comp.contempt_share_strangers == pytest.approx(1 / 2)
    assert comp.group_labeled == {"amplifier": 2, "target": 1, "stranger": 2}
    assert comp.group_contempt == {"amplifier": 2, "target": 0, "stranger": 1}


def test_deleted_target_post_still_gets_a_window_lift():
    responses = [reply(f"a{i}", i, "hostile", "author") for i in range(30)]
    history = [post(1) for _ in range(20)]
    row = build_row(
        incident(responses),
        target_post=None,
        history=history,
        ally=rel(actor_follows=follow_uri(AMP, ANCHOR - timedelta(days=1))),
        to_target={},
        to_amplifier={},
        amplifier_followers=100_000,
        pre_arrivals=0,
        likes=like_signals(
            incident(responses),
            target_post=None,
            response_counts={},
            dunk_counts=[],
            liked=None,
        ),
    )
    assert not row.target_post_found
    assert row.reply_lift_median is None
    assert row.window_lift_median == pytest.approx(31 / 2)
    assert row.burst_ratio == pytest.approx(31.0)
    assert row.amplifier_follows_target is True
    assert row.target_follows_amplifier is False
    assert row.contempt == 30
    assert row.contempt_share == pytest.approx(1.0)
    assert row.follower_ratio_log10 == pytest.approx(2.52, abs=0.01)

    assert row.like_reply_ratio is None
    assert row.arrivals_liked_target is None
    assert row.dunk_ratio is None


def test_like_reply_falls_when_a_post_is_ratioed():
    assert like_reply(post(200, likes=15)) < like_reply(post(5, likes=60)) / 20


def test_like_signals_measure_approval_on_both_sides():
    responses = [
        reply("attacker", 3, "hostile", "author"),
        reply("defender", 5, "supportive"),
        reply("bystander", 9, "neutral"),
    ]
    inc = incident(responses)
    target = post(30, likes=10, uri=inc.target_post_uri)
    counts = {
        responses[0].uri: post(0, likes=50, uri=responses[0].uri),
        responses[1].uri: post(0, likes=3, uri=responses[1].uri),
        responses[2].uri: post(0, likes=0, uri=responses[2].uri),
    }
    dunk = post(0, likes=200, uri=inc.dunk_uris[0])
    liked = Likers(
        complete=True,
        liked_at={
            "defender": (ANCHOR - timedelta(hours=2)).isoformat(),
            "bystander": (ANCHOR + timedelta(minutes=20)).isoformat(),
        },
    )
    signals = like_signals(
        inc, target_post=target, response_counts=counts, dunk_counts=[dunk], liked=liked
    )
    assert signals.arrivals_liked_target == pytest.approx(2 / 3)
    assert signals.arrivals_liked_before == pytest.approx(1 / 3)
    assert signals.top_response_ratio == pytest.approx(51 / 11)
    assert signals.responses_outliking_target == pytest.approx(1 / 3)
    assert signals.dunk_ratio == pytest.approx(201 / 11)
    assert signals.contempt_like_lift == pytest.approx(51 / 2.5)
    assert signals.like_group_labeled == {"liked": 2, "no_like": 1}
    assert signals.like_group_contempt == {"liked": 0, "no_like": 1}

    truncated = like_signals(
        inc,
        target_post=target,
        response_counts=counts,
        dunk_counts=[dunk],
        liked=Likers(complete=False, liked_at={"defender": ""}),
    )
    assert truncated.arrivals_liked_target is None
    assert truncated.like_group_labeled == {"liked": 0, "no_like": 0}

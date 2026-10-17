from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pileon_lab.nuke import Evidence, Policy, Tally, policy
from pileon_lab.sources import Likers, Relationship
from pileon_lab.study import Incident, Response
from pileon_lab.tid import for_time

ANCHOR = datetime(2026, 3, 30, 18, 0, tzinfo=UTC)
TARGET = "did:plc:target"
AMP = "did:plc:amp"


def follow(at):
    return f"at://did:plc:x/app.bsky.graph.follow/{for_time(at)}"


def reply(author, minutes, valence, direction="author"):
    return Response(
        uri=f"at://{author}/app.bsky.feed.post/{minutes}",
        author=author,
        at=ANCHOR + timedelta(minutes=minutes),
        channel="replies",
        valence=valence,
        direction=direction,
    )


INCIDENT = Incident(
    incident_id="i",
    source="test",
    target_did=TARGET,
    target_type="person",
    target_followers=100,
    anchor=ANCHOR,
    amplifier_did=AMP,
    target_post_uri=f"at://{TARGET}/app.bsky.feed.post/t",
    dunk_uris=(f"at://{AMP}/app.bsky.feed.post/d",),
    responses=(
        reply("did:plc:early", 5, "hostile"),
        reply("did:plc:liker", 30, "abusive"),
        reply("did:plc:fan", 40, "supportive"),
        reply("did:plc:late", 90, "hostile"),
        reply("did:plc:aside", 50, "hostile", direction="third_party"),
    ),
)

EVIDENCE = Evidence(
    dunk_likers=Likers(
        complete=True,
        liked_at={
            "did:plc:liker": (ANCHOR + timedelta(minutes=2)).isoformat(),
            "did:plc:fan": (ANCHOR + timedelta(minutes=3)).isoformat(),
            "did:plc:late": (ANCHOR + timedelta(minutes=80)).isoformat(),
            TARGET: ANCHOR.isoformat(),
        },
    ),
    to_target={
        "did:plc:fan": Relationship(
            resolved=True, actor_follows=None, follows_actor=follow(ANCHOR)
        ),
    },
    to_amplifier={
        "did:plc:early": Relationship(
            resolved=True,
            actor_follows=None,
            follows_actor=follow(ANCHOR - timedelta(days=30)),
        ),
        "did:plc:late": Relationship(
            resolved=True,
            actor_follows=None,
            follows_actor=follow(ANCHOR + timedelta(hours=2)),
        ),
    },
    amplifier_followers=5000,
)


AT = ANCHOR + timedelta(minutes=15)


def blocked(yield_):
    return policy(INCIDENT, EVIDENCE, yield_, AT).blocked


def test_likers_are_those_who_liked_by_the_cut_never_the_target():
    assert blocked("likers") == {"did:plc:liker", "did:plc:fan"}
    assert blocked("likers-safe") == {"did:plc:liker"}


def test_audience_counts_only_follows_dated_before_the_cut():
    assert blocked("audience") == {"did:plc:early"}
    assert blocked("audience+likers") == {"did:plc:early", "did:plc:liker"}
    assert policy(INCIDENT, EVIDENCE, "audience+likers", AT).size == 5001


def test_followers_gate_lets_only_prior_followers_through():
    assert blocked("gate-followers") == {
        "did:plc:early",
        "did:plc:liker",
        "did:plc:late",
        "did:plc:aside",
    }
    assert policy(INCIDENT, EVIDENCE, "gate-followers", AT).size is None


def test_tally_scores_only_responses_after_the_cut():
    tally = Tally()
    defense = Policy(frozenset({"did:plc:liker", "did:plc:fan", "did:plc:early"}), 3)
    tally.add(INCIDENT, defense, AT)
    assert (tally.harm, tally.harm_prevented) == (2, 1)
    assert (tally.supportive, tally.supportive_blocked) == (1, 1)
    assert (tally.other, tally.other_blocked) == (1, 0)
    assert tally.sizes == [3]


def test_strike_stops_only_repeat_harm():
    repeat = (*INCIDENT.responses, reply("did:plc:liker", 70, "hostile"))
    tally = Tally()
    tally.add(
        replace(INCIDENT, responses=repeat),
        policy(INCIDENT, EVIDENCE, "strike", AT),
        AT,
    )
    assert (tally.harm, tally.harm_prevented) == (3, 1)

from collections import Counter

import pytest

from pileon_lab.codebook import (
    direction_prompt,
    parse_direction,
    parse_label,
    valence_prompt,
)
from pileon_lab.evalset import hostile_scores, split_of
from pileon_lab.label import choose
from pileon_lab.surges import parse_surge, sample, size_bucket, to_incident

ONSET = 1_786_000_000_000_000
TARGET = "did:plc:target"


def surge_line(strangers=20, *, warmup=False, quotes=True, onset=ONSET, target=TARGET):
    return {
        "target": target,
        "onset_us": onset,
        "triggered_us": onset + 60_000_000,
        "last_us": onset + 3_600_000_000,
        "warmup": warmup,
        "baseline_per_hour": 0.2,
        "trigger_strangers": 8,
        "trigger_surprise": 7.0,
        "peak_strangers": strangers,
        "peak_surprise": 12.0,
        "arrivals": strangers,
        "strangers": strangers,
        "replies": 3,
        "quotes": 1,
        "mentions": 0,
        "target_posts": [[f"at://{target}/app.bsky.feed.post/p1", 3]],
        "quote_posts": (
            [
                {
                    "uri": "at://did:plc:amp/app.bsky.feed.post/q1",
                    "author": "did:plc:amp",
                    "at_us": onset - 60_000_000,
                    "engagement": {
                        "replies": 2,
                        "quotes": 1,
                        "reposts": 4,
                        "likes": 30,
                    },
                }
            ]
            if quotes
            else []
        ),
        "recorded": [
            [-60, "did:plc:amp", "q", "q1", "p1", True],
            [-30, "did:plc:early", "r", "e1", "p1", True],
            [0, "did:plc:a", "r", "a1", "p1", True],
            [45, "did:plc:b", "r", "b1", "p1", False],
            [90, TARGET, "r", "t1", "p1", False],
            [120, "did:plc:c", "m", "c1", "", True],
        ],
        "truncated": False,
    }


def test_parse_surge_rebuilds_uris_and_engagement():
    surge = parse_surge(surge_line())
    assert surge.surge_id == f"{TARGET}:{ONSET}"
    assert surge.quote_posts[0].engagement == 37
    assert surge.recorded[2].uri() == "at://did:plc:a/app.bsky.feed.post/a1"
    assert surge.recorded[0].stranger


def test_to_incident_keeps_arrivals_from_onset_and_drops_target_and_dunk():
    incident = to_incident(parse_surge(surge_line()))
    assert incident.amplifier_did == "did:plc:amp"
    assert incident.dunk_uris == ("at://did:plc:amp/app.bsky.feed.post/q1",)
    assert incident.target_post_uri == f"at://{TARGET}/app.bsky.feed.post/p1"
    assert [r.author for r in incident.responses] == [
        "did:plc:a",
        "did:plc:b",
        "did:plc:c",
    ]
    assert [r.channel for r in incident.responses] == ["replies", "replies", "mentions"]
    no_quote = to_incident(parse_surge(surge_line(quotes=False)))
    assert no_quote.amplifier_did == ""
    assert no_quote.dunk_uris == ()


def test_sample_balances_size_buckets_and_skips_warmup():
    surges = [
        parse_surge(surge_line(strangers, onset=ONSET + i, target=f"did:plc:t{i}"))
        for i, strangers in enumerate([9] * 50 + [40] * 50 + [600] * 3)
    ]
    surges.append(parse_surge(surge_line(20, warmup=True, target="did:plc:w")))
    chosen = sample(surges, size=9, seed=1)
    assert Counter(size_bucket(s.strangers) for s in chosen) == Counter(
        {3: 3, 5: 3, 9: 3}
    )
    assert all(not s.warmup for s in chosen)
    assert sample(surges, size=9, seed=1) == chosen


def test_choose_is_seeded_and_limited_to_replies_and_quotes_on_target_posts():
    surge = parse_surge(surge_line())
    picks = choose(surge, per_surge=10, seed=3)
    assert {uri for uri, _ in picks} == {
        "at://did:plc:a/app.bsky.feed.post/a1",
        "at://did:plc:b/app.bsky.feed.post/b1",
    }
    assert all(target == f"at://{TARGET}/app.bsky.feed.post/p1" for _, target in picks)
    assert choose(surge, per_surge=1, seed=3) == choose(surge, per_surge=1, seed=3)


def test_prompts_carry_context_and_replies_parse():
    prompt = valence_prompt(
        "t.bsky.social", "you clown", target_post="hello", framing="lol"
    )
    assert "The prominent account's framing: lol" in prompt
    assert "What @t.bsky.social (the TARGET) said: hello" in prompt
    assert prompt.rstrip().endswith("POST: you clown")
    direction = direction_prompt("t.bsky.social", "die", target_post="")
    assert "(unavailable)" in direction
    assert '{"direction": "author"|"third_party"|"unclear"}' in direction
    assert parse_label('```json\n{"label": "hostile", "confidence": 0.9}\n```') == (
        "hostile",
        0.9,
    )
    assert parse_label("no json here") == ("parse_error", 0.0)
    assert parse_direction('{"direction": "Author"}') == "author"
    assert parse_direction('{"direction": "nobody"}') == "parse_error"


def test_split_is_stable_and_hostile_scores_count_both_classes():
    assert {split_of(f"n{i:04d}") for i in range(50)} == {"dev", "test"}
    assert split_of("n0001") == split_of("n0001")
    pairs = [("hostile", "abusive"), ("hostile", "neutral"), ("critical", "hostile")]
    precision, recall = hostile_scores(pairs)
    assert precision == pytest.approx(0.5)
    assert recall == pytest.approx(0.5)

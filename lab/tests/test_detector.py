from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pileon_lab.detector import (
    FAMILIES,
    Reference,
    bootstrap_auc,
    freeze,
    grade,
    percentile,
    quantiles,
    read_gold,
    recall_budget,
    weighted_auc,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_percentile_is_the_mid_rank_in_the_reference() -> None:
    reference = quantiles([float(i) for i in range(101)], points=100)
    assert percentile(reference, -1.0) == 0.0
    assert percentile(reference, 1000.0) == 1.0
    assert percentile(reference, 50.0) == pytest.approx(0.5, abs=0.01)


def _row(n: int, *, hot: bool) -> dict[str, object]:
    sign = 1 if hot else -1
    return {
        "incident_id": f"s{n}",
        "reply_vs_record": 5.0 + sign * n / 10,
        "dunk_ratio": 1.0 + sign * n / 10,
        "like_reply_ratio": 3.0 - sign * n / 10,
        "follower_ratio_log10": sign * n / 10,
        "labeled": 20,
        "contempt": 5 if hot else 0,
        "supportive": 0,
    }


def test_low_signals_count_toward_a_pileon_when_their_sign_is_negative() -> None:
    rows = [_row(n, hot=n % 2 == 0) for n in range(1, 41)]
    reference = freeze(rows, "retro")
    hot, cold = _row(30, hot=True), _row(30, hot=False)
    hot_score, parts = reference.score(hot)
    cold_score, _ = reference.score(cold)
    assert hot_score is not None
    assert cold_score is not None
    assert hot_score > cold_score
    assert set(parts) == {"volume", "approval", "reach"}
    assert FAMILIES["approval"]["like_reply_ratio"] == -1


def test_one_family_is_not_enough_to_score() -> None:
    reference = Reference("retro", {"dunk_ratio": [0.0, 1.0, 2.0]}, {})
    assert reference.score({"dunk_ratio": 1.5})[0] is None


def test_freeze_cutoffs_hit_their_alert_rates_on_the_reference() -> None:
    rows = [_row(n, hot=n % 2 == 0) for n in range(1, 201)]
    reference = freeze(rows, "retro")
    scores = [s for r in rows if (s := reference.score(r)[0]) is not None]
    for budget, cutoff in reference.cutoffs.items():
        rate = sum(s >= cutoff for s in scores) / len(scores)
        assert rate == pytest.approx(float(budget), abs=0.02)
    assert Reference.from_json(reference.to_json()) == reference


def test_grade_reports_auc_and_alerts() -> None:
    rows = [_row(n, hot=n % 2 == 0) for n in range(1, 41)]
    reference = freeze(rows, "retro")
    gold = {f"s{n}": (n % 2 == 0, 1.0) for n in range(1, 41)}
    lines = grade(reference, rows, gold)
    assert lines[0] == "40 graded surges (40 weighted), 20 positive; 0 unscored"
    assert lines[1].startswith("AUC 1.000")
    assert lines[-1] == "alert rate needed for 80% recall: 40%"
    band = bootstrap_auc([(1.0, False, 1.0), (2.0, False, 1.0), (3.0, True, 1.0)])
    assert band == (1.0, 1.0)


def test_weights_count_each_pair_by_both_surges_weights() -> None:
    graded = [(0.9, True, 1.0), (0.5, False, 1.0), (0.95, False, 4.0)]
    assert weighted_auc(graded) == pytest.approx(1 / 5)
    assert weighted_auc([(0.1, True, 1.0)]) is None
    assert recall_budget([(0.9, True, 1.0), (0.5, False, 3.0)]) == pytest.approx(0.25)
    assert read_gold('{"a": true, "b": {"positive": false, "weight": 4}}') == {
        "a": (True, 1.0),
        "b": (False, 4.0),
    }


ONSET_US = 1_786_000_000_000_000
MINUTE_US = 60_000_000
TARGET = "did:plc:target"
QUOTER = "did:plc:quoter"


def _follow(minutes: int) -> str:
    from pileon_lab.tid import encode_us  # noqa: PLC0415

    tid = encode_us(ONSET_US + minutes * MINUTE_US)
    return f"at://did:plc:x/app.bsky.graph.follow/{tid}"


def _iso(minutes: int) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    at = datetime.fromtimestamp((ONSET_US + minutes * MINUTE_US) / 1e6, tz=UTC)
    return at.isoformat().replace("+00:00", "Z")


def _fake_network() -> Any:
    import httpx  # noqa: PLC0415

    def post(rkey: str, replies: int, *, likes: int = 0) -> dict[str, Any]:
        return {
            "uri": f"at://{TARGET}/app.bsky.feed.post/{rkey}",
            "author": {"did": TARGET},
            "record": {"text": ""},
            "replyCount": replies,
            "likeCount": likes,
        }

    posts = {
        p["uri"]: p
        for p in [post("p", 9, likes=2), *(post(f"h{n}", n) for n in (1, 3, 5))]
    }
    relationships = {
        TARGET: {"did:plc:a": _follow(-60), "did:plc:b": None},
        QUOTER: {"did:plc:a": None, "did:plc:b": _follow(-1)},
    }

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - per endpoint
        params = request.url.params
        path = request.url.path
        if request.url.host == "plc.directory":
            service = {"id": "#atproto_pds", "serviceEndpoint": "https://pds.example/"}
            return httpx.Response(200, json={"service": [service]})
        if path.endswith("listRecords"):
            records = [
                {"uri": f"at://{TARGET}/app.bsky.feed.post/h{n}"} for n in (1, 3, 5)
            ]
            return httpx.Response(200, json={"records": records})
        if path.endswith("getPosts"):
            found = [posts[u] for u in params.get_list("uris") if u in posts]
            return httpx.Response(200, json={"posts": found})
        if path.endswith("getLikes"):
            likes = [
                {"actor": {"did": "did:plc:a"}, "createdAt": _iso(1)},
                {"actor": {"did": "did:plc:z"}, "createdAt": _iso(60 * 24)},
            ]
            return httpx.Response(200, json={"likes": likes})
        if path.endswith("getRelationships"):
            mine = relationships[params["actor"]]
            rels = [
                {
                    "$type": "app.bsky.graph.defs#relationship",
                    "did": d,
                    "followedBy": mine[d],
                }
                for d in params.get_list("others")
            ]
            return httpx.Response(200, json={"relationships": rels})
        if path.endswith("getProfiles"):
            counts = {TARGET: 99, QUOTER: 9_999}
            profiles = [
                {"did": d, "followersCount": counts[d]}
                for d in params.get_list("actors")
            ]
            return httpx.Response(200, json={"profiles": profiles})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_live_row_cuts_everything_at_the_trigger(tmp_path: Path) -> None:
    import asyncio  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from pileon_lab.detector import live_row  # noqa: PLC0415
    from pileon_lab.fetch import Cache, Fetcher  # noqa: PLC0415
    from pileon_lab.surges import parse_surge  # noqa: PLC0415

    quote = {
        "uri": f"at://{QUOTER}/app.bsky.feed.post/q",
        "author": QUOTER,
        "at_us": ONSET_US,
        "engagement": {"replies": 0, "quotes": 0, "reposts": 0, "likes": 3},
    }
    surge = parse_surge(
        {
            "target": TARGET,
            "onset_us": ONSET_US,
            "triggered_us": ONSET_US + 30 * MINUTE_US,
            "trigger_target_post": f"at://{TARGET}/app.bsky.feed.post/p",
            "trigger_quote": quote,
            "warmup": False,
            "baseline_per_hour": 0.1,
            "strangers": 3,
            "arrivals": 3,
            "replies": 3,
            "quotes": 1,
            "mentions": 0,
            "peak_surprise": 9.0,
            "target_posts": [[f"at://{TARGET}/app.bsky.feed.post/p", 3]],
            "quote_posts": [quote],
            "recorded": [
                [0, QUOTER, "q", "q", "p", True],
                [60, "did:plc:a", "r", "a1", "p", True],
                [120, "did:plc:a", "r", "a2", "p", True],
                [180, "did:plc:b", "r", "b1", "p", True],
                [31 * 60, "did:plc:c", "r", "c1", "p", True],
            ],
        }
    )

    async def run() -> dict[str, Any]:
        client = httpx.AsyncClient(transport=_fake_network())
        fetcher = Fetcher(Cache(tmp_path / "c.db"), client=client)
        try:
            return await live_row(surge, {"incident_id": surge.surge_id}, fetcher)
        finally:
            await fetcher.close()

    row = asyncio.run(run())
    assert row["live_cut_s"] == 1800.0
    assert row["live_arrivals"] == 2.0
    assert row["live_lift"] == pytest.approx(2 / 3)
    assert row["live_likes_per_reply"] == pytest.approx(2 / 4)
    assert row["live_liked_share"] == 0.5
    assert row["live_follow_target"] == 0.5
    assert row["live_follow_amplifier"] == 0.5
    assert row["live_follower_ratio_log10"] == pytest.approx(2.0)
    assert row["target_followers"] == 99
    assert row["live_same_amplifier"] is True

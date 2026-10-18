from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from pileon_lab.detector import Reference
from pileon_lab.heldout import Dev
from pileon_lab.surges import QuoteCandidate, SurgeRecord, copy_surges, read_surges
from pileon_lab.trawl import (
    DRAWS,
    Judged,
    draw,
    enrichment,
    exclusion,
    fisher_greater,
    frame_rate,
    report,
    tiers,
    trend_p,
    union,
)

if TYPE_CHECKING:
    from pathlib import Path

LATE = datetime(2026, 9, 2, tzinfo=UTC)
CHEAP = Reference(
    "retro",
    {
        "like_reply_ratio": [float(i) for i in range(101)],
        "dunk_ratio": [float(i) for i in range(101)],
        "follower_ratio_log10": [float(i) for i in range(101)],
    },
    {},
)


def surge(**changes: Any) -> SurgeRecord:
    fields: dict[str, Any] = {
        "target": "did:plc:t",
        "onset_us": int(LATE.timestamp() * 1e6),
        "warmup": False,
        "baseline_per_hour": 0.1,
        "strangers": 20,
        "arrivals": 20,
        "replies": 8,
        "quotes": 2,
        "mentions": 0,
        "peak_surprise": 5.0,
        "target_posts": (("at://did:plc:t/app.bsky.feed.post/p", 8),),
        "quote_posts": (
            QuoteCandidate("at://did:plc:q/app.bsky.feed.post/q", "did:plc:q", 0, 9),
        ),
        "recorded": (),
    }
    return SurgeRecord(**(fields | changes))


def test_the_frame_keeps_late_busy_surges_away_from_dev_people() -> None:
    dev = Dev(frozenset({"did:plc:dev"}))
    assert exclusion(surge(), dev) is None
    assert exclusion(surge(warmup=True), dev) == "warm-up"
    assert exclusion(surge(target_posts=()), dev) == "no target post"
    early = int(datetime(2026, 8, 20, tzinfo=UTC).timestamp() * 1e6)
    assert exclusion(surge(onset_us=early), dev) == "interim window"
    assert exclusion(surge(replies=7), dev) == "fewer than 10 replies and quotes"
    assert exclusion(surge(target="did:plc:dev"), dev) == "overlaps dev"
    quoted = (
        QuoteCandidate("at://did:plc:dev/app.bsky.feed.post/q", "did:plc:dev", 0, 1),
    )
    assert exclusion(surge(quote_posts=quoted), dev) == "overlaps dev"


def _row(n: int, *, approval: float, reach: float) -> dict[str, Any]:
    return {
        "incident_id": f"s{n:04}",
        "like_reply_ratio": 100 - approval,
        "dunk_ratio": approval,
        "follower_ratio_log10": reach,
    }


def test_tiers_carve_out_the_most_approved_then_cut_by_score() -> None:
    rows = [_row(n, approval=n % 100, reach=(n * 37) % 100) for n in range(1000)]
    rows.append({"incident_id": "lonely", "dunk_ratio": 5.0})
    assigned, cuts = tiers(rows, CHEAP)
    assert "lonely" not in assigned
    counts = {t: list(assigned.values()).count(t) for t in set(assigned.values())}
    assert counts["rally"] == pytest.approx(50, abs=15)
    assert counts["top"] == pytest.approx(5, abs=6)  # ties at the cutoff join
    assert counts["high"] + counts["top"] == pytest.approx(20, abs=8)
    best = max(rows[:1000], key=lambda r: r["dunk_ratio"] + r["follower_ratio_log10"])
    assert assigned[best["incident_id"]] == "top"
    assert all(
        CHEAP.score(r)[1]["approval"] <= cuts["rally_approval"]
        for r in rows[:1000]
        if assigned[r["incident_id"]] == "rally"
    )


def test_draw_is_seeded_and_weights_by_tier_size() -> None:
    assigned = {f"s{i:04}": "low" for i in range(400)}
    assigned |= {f"t{i:02}": "top" for i in range(10)}
    planned = draw(assigned)
    assert planned == draw(assigned)
    low = [e for e in planned.values() if e["stratum"] == "low"]
    assert len(low) == DRAWS["low"]
    assert low[0]["weight"] == pytest.approx(400 / DRAWS["low"])
    top = [e for e in planned.values() if e["stratum"] == "top"]
    assert len(top) == 10
    assert top[0]["weight"] == 1.0


def test_trend_and_fisher_tests() -> None:
    assert trend_p([(0, 10, 0.0), (0, 10, 1.0)]) is None
    rising = trend_p([(0, 30, 0.0), (1, 30, 1.0), (3, 30, 2.0), (9, 30, 3.0)])
    assert rising is not None
    assert rising < 0.001
    falling = trend_p([(9, 30, 0.0), (0, 30, 3.0)])
    assert falling is not None
    assert falling > 0.99
    assert fisher_greater(3, 3, 0, 3) == pytest.approx(1 / 20)
    assert fisher_greater(0, 3, 3, 3) == pytest.approx(1.0)


def test_report_counts_judged_classes_and_writes_weighted_gold() -> None:
    planned = {
        "a": {"stratum": "top", "weight": 5.0},
        "b": {"stratum": "top", "weight": 5.0},
        "c": {"stratum": "low", "weight": 1000.0},
        "d": {"stratum": "low", "weight": 1000.0},
        "e": {"stratum": "rally", "weight": 100.0},
        "f": {"stratum": "low", "weight": 1000.0},
    }
    judged = Judged(
        {"a": "pileon", "b": "crossfire", "c": "rally", "d": None}, frozenset({"f"})
    )
    sizes = {"top": 10, "low": 3000, "rally": 100}
    lines, gold = report(planned, judged, sizes)
    assert gold["primary"] == {
        "a": {"positive": True, "weight": 5.0},
        "b": {"positive": False, "weight": 5.0},
        "c": {"positive": False, "weight": 3000.0},
    }
    assert gold["hostile"]["b"]["positive"] is True
    text = "\n".join(lines)
    assert "1/2 50%" in next(line for line in lines if line.startswith("top"))
    assert "'verdict missing': 1" in text
    assert "'fewer than 10 labels': 1" in text
    assert "lost: {'no packet': 1}" in text
    assert "INCOMPLETE: 1 surges lack a verdict" in text


def test_enrichment_compares_the_top_tier_with_the_frame() -> None:
    outcomes = {"top": [True] * 2 + [False] * 8, "low": [True] + [False] * 99}
    sizes = {"top": 100, "low": 9900}
    rate = frame_rate(outcomes, sizes)
    assert rate == pytest.approx((100 * 0.2 + 9900 * 0.01) / 10000)
    point, band, undefined = enrichment(outcomes, sizes)
    assert rate is not None
    assert point == pytest.approx(0.2 / rate)
    assert band is not None
    assert band[0] < point < band[1]
    assert 0 <= undefined < 0.5
    assert enrichment({"top": [False] * 5, "low": [False] * 5}, sizes)[0] is None


def test_union_merges_strata_and_copy_keeps_lines_verbatim(tmp_path: Path) -> None:
    merged = union(
        {
            "heldout": {
                "x": {"stratum": "random", "weight": 4.0, "overlap": False},
                "y": {"stratum": "unsampled", "weight": 0.0, "overlap": False},
            },
            "trawl": {"x": {"stratum": "top", "weight": 5.0, "overlap": False}},
        }
    )
    assert merged == {
        "x": {"stratum": "heldout:random+trawl:top", "weight": 1.0, "overlap": False}
    }
    line = {
        "target": "did:plc:t",
        "onset_us": 7,
        "warmup": False,
        "baseline_per_hour": 0.1,
        "strangers": 9,
        "arrivals": 9,
        "replies": 9,
        "quotes": 1,
        "mentions": 0,
        "peak_surprise": 4.5,
        "target_posts": [["at://did:plc:t/app.bsky.feed.post/p", 9]],
        "quote_posts": [
            {"uri": "at://q", "author": "did:plc:q", "at_us": 1, "engagement": {"l": 2}}
        ],
        "recorded": [[5, "did:plc:r", "r", "k", "p", 1]],
        "extra": "kept",
    }
    source = tmp_path / "all.jsonl"
    source.write_text(
        json.dumps(line) + "\n" + json.dumps(line | {"onset_us": 8}) + "\n"
    )
    out = tmp_path / "some.jsonl"
    assert copy_surges(source, {"did:plc:t:7"}, out) == 1
    assert json.loads(out.read_text()) == line
    assert [s.surge_id for s in read_surges(out)] == ["did:plc:t:7"]

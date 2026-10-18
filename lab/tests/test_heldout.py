from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from pileon_lab.detector import Reference
from pileon_lab.heldout import Dev, gold, plan, stratum
from pileon_lab.surges import QuoteCandidate, SurgeRecord

if TYPE_CHECKING:
    from pathlib import Path

LATE = datetime(2026, 9, 2, tzinfo=UTC)
EARLY = datetime(2026, 8, 20, tzinfo=UTC)
ALERTING = Reference(
    "retro", {"dunk_ratio": [0.0, 1.0], "reply_vs_record": [0.0, 1.0]}, {"0.10": 0.9}
)


def surge(n: int, *, onset: datetime = LATE, quoter: str = "did:plc:q") -> SurgeRecord:
    return SurgeRecord(
        target=f"did:plc:t{n}",
        onset_us=int(onset.timestamp() * 1e6),
        warmup=False,
        baseline_per_hour=0.1,
        strangers=20,
        arrivals=20,
        replies=20,
        quotes=1,
        mentions=0,
        peak_surprise=5.0,
        target_posts=((f"at://did:plc:t{n}/app.bsky.feed.post/p", 20),),
        quote_posts=(
            QuoteCandidate(f"at://{quoter}/app.bsky.feed.post/q", quoter, 0, 9),
        ),
        recorded=(),
    )


def row(
    sid: str, *, contempt: int = 0, labeled: int = 20, hot: bool = False
) -> dict[str, Any]:
    value = 5.0 if hot else 0.5
    return {
        "incident_id": sid,
        "labeled": labeled,
        "contempt": contempt,
        "supportive": 0,
        "dunk_ratio": value,
        "reply_vs_record": value,
    }


def test_strata_follow_the_preregistered_order() -> None:
    assert stratum(row("a", contempt=1), None, [ALERTING], surge_id="a") == (
        "screen",
        1.0,
    )
    assert stratum(row("b", hot=True), None, [ALERTING], surge_id="b") == ("alert", 1.0)
    kinds = [
        stratum(row(f"c{i}"), None, [ALERTING], surge_id=f"c{i}")[0] for i in range(400)
    ]
    assert set(kinds) == {"random", "unsampled"}
    assert kinds.count("random") / len(kinds) == pytest.approx(0.25, abs=0.06)
    assert stratum(row("c1"), None, [ALERTING], surge_id="c1") == stratum(
        row("c1"), None, [ALERTING], surge_id="c1"
    )


def test_plan_drops_interim_and_thin_surges_and_flags_overlap() -> None:
    surges = [surge(1, onset=EARLY), surge(2), surge(3), surge(4, quoter="did:plc:dev")]
    rows = {s.surge_id: row(s.surge_id, contempt=2) for s in surges}
    rows[surges[2].surge_id]["labeled"] = 9
    planned, lines = plan(surges, rows, {}, [ALERTING], Dev(frozenset({"did:plc:dev"})))
    assert set(planned) == {surges[1].surge_id, surges[3].surge_id}
    assert planned[surges[3].surge_id]["overlap"] is True
    assert planned[surges[1].surge_id] == {
        "stratum": "screen",
        "weight": 1.0,
        "overlap": False,
    }
    assert lines[-1].split()[1:5] == ["4", "1", "1", "0"]


def test_gold_prefers_in_context_labels_and_weights_them(tmp_path: Path) -> None:
    planned = {
        "judged": {"stratum": "random", "weight": 4.0, "overlap": False},
        "gone": {"stratum": "screen", "weight": 1.0, "overlap": False},
        "shared": {"stratum": "screen", "weight": 1.0, "overlap": True},
        "skipped": {"stratum": "unsampled", "weight": 0.0, "overlap": False},
    }
    rows = {
        "judged": row("judged"),
        "gone": row("gone", contempt=5),
        "shared": row("shared", contempt=5),
        "skipped": row("skipped", contempt=5),
    }
    (tmp_path / "key.json").write_text(
        json.dumps({"S01": {"surge": "judged", "posts": {}}})
    )
    for judge in ("opus", "fable"):
        lines = [{"surge": "S01", "verdict": "rally", "severity": 0, "piled_on": False}]
        lines += [
            {"surge": "S01", "post": f"P{i}", "label": "hostile", "aimed": "target"}
            for i in range(3)
        ]
        lines += [
            {"surge": "S01", "post": f"P{i}", "label": "neutral", "aimed": "none"}
            for i in range(3, 8)
        ]
        (tmp_path / f"{judge}.jsonl").write_text("\n".join(map(json.dumps, lines)))
    out, sources = gold(
        planned, tmp_path, rows, measure="primary", include_overlap=False
    )
    assert out == {
        "judged": {"positive": True, "weight": 4.0},
        "gone": {"positive": True, "weight": 1.0},
    }
    assert sources == {
        "in context": 1,
        "cascade (unjudgeable)": 1,
        "overlap excluded": 1,
    }
    cascade, _ = gold(planned, None, rows, measure="cascade", include_overlap=True)
    assert cascade["judged"] == {"positive": False, "weight": 1.0}
    assert cascade["skipped"] == {"positive": True, "weight": 1.0}


def test_early_alerts_and_lead_share_use_pooled_contempt(tmp_path: Path) -> None:
    from datetime import timedelta  # noqa: PLC0415 - local to this test

    from pileon_lab.heldout import contempt_weights, early_alerts, lead_shares  # noqa: PLC0415

    rows = {
        "hot": row("hot", hot=True),
        "cold": row("cold"),
        "hit": row("hit", hot=True),
    }
    entries = {
        "hot": {"positive": False, "weight": 1.0},
        "cold": {"positive": True, "weight": 1.0},
        "hit": {"positive": True, "weight": 4.0},
    }
    assert early_alerts(entries, rows, ALERTING) == {
        "hot": "false alert",
        "hit": "true alert",
    }
    key = {"S01": {"surge": "hit", "posts": {"P1": "at://a", "P2": "at://b"}}}
    (tmp_path / "key.json").write_text(json.dumps(key))
    calls = {"opus": ["hostile", "hostile"], "fable": ["hostile", "neutral"]}
    for judge, labels in calls.items():
        lines = [
            {"surge": "S01", "post": f"P{i}", "label": label, "aimed": "target"}
            for i, label in enumerate(labels, start=1)
        ]
        (tmp_path / f"{judge}.jsonl").write_text("\n".join(map(json.dumps, lines)))
    weights = contempt_weights(tmp_path)
    assert weights == {"hit": {"at://a": 1.0, "at://b": 0.5}}
    times = {"at://a": LATE + timedelta(minutes=5), "at://b": LATE + timedelta(hours=2)}
    assert lead_shares(weights["hit"], times, LATE) == (0.5, 1.5)

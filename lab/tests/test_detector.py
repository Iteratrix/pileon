from __future__ import annotations

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

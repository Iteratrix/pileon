import pytest
from hypothesis import given
from hypothesis import strategies as st

from pileon_lab.sweep import (
    auc,
    deidentify,
    fill_template,
    ranking,
    round_sig,
    surge_class,
)


def test_auc_perfect_inverse_and_chance():
    assert auc([1, 2, 3, 4], [False, False, True, True]) == pytest.approx(1.0)
    assert auc([1, 2, 3, 4], [True, True, False, False]) == pytest.approx(0.0)
    assert auc([5, 5, 5, 5], [True, False, True, False]) == pytest.approx(0.5)
    assert auc([1, 2], [True, True]) is None


@given(st.lists(st.tuples(st.integers(-5, 5), st.booleans()), min_size=2))
def test_auc_matches_pairwise_definition(pairs):
    scores = [p[0] for p in pairs]
    labels = [p[1] for p in pairs]
    pos = [s for s, lab in pairs if lab]
    neg = [s for s, lab in pairs if not lab]
    result = auc(scores, labels)
    if not pos or not neg:
        assert result is None
        return
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    assert result == pytest.approx(wins / (len(pos) * len(neg)))


def test_round_sig_and_deidentify_drop_identifiers():
    assert round_sig(123_456) == 120_000
    assert round_sig(0) == 0
    row = {
        "incident_id": "did:plc:secret:2026-01-01",
        "anchor": "2026-01-01T00:00:00+00:00",
        "target_followers": 1234,
        "reply_lift_median": 3.14159265,
        "contempt": 4,
    }
    out = deidentify(row)
    assert "incident_id" not in out
    assert "anchor" not in out
    assert out["target_followers"] == 1200
    assert out["reply_lift_median"] == pytest.approx(3.1416)


def test_ranking_puts_the_separating_dimension_first():
    rows = [
        {"reply_lift_median": float(i), "arrivals": 7, "contempt": int(i >= 5)}
        for i in range(10)
    ]
    ranked = ranking(rows, positive=lambda r: r["contempt"] >= 1)
    assert ranked[0][0] == "reply_lift_median"
    assert ranked[0][1] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("contempt", "supportive", "expected"),
    [(3, 1, "pileon"), (0, 5, "rally"), (3, 5, "crossfire"), (0, 1, "spotlight")],
)
def test_surge_classes_split_on_both_shares(contempt, supportive, expected):
    row = {"labeled": 20, "contempt": contempt, "supportive": supportive}
    assert surge_class(row, hostile_at=0.1, support_at=0.25) == expected


def test_fill_template_escapes_script_close():
    page = fill_template("<script>__SWEEP_DATA__</script>", '{"x":"</script><b>"}')
    assert page.count("</script>") == 1
    assert "<\\/script><b>" in page
    with pytest.raises(ValueError, match="exactly once"):
        fill_template("no placeholder", "{}")
    noted = fill_template("<p>__CORPUS_NOTE__</p>__SWEEP_DATA__", "{}", note="A & <b>")
    assert noted.startswith("<p>A &amp; &lt;b&gt;</p>")

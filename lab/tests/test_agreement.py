import pytest

from pileon_lab.agreement import cohens_kappa, weighted_kappa
from pileon_lab.codebook import V2_LABELS, codebook_text, to_gold
from pileon_lab.study import repo_of


def test_perfect_and_chance_agreement():
    same = [("hostile", "hostile"), ("neutral", "neutral"), ("critical", "critical")]
    assert cohens_kappa(same) == pytest.approx(1.0)
    assert weighted_kappa(same) == pytest.approx(1.0)
    assert cohens_kappa([]) == 0.0


def test_weighted_kappa_penalizes_distant_disagreement_more():
    near = [("critical", "hostile"), ("hostile", "hostile"), ("neutral", "neutral")]
    far = [("supportive", "hostile"), ("hostile", "hostile"), ("neutral", "neutral")]
    assert weighted_kappa(near) > weighted_kappa(far)


def test_weighted_kappa_ignores_labels_off_the_scale():
    pairs = [("hostile", "hostile"), ("neutral", "neutral"), ("parse_error", "hostile")]
    assert weighted_kappa(pairs) == pytest.approx(1.0)


def test_mapping_b_folds_abusive_and_hostile():
    assert to_gold("abusive") == "hostile"
    assert to_gold("hostile") == "hostile"
    assert to_gold("hostile", mapping="A") == "critical"
    assert to_gold("supportive") == "supportive"


def test_codebook_text_names_every_label_and_drops_io_lines():
    text = codebook_text()
    for label in V2_LABELS:
        assert f"- {label}:" in text
    assert "Reply with ONLY" not in text


def test_repo_of_reads_the_account_from_an_at_uri():
    assert repo_of("at://did:plc:abc/app.bsky.feed.post/3x") == "did:plc:abc"

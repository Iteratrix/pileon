"""Off-the-shelf sentiment and toxicity models as baselines for hostility.

These score a reply's text on its own, the way such tools are usually used:
no target post, no notion of who the reply is aimed at. They are compared
with the context-aware labelers on one binary question, "hostile or worse,
aimed at the target, or not", against both the human gold and the judges'
consensus. AUC is threshold-free; F1 is reported at 0.5 and at the best
threshold (optimistic, since that threshold is chosen on the same items).

Everything runs locally on CPU (`uv sync --group baselines`); no text leaves
the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pileon_lab.judges import contexts, gold_labels
from pileon_lab.sweep import auc

ORDINAL = {"supportive": 0, "neutral": 1, "critical": 2, "hostile": 3, "abusive": 4}
HOSTILE = frozenset({"hostile", "abusive"})


@dataclass(frozen=True, slots=True)
class Baseline:
    name: str
    model: str
    positive: str
    aspect: str | None = None


BASELINES = (
    Baseline("toxic-bert toxicity", "unitary/toxic-bert", "toxic"),
    Baseline("toxic-bert insult", "unitary/toxic-bert", "insult"),
    Baseline(
        "twitter offensive", "cardiffnlp/twitter-roberta-base-offensive", "offensive"
    ),
    Baseline(
        "twitter sentiment (negative)",
        "cardiffnlp/twitter-roberta-base-sentiment-latest",
        "negative",
    ),
    Baseline(
        "aspect sentiment toward 'you' (negative)",
        "yangheng/deberta-v3-base-absa-v1.1",
        "negative",
        aspect="you",
    ),
)


def run_baseline(baseline: Baseline, texts: list[str]) -> list[float]:
    """Probability of the baseline's positive class for each text."""
    import torch  # noqa: PLC0415 - optional dependency group
    from transformers import (  # noqa: PLC0415 - optional dependency group
        AutoModelForSequenceClassification,
        AutoTokenizer,
    )

    tokenizer = AutoTokenizer.from_pretrained(baseline.model)
    model = AutoModelForSequenceClassification.from_pretrained(baseline.model)
    model.eval()
    labels = {i: name.lower() for i, name in model.config.id2label.items()}
    index = next(i for i, name in labels.items() if name == baseline.positive)
    multi_label = model.config.problem_type == "multi_label_classification" or (
        baseline.model == "unitary/toxic-bert"
    )
    scores: list[float] = []
    with torch.no_grad():
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            encoded = (
                tokenizer(
                    batch,
                    [baseline.aspect] * len(batch),
                    truncation=True,
                    max_length=256,
                    padding=True,
                    return_tensors="pt",
                )
                if baseline.aspect
                else tokenizer(
                    batch,
                    truncation=True,
                    max_length=256,
                    padding=True,
                    return_tensors="pt",
                )
            )
            logits = model(**encoded).logits
            probs = (
                torch.sigmoid(logits) if multi_label else torch.softmax(logits, dim=-1)
            )
            scores.extend(float(p) for p in probs[:, index])
    return scores


def f1_at(scores: list[float], gold: list[bool], threshold: float) -> float:
    predicted = [s >= threshold for s in scores]
    tp = sum(p and g for p, g in zip(predicted, gold, strict=True))
    fp = sum(p and not g for p, g in zip(predicted, gold, strict=True))
    fn = sum(g and not p for p, g in zip(predicted, gold, strict=True))
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def best_f1(scores: list[float], gold: list[bool]) -> float:
    return max((f1_at(scores, gold, t) for t in sorted(set(scores))), default=0.0)


def read_labels(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("label") in ORDINAL:
                out[row["id"]] = row["label"]
    return out


def probability_entries(path: Path) -> dict[str, dict[str, float]]:
    """Continuous hostility scores from a decision model's probabilities."""
    if path.stem.endswith("-gated"):
        return {}
    hostile: dict[str, float] = {}
    aimed: dict[str, float] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        probabilities = row.get("probabilities")
        if not probabilities:
            continue
        p = sum(float(probabilities.get(label, 0.0)) for label in HOSTILE)
        hostile[row["id"]] = p
        if row.get("aimed") is not None:
            aimed[row["id"]] = p * float(row["aimed"])
    out = {}
    if hostile:
        out[f"{path.stem} P(hostile+)"] = hostile
    if aimed:
        out[f"{path.stem} P(hostile+) x aimed"] = aimed
    return out


def evaluate(
    entries: dict[str, dict[str, float]], gold: dict[str, bool], *, natural: set[str]
) -> list[str]:
    """One line per entry: AUC, F1 at 0.5 (or the natural cut), best F1."""
    lines = []
    for name, scores in entries.items():
        ids = sorted(i for i in gold if i in scores)
        if not ids or not any(gold[i] for i in ids):
            continue
        s = [scores[i] for i in ids]
        g = [gold[i] for i in ids]
        cut = 2.5 if name in natural else 0.5
        lines.append(
            f"  {name:<44} n={len(ids):>3}  AUC {auc(s, g) or 0:.3f}  "
            f"F1 {f1_at(s, g, cut):.3f}  best F1 {best_f1(s, g):.3f}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, required=True, help="a judge packet dir")
    parser.add_argument("--study-db", type=Path, help="human gold source (optional)")
    parser.add_argument("--extra-gold", type=Path)
    args = parser.parse_args(argv)

    rows = [
        json.loads(line)
        for line in (args.dir / "packet.jsonl").read_text().splitlines()
    ]
    ids = [r["id"] for r in rows]
    cache = args.dir / "baseline-scores.json"
    stored: dict[str, Any] = json.loads(cache.read_text()) if cache.exists() else {}
    for baseline in BASELINES:
        if baseline.name in stored:
            continue
        print(f"[baselines] {baseline.name}", file=sys.stderr, flush=True)
        scores = run_baseline(baseline, [r["reply"] for r in rows])
        stored[baseline.name] = dict(zip(ids, scores, strict=True))
        cache.write_text(json.dumps(stored))

    entries: dict[str, dict[str, float]] = dict(stored)
    natural: set[str] = set()
    for path in sorted(args.dir.glob("*.jsonl")):
        if path.stem == "packet":
            continue
        labels = read_labels(path)
        entries[path.stem] = {i: float(ORDINAL[label]) for i, label in labels.items()}
        natural.add(path.stem)
        entries.update(probability_entries(path))

    out = []
    judges = {j: read_labels(args.dir / f"{j}.jsonl") for j in ("opus", "fable")}
    if all(judges.values()):
        consensus = {
            i: judges["opus"][i] in HOSTILE
            for i in judges["opus"]
            if judges["fable"].get(i) == judges["opus"][i]
        }
        out.append(f"== vs judge consensus (hostile or worse), {len(consensus)} items")
        out += evaluate(
            {k: v for k, v in entries.items() if k not in judges},
            consensus,
            natural=natural,
        )
    if args.study_db is not None:
        key: dict[str, str] = json.loads((args.dir / "key.json").read_text())
        human = gold_labels(args.study_db, args.extra_gold)
        ctx = contexts(args.study_db, sorted(human))
        gold = {
            i: human[u] == "hostile"
            for i, u in key.items()
            if u in human and ctx.get(u, {}).get("person")
        }
        out.append(f"== vs human gold (hostile), persons-only, {len(gold)} items")
        out += evaluate(entries, gold, natural=natural)
    text = "\n".join(out)
    (args.dir / "baselines.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

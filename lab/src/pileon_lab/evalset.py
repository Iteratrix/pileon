"""Hill-climb valence labelers against judge consensus, with a human guardrail.

`packet` writes a blind packet of labeled network items for the gold judges,
stratified by the bulk label so hostile items are well represented. `run`
labels any packet (a network packet, or the study's human-gold packet from
`pileon_lab.judges`) with a local model and a named prompt variant. `score`
compares every labeler in a packet directory with the judges' consensus
(items both judges labeled the same) on a fixed dev/test split: variants
compete on dev; only a chosen winner is read on test. A variant only counts as
better if it also holds up against the human gold (score the study packet
with `pileon_lab.judges score`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path

from pileon_lab.agreement import weighted_kappa
from pileon_lab.codebook import V2_LABELS, codebook_text, parse_label, valence_prompt
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.label import Item, LabelStore, Ollama, hydrate
from pileon_lab.surges import read_surges, sample

PromptBuilder = Callable[[str, str, str, str], str]
"""(target handle, reply text, target post, framing) -> prompt."""

VARIANTS: dict[str, PromptBuilder] = {
    "v2": lambda handle, text, target_post, framing: valence_prompt(
        handle, text, target_post=target_post, framing=framing
    ),
}
HOSTILE = frozenset({"hostile", "abusive"})
JUDGES = ("opus", "fable")


def split_of(item_id: str) -> str:
    """Fixed dev/test assignment from a hash of the item id."""
    return "dev" if hashlib.sha256(item_id.encode()).digest()[0] % 2 == 0 else "test"


def write_packet(
    items: list[Item], labels: dict[str, str], outdir: Path, *, size: int, seed: int
) -> int:
    """Write a blind packet: half bulk-hostile items (when there are enough)."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling
    hostile = [i for i in items if labels.get(i.uri) in HOSTILE]
    other = [i for i in items if i.uri in labels and labels[i.uri] not in HOSTILE]
    rng.shuffle(hostile)
    rng.shuffle(other)
    take_hostile = min(len(hostile), size // 2)
    chosen = hostile[:take_hostile] + other[: size - take_hostile]
    rng.shuffle(chosen)
    outdir.mkdir(parents=True, exist_ok=True)
    key = {}
    with (outdir / "packet.jsonl").open("w") as packet:
        for n, item in enumerate(chosen, start=1):
            item_id = f"n{n:04d}"
            key[item_id] = item.uri
            packet.write(
                json.dumps(
                    {
                        "id": item_id,
                        "target": item.target_handle,
                        "framing": item.framing,
                        "target_post": item.target_post,
                        "reply": item.text,
                    }
                )
                + "\n"
            )
    (outdir / "key.json").write_text(json.dumps(key, indent=1))
    (outdir / "codebook.md").write_text(codebook_text())
    return len(chosen)


def run_variant(packet: Path, out: Path, model: Ollama, variant: str) -> int:
    """Label every packet item with `variant`; resumes from an existing output."""
    build = VARIANTS[variant]
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text().splitlines() if line}
    count = 0
    with out.open("a") as sink:
        for line in packet.read_text().splitlines():
            row = json.loads(line)
            if row["id"] in done:
                continue
            raw = model.ask(
                build(row["target"], row["reply"], row["target_post"], row["framing"])
            )
            label, confidence = parse_label(raw)
            sink.write(
                json.dumps({"id": row["id"], "label": label, "confidence": confidence})
                + "\n"
            )
            sink.flush()
            count += 1
            if count % 25 == 0:
                print(f"[{out.stem}] {count}", file=sys.stderr, flush=True)
    return count


def read_labels(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("label") in V2_LABELS:
                out[row["id"]] = row["label"]
    return out


def hostile_scores(pairs: list[tuple[str, str]]) -> tuple[float, float]:
    """Precision and recall of `hostile or abusive` against the gold."""
    flagged = [(a in HOSTILE, b in HOSTILE) for a, b in pairs]
    tp = sum(1 for a, b in flagged if a and b)
    predicted = sum(1 for a, _ in flagged if a)
    actual = sum(1 for _, b in flagged if b)
    return (tp / predicted if predicted else 0.0, tp / actual if actual else 0.0)


def score(outdir: Path, *, split: str) -> list[str]:
    """Score every labeler file in `outdir` against judge consensus on `split`."""
    judges = {name: read_labels(outdir / f"{name}.jsonl") for name in JUDGES}
    shared = set.intersection(*(set(j) for j in judges.values()))
    consensus = {
        i: judges[JUDGES[0]][i]
        for i in shared
        if len({j[i] for j in judges.values()}) == 1
    }
    lines = [
        (
            f"judges agree on {len(consensus)} of {len(shared)} shared items; "
            f"scoring on {split}"
        )
    ]
    for path in sorted(outdir.glob("*.jsonl")):
        if path.stem in JUDGES or path.stem == "packet":
            continue
        labels = read_labels(path)
        ids = sorted(i for i in consensus if i in labels and split_of(i) == split)
        if not ids:
            continue
        pairs = [(labels[i], consensus[i]) for i in ids]
        agree = sum(a == b for a, b in pairs) / len(pairs)
        precision, recall = hostile_scores(pairs)
        lines.append(
            f"{path.stem:<36} n={len(ids):>4}  agree {agree:4.0%}  "
            f"weighted {weighted_kappa(pairs, order=V2_LABELS):.3f}  "
            f"hostile P {precision:.2f} R {recall:.2f}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("packet")
    pk.add_argument("--surges", type=Path, required=True)
    pk.add_argument("--cache", type=Path, required=True)
    pk.add_argument("--labels", type=Path, required=True)
    pk.add_argument("--labeler", default="qwen3.5:27b-mlx+v2")
    pk.add_argument("--sample-size", type=int, default=500)
    pk.add_argument("--per-surge", type=int, default=30)
    pk.add_argument("--seed", type=int, default=20261017)
    pk.add_argument("--include-warmup", action="store_true")
    pk.add_argument("--size", type=int, default=300)
    pk.add_argument("--dir", type=Path, required=True)
    rn = sub.add_parser("run")
    rn.add_argument("--dir", type=Path, required=True)
    rn.add_argument("--ollama", default="http://localhost:11435")
    rn.add_argument("--model", required=True)
    rn.add_argument("--variant", default="v2", choices=sorted(VARIANTS))
    sc = sub.add_parser("score")
    sc.add_argument("--dir", type=Path, required=True)
    sc.add_argument("--split", default="dev", choices=("dev", "test"))
    args = parser.parse_args(argv)

    if args.command == "packet":
        surges = sample(
            list(read_surges(args.surges)),
            size=args.sample_size,
            seed=args.seed,
            include_warmup=args.include_warmup,
        )

        async def fetch() -> list[Item]:
            fetcher = Fetcher(Cache(args.cache), offline=True)
            try:
                return await hydrate(
                    surges, fetcher, per_surge=args.per_surge, seed=args.seed
                )
            finally:
                await fetcher.close()

        items = asyncio.run(fetch())
        store = LabelStore(args.labels)
        labels = store.labeled(args.labeler)
        store.close()
        written = write_packet(items, labels, args.dir, size=args.size, seed=args.seed)
        print(f"wrote {written} items to {args.dir}", file=sys.stderr)
    elif args.command == "run":
        model = Ollama(args.ollama, args.model)
        try:
            name = f"{args.model.replace(':', '_')}+{args.variant}"
            run_variant(
                args.dir / "packet.jsonl",
                args.dir / f"{name}.jsonl",
                model,
                args.variant,
            )
        finally:
            model.close()
    else:
        print("\n".join(score(args.dir, split=args.split)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

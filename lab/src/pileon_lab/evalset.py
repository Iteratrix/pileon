"""Hill-climb valence labelers against judge consensus, with a human guardrail.

`packet` writes a blind packet of labeled network items for the gold judges,
stratified by the bulk label so hostile items are well represented. `run`
labels any packet (a network packet, or the study's human-gold packet from
`pileon_lab.judges`) with a local model and a named prompt variant; `direct`
adds the bulk labeler's direction pass to such a run. `bulk`
writes the bulk labeler's stored labels for a packet's items. `score`
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
from pileon_lab.codebook import (
    LABEL_SCHEMA,
    V2_LABELS,
    VARIANTS,
    codebook_text,
    direction_prompt,
    parse_direction,
    parse_label,
)
from pileon_lab.decision import SystemOne, gate
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.label import Item, LabelStore, Ollama, hydrate
from pileon_lab.surges import read_surges, sample

HOSTILE = frozenset({"hostile", "abusive"})
JUDGES = ("opus", "fable")
RUN_VARIANTS = sorted([*VARIANTS, *(f"{v}+json" for v in VARIANTS), "systemone"])


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
    return len(write_items(chosen, outdir))


def write_items(chosen: list[Item], outdir: Path) -> dict[str, str]:
    """Write `chosen` as a blind judge packet; return the id -> URI key."""
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
    return key


def sampled_items(  # noqa: PLR0913 - mirrors the labeler CLI
    surges: Path,
    cache: Path,
    *,
    sample_size: int,
    per_surge: int,
    seed: int,
    include_warmup: bool,
) -> list[Item]:
    """The labeler's items for the same sample, hydrated from the cache only."""
    chosen = sample(
        list(read_surges(surges)),
        size=sample_size,
        seed=seed,
        include_warmup=include_warmup,
    )

    async def fetch() -> list[Item]:
        fetcher = Fetcher(Cache(cache), offline=True)
        try:
            return await hydrate(chosen, fetcher, per_surge=per_surge, seed=seed)
        finally:
            await fetcher.close()

    return asyncio.run(fetch())


def add_sample_args(parser: argparse.ArgumentParser) -> None:
    """The labeler's sample arguments, shared by every packet builder."""
    parser.add_argument("--surges", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--labeler", default="gemma4:31b+contrast")
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--per-surge", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--include-warmup", action="store_true")


Labeler = Callable[[dict[str, str]], dict[str, object]]


def chat_labeler(model: Ollama, variant: str) -> Labeler:
    """A chat labeler; a `+json` suffix holds decoding to the label schema."""
    base, structured = variant.removesuffix("+json"), variant.endswith("+json")
    build = VARIANTS[base]
    schema = LABEL_SCHEMA if structured else None

    def label(row: dict[str, str]) -> dict[str, object]:
        raw = model.ask(
            build(row["target"], row["reply"], row["target_post"], row["framing"]),
            schema=schema,
        )
        value, confidence = parse_label(raw)
        return {"label": value, "confidence": confidence}

    return label


def decision_labeler(model: SystemOne) -> Labeler:
    """A decision-model labeler that keeps every probability it returns."""

    def label(row: dict[str, str]) -> dict[str, object]:
        decision = model.decide(
            row["target"],
            row["reply"],
            target_post=row["target_post"],
            framing=row["framing"],
        )
        return {
            "label": decision.label,
            "confidence": decision.confidence,
            "probabilities": decision.probabilities,
            "aimed": decision.aimed,
            "severity": decision.severity,
        }

    return label


def run_variant(packet: Path, out: Path, labeler: Labeler) -> int:
    """Label every packet item; resumes from an existing output."""
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text().splitlines() if line}
    count = 0
    with out.open("a") as sink:
        for line in packet.read_text().splitlines():
            row = json.loads(line)
            if row["id"] in done:
                continue
            sink.write(json.dumps({"id": row["id"], **labeler(row)}) + "\n")
            sink.flush()
            count += 1
            if count % 25 == 0:
                print(f"[{out.stem}] {count}", file=sys.stderr, flush=True)
    return count


def write_gated(source: Path, out: Path) -> None:
    """Copy decision rows with hostility not aimed at the target as neutral."""
    with out.open("w") as sink:
        for line in source.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            row["label"] = gate(row["label"], row.get("aimed"))
            sink.write(json.dumps(row) + "\n")


def direct_variant(packet: Path, source: Path, out: Path, model: Ollama) -> int:
    """The bulk labeler's direction pass, on a packet run's labels.

    Each hostile or abusive label gets the direction prompt; anything not
    aimed at the author folds to neutral, as in `write_bulk`. Resumes from an
    existing output.
    """
    rows = {
        row["id"]: row
        for row in (
            json.loads(line) for line in packet.read_text().splitlines() if line
        )
    }
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.read_text().splitlines() if line}
    asked = 0
    with out.open("a") as sink:
        for line in source.read_text().splitlines():
            if not line.strip():
                continue
            labeled = json.loads(line)
            if labeled["id"] in done:
                continue
            if labeled["label"] in HOSTILE:
                row = rows[labeled["id"]]
                raw = model.ask(
                    direction_prompt(
                        row["target"], row["reply"], target_post=row["target_post"]
                    )
                )
                labeled["direction"] = parse_direction(raw)
                if labeled["direction"] != "author":
                    labeled["label"] = "neutral"
                asked += 1
            sink.write(json.dumps(labeled) + "\n")
            sink.flush()
    return asked


def write_bulk(outdir: Path, labels_db: Path, labeler: str) -> tuple[Path, Path]:
    """Write a bulk labeler's labels for a packet's items, as scored files.

    Two files: the labels as stored, and with the direction pass applied
    (hostility not aimed at the author folds to neutral, the codebook's
    reading of third-party negativity), which is what counts toward a pile-on.
    """
    key: dict[str, str] = json.loads((outdir / "key.json").read_text())
    store = LabelStore(labels_db)
    try:
        labels, directed = store.labeled(labeler), store.directed(labeler)
    finally:
        store.close()
    name = labeler.replace(":", "_")
    stored = outdir / f"{name}@bulk.jsonl"
    folded = outdir / f"{name}@bulk-directed.jsonl"
    with stored.open("w") as raw, folded.open("w") as gated:
        for item_id, uri in key.items():
            label = labels.get(uri)
            if label is None:
                continue
            raw.write(json.dumps({"id": item_id, "label": label}) + "\n")
            if label in HOSTILE and directed.get(uri) != "author":
                label = "neutral"
            gated.write(json.dumps({"id": item_id, "label": label}) + "\n")
    return stored, folded


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


def run(args: argparse.Namespace) -> None:
    """Label a packet with one model and variant (`systemone`: a decision model)."""
    packet, name = args.dir / "packet.jsonl", args.model.replace(":", "_")
    if args.variant == "systemone":
        decider = SystemOne(args.ollama, args.model)
        try:
            out = args.dir / f"{name}+systemone.jsonl"
            run_variant(packet, out, decision_labeler(decider))
            write_gated(out, args.dir / f"{name}+systemone-gated.jsonl")
        finally:
            decider.close()
    else:
        model = Ollama(args.ollama, args.model)
        try:
            out = args.dir / f"{name}+{args.variant}.jsonl"
            run_variant(packet, out, chat_labeler(model, args.variant))
        finally:
            model.close()


def direct(args: argparse.Namespace) -> None:
    """Add the direction pass to a chat run of one model and variant."""
    stem = f"{args.model.replace(':', '_')}+{args.variant}"
    model = Ollama(args.ollama, args.model)
    try:
        asked = direct_variant(
            args.dir / "packet.jsonl",
            args.dir / f"{stem}.jsonl",
            args.dir / f"{stem}-directed.jsonl",
            model,
        )
    finally:
        model.close()
    print(f"asked direction for {asked} items", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("packet")
    add_sample_args(pk)
    pk.add_argument("--size", type=int, default=300)
    pk.add_argument("--dir", type=Path, required=True)
    rn = sub.add_parser("run")
    rn.add_argument("--dir", type=Path, required=True)
    rn.add_argument("--ollama", default="http://localhost:11435")
    rn.add_argument("--model", required=True)
    rn.add_argument("--variant", default="v2", choices=RUN_VARIANTS)
    dr = sub.add_parser("direct")
    dr.add_argument("--dir", type=Path, required=True)
    dr.add_argument("--ollama", default="http://localhost:11435")
    dr.add_argument("--model", required=True)
    dr.add_argument("--variant", default="v2", choices=sorted(VARIANTS))
    bk = sub.add_parser("bulk")
    bk.add_argument("--dir", type=Path, required=True)
    bk.add_argument("--labels", type=Path, required=True)
    bk.add_argument("--labeler", default="gemma4:31b+contrast")
    sc = sub.add_parser("score")
    sc.add_argument("--dir", type=Path, required=True)
    sc.add_argument("--split", default="dev", choices=("dev", "test"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "packet":
        items = sampled_items(
            args.surges,
            args.cache,
            sample_size=args.sample_size,
            per_surge=args.per_surge,
            seed=args.seed,
            include_warmup=args.include_warmup,
        )
        store = LabelStore(args.labels)
        labels = store.labeled(args.labeler)
        store.close()
        written = write_packet(items, labels, args.dir, size=args.size, seed=args.seed)
        print(f"wrote {written} items to {args.dir}", file=sys.stderr)
    elif args.command == "run":
        run(args)
    elif args.command == "direct":
        direct(args)
    elif args.command == "bulk":
        for path in write_bulk(args.dir, args.labels, args.labeler):
            print(f"wrote {path}", file=sys.stderr)
    else:
        print("\n".join(score(args.dir, split=args.split)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

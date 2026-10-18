"""Confirm the bulk labeler's hostile calls with the two gold judges.

The local labeler works as a high-recall screen: on the interim network packet
it caught 11 of the 12 consensus-hostile replies, but only 11 of its 26
hostile calls were consensus-hostile. `packet` writes a blind judge packet of
every author-directed hostile call the judges have not seen yet, shuffled with
random decoys from the rest, so the judges cannot infer the base rate and the
screen's misses can be estimated. Once the Opus and Fable judges have written
`opus.jsonl` and `fable.jsonl`, `merge` stores a `<labeler>@confirmed`
labeler. Each judged hostile call takes the ordinal median of the screen and
the two judges, so it stays hostile when at least one judge agrees; every
other item keeps the screen's label. `merge` also reports how many calls were
confirmed and how many hostile replies the screen likely missed.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from pileon_lab.codebook import V2_LABELS
from pileon_lab.evalset import (
    HOSTILE,
    JUDGES,
    add_sample_args,
    read_labels,
    sampled_items,
    write_items,
)
from pileon_lab.label import LabelStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pileon_lab.label import Item


def flagged(labels: dict[str, str], directed: dict[str, str]) -> set[str]:
    """Items the screen calls hostile or abusive toward the author."""
    return {
        uri
        for uri, label in labels.items()
        if label in HOSTILE and directed.get(uri) == "author"
    }


def judged_items(dirs: Sequence[Path]) -> dict[str, tuple[str, ...]]:
    """URI -> the judges' labels, for items every judge labeled."""
    out: dict[str, tuple[str, ...]] = {}
    for d in dirs:
        key: dict[str, str] = json.loads((d / "key.json").read_text())
        judges = [read_labels(d / f"{name}.jsonl") for name in JUDGES]
        for item_id, uri in key.items():
            if all(item_id in j for j in judges):
                out[uri] = tuple(j[item_id] for j in judges)
    return out


DECOY_KINDS = ("toxic", "decoy")
"""Decoy strata: the most toxic-looking unflagged items, then random others."""


def decoy_uris(directory: Path) -> dict[str, set[str]]:
    """URIs `packet` drew as decoys into `directory`, by stratum."""
    out: dict[str, set[str]] = {kind: set() for kind in DECOY_KINDS}
    strata = directory / "strata.json"
    if strata.exists():
        key: dict[str, str] = json.loads((directory / "key.json").read_text())
        kinds: dict[str, str] = json.loads(strata.read_text())
        for item_id, kind in kinds.items():
            if kind in out:
                out[kind].add(key[item_id])
    return out


def write_confirm_packet(  # noqa: PLR0913 - one call site, all keywords
    items: list[Item],
    labels: dict[str, str],
    directed: dict[str, str],
    outdir: Path,
    *,
    judged: set[str],
    decoys: int,
    seed: int,
    toxicity: dict[str, float] | None = None,
    toxic: int = 0,
    toxic_pool: int = 0,
) -> Counter[str]:
    """Write every unjudged hostile call plus two strata of decoys.

    With `toxicity` scores, the `toxic_pool` highest-scoring unflagged items
    form the toxic stratum and `toxic` of them are drawn; `decoys` more come
    at random from the rest. `pools.json` records both strata's sizes, so
    `merge` can weight each stratum's miss rate by its size.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling
    hits = flagged(labels, directed)
    fresh = list({i.uri: i for i in items if i.uri not in judged}.values())
    calls = [i for i in fresh if i.uri in hits]
    pool = [i for i in fresh if i.uri not in hits and labels.get(i.uri) in V2_LABELS]
    scores = toxicity or {}
    top = (
        sorted(pool, key=lambda i: -scores.get(i.uri, 0.0))[:toxic_pool]
        if scores
        else []
    )
    top_uris = {i.uri for i in top}
    rest = [i for i in pool if i.uri not in top_uris]
    rng.shuffle(top)
    rng.shuffle(rest)
    chosen = calls + top[:toxic] + rest[:decoys]
    rng.shuffle(chosen)
    key = write_items(chosen, outdir)
    kind_of = {i.uri: "flagged" for i in calls} | {i.uri: "toxic" for i in top}
    strata = {i: kind_of.get(uri, "decoy") for i, uri in key.items()}
    (outdir / "strata.json").write_text(json.dumps(strata, indent=1))
    (outdir / "pools.json").write_text(
        json.dumps({"toxic": len(top), "decoy": len(rest)})
    )
    return Counter(strata.values())


def median(labels: Sequence[str]) -> str:
    """Ordinal median on the codebook's scale."""
    ranked = sorted(labels, key=V2_LABELS.index)
    return ranked[len(ranked) // 2]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def merge(
    store: LabelStore, labeler: str, dirs: Sequence[Path]
) -> tuple[str, list[str]]:
    """Store `<labeler>@confirmed` and report confirmation and miss rates."""
    labels, directed = store.labeled(labeler), store.directed(labeler)
    judged = judged_items(dirs)
    hits = flagged(labels, directed)
    dest = f"{labeler}@confirmed"
    store.clone(labeler, dest)
    medians = {}
    for uri in sorted(hits & judged.keys()):
        medians[uri] = median((labels[uri], *judged[uri]))
        if medians[uri] != labels[uri]:
            store.set_label(uri, dest, medians[uri])
    kept = sum(label in HOSTILE for label in medians.values())
    both = sum(all(j in HOSTILE for j in judged[u]) for u in medians)
    lo, hi = wilson(kept, len(medians))
    rate = kept / max(len(medians), 1)
    now = Counter(medians.values()).most_common()
    lines = [
        (
            f"{labeler}: {len(hits)} author-directed hostile calls, "
            f"{len(medians)} judged"
        ),
        (
            f"  kept hostile (at least one judge agrees): {kept} ({rate:.0%}, "
            f"95% CI {lo:.0%}-{hi:.0%}); both judges: {both}"
        ),
        "  judged calls now: " + ", ".join(f"{k} {v}" for k, v in now),
    ]
    unflagged = sum(
        1 for u, label in labels.items() if u not in hits and label in V2_LABELS
    )
    for directory in dirs:
        lines += miss_estimate(directory, judged, labels, unflagged=unflagged)
    if len(medians) < len(hits):
        lines.append(f"  {len(hits) - len(medians)} calls not yet judged keep it")
    return dest, lines


def miss_estimate(
    directory: Path,
    judged: dict[str, tuple[str, ...]],
    labels: dict[str, str],
    *,
    unflagged: int,
) -> list[str]:
    """Hostile replies the screen missed, estimated from one packet's decoys.

    Each stratum's rate (both judges hostile) is scaled by the stratum's size
    from `pools.json`; a packet without it has random decoys over every
    unflagged item. The interval adds the strata's Wilson bounds, which is
    conservative.
    """
    drawn = decoy_uris(directory)
    pools_path = directory / "pools.json"
    pools: dict[str, int] = (
        json.loads(pools_path.read_text())
        if pools_path.exists()
        else {"toxic": 0, "decoy": unflagged}
    )
    lines, total, low, high = [], 0.0, 0.0, 0.0
    for kind in DECOY_KINDS:
        sample = [u for u in drawn[kind] if u in judged and u in labels]
        if not sample:
            continue
        agreed = sum(all(j in HOSTILE for j in judged[u]) for u in sample)
        either = sum(any(j in HOSTILE for j in judged[u]) for u in sample)
        size = pools.get(kind, 0)
        lo, hi = wilson(agreed, len(sample))
        total += agreed / len(sample) * size
        low += lo * size
        high += hi * size
        lines.append(
            f"  {directory.name} {kind} decoys: {len(sample)} judged of {size}; "
            f"both judges hostile {agreed}, either {either}"
        )
    if lines:
        lines.append(
            f"  {directory.name} misses ~{total:.0f} (95% CI {low:.0f}-{high:.0f})"
        )
    return lines


def toxicity_scores(items: list[Item]) -> dict[str, float]:
    """toxic-bert's insult probability per item (needs the `baselines` group)."""
    from pileon_lab.baselines import BASELINES, run_baseline  # noqa: PLC0415

    insult = next(b for b in BASELINES if b.name == "toxic-bert insult")
    unique = list({i.uri: i for i in items}.values())
    scores = run_baseline(insult, [i.text for i in unique])
    return {i.uri: s for i, s in zip(unique, scores, strict=True)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("packet")
    add_sample_args(pk)
    pk.add_argument("--decoys", type=int, default=120)
    pk.add_argument("--toxic", type=int, default=80, help="0 skips the toxic stratum")
    pk.add_argument("--toxic-pool", type=int, default=400)
    pk.add_argument("--judged", type=Path, nargs="*", default=[])
    pk.add_argument("--dir", type=Path, required=True)
    mg = sub.add_parser("merge")
    mg.add_argument("--labels", type=Path, required=True)
    mg.add_argument("--labeler", default="gemma4:31b+contrast")
    mg.add_argument("--judged", type=Path, nargs="+", required=True)
    args = parser.parse_args(argv)

    store = LabelStore(args.labels)
    try:
        if args.command == "packet":
            items = sampled_items(
                args.surges,
                args.cache,
                sample_size=args.sample_size,
                per_surge=args.per_surge,
                seed=args.seed,
                include_warmup=args.include_warmup,
            )
            labels = store.labeled(args.labeler)
            directed = store.directed(args.labeler)
            hits = flagged(labels, directed)
            toxicity = (
                toxicity_scores([i for i in items if i.uri not in hits])
                if args.toxic
                else None
            )
            counts = write_confirm_packet(
                items,
                labels,
                directed,
                args.dir,
                judged=set(judged_items(args.judged)),
                decoys=args.decoys,
                seed=args.seed,
                toxicity=toxicity,
                toxic=args.toxic,
                toxic_pool=args.toxic_pool,
            )
            print(
                f"wrote {counts['flagged']} hostile calls, {counts['toxic']} toxic "
                f"and {counts['decoy']} random decoys to {args.dir}",
                file=sys.stderr,
            )
        else:
            dest, lines = merge(store, args.labeler, args.judged)
            print("\n".join(lines))
            print(f"stored {dest}", file=sys.stderr)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

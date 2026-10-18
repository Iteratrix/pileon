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


def decoy_uris(dirs: Sequence[Path]) -> set[str]:
    """URIs drawn as random decoys by `packet` (not by any stratified packet)."""
    out = set()
    for d in dirs:
        strata = d / "strata.json"
        if not strata.exists():
            continue
        key: dict[str, str] = json.loads((d / "key.json").read_text())
        kinds: dict[str, str] = json.loads(strata.read_text())
        out |= {key[i] for i, kind in kinds.items() if kind == "decoy"}
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
) -> Counter[str]:
    """Write every unjudged hostile call plus `decoys` random other items."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling
    hits = flagged(labels, directed)
    fresh = list({i.uri: i for i in items if i.uri not in judged}.values())
    calls = [i for i in fresh if i.uri in hits]
    pool = [i for i in fresh if i.uri not in hits and labels.get(i.uri) in V2_LABELS]
    rng.shuffle(pool)
    chosen = calls + pool[:decoys]
    rng.shuffle(chosen)
    key = write_items(chosen, outdir)
    strata = {i: "flagged" if uri in hits else "decoy" for i, uri in key.items()}
    (outdir / "strata.json").write_text(json.dumps(strata, indent=1))
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
    decoys = [u for u in decoy_uris(dirs) if u in judged and u in labels]
    if decoys:
        either = sum(any(j in HOSTILE for j in judged[u]) for u in decoys)
        agreed = sum(all(j in HOSTILE for j in judged[u]) for u in decoys)
        unflagged = sum(
            1 for u, label in labels.items() if u not in hits and label in V2_LABELS
        )
        lo, hi = wilson(agreed, len(decoys))
        lines.append(
            f"  decoys: {len(decoys)} judged; both judges hostile {agreed}, "
            f"either {either}; misses among {unflagged} unflagged items "
            f"~{agreed / len(decoys) * unflagged:.0f} "
            f"(95% CI {lo * unflagged:.0f}-{hi * unflagged:.0f})"
        )
    if len(medians) < len(hits):
        lines.append(f"  {len(hits) - len(medians)} calls not yet judged keep it")
    return dest, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("packet")
    add_sample_args(pk)
    pk.add_argument("--decoys", type=int, default=120)
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
            counts = write_confirm_packet(
                items,
                store.labeled(args.labeler),
                store.directed(args.labeler),
                args.dir,
                judged=set(judged_items(args.judged)),
                decoys=args.decoys,
                seed=args.seed,
            )
            print(
                f"wrote {counts['flagged']} hostile calls and "
                f"{counts['decoy']} decoys to {args.dir}",
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

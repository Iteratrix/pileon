"""Calibrate LLM gold judges against the study's human gold labels.

Before an LLM judge's labels count as gold for new items, it has to match the
184 human gold labels from qp-pileon at least as well as the bulk labeler did.
`export` writes a blind packet (opaque ids, shuffled, no labels) plus the
exact codebook the bulk labeler used; `score` compares each judge's answers
to the human labels with the study's own kappa code, alongside the bulk
labeler's v2 labels on the same items.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

from qp_pileon.calibrate import cohens_kappa, weighted_kappa
from qp_pileon.targets import NON_PERSON
from qp_pileon.valence import VALENCE_PROMPT_V2, map_v2_to_gold

BULK_GOLD_TAG = "qwen3.5:27b-mlx+v2gold"
V2_LABELS = ("supportive", "neutral", "critical", "hostile", "abusive")
SEED = 20261016


def codebook() -> str:
    """The v2 codebook exactly as the bulk labeler saw it, minus the I/O lines."""
    return VALENCE_PROMPT_V2.split("Reply with ONLY")[0].rstrip() + "\n"


def gold_labels(db: Path, extra: Path | None) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        gold = dict(conn.execute("SELECT item_uri, label FROM gold_human"))
    finally:
        conn.close()
    if extra is not None:
        gold |= json.loads(extra.read_text())
    return gold


def contexts(db: Path, uris: list[str]) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(uris), 500):
            batch = uris[start : start + 500]
            marks = ",".join("?" * len(batch))
            rows = conn.execute(
                "SELECT t.item_uri, COALESCE(p.handle, t.target_did), t.text, "  # noqa: S608
                "t.target_post, t.framing, COALESCE(p.target_type, ''), t.role "
                "FROM text_items t LEFT JOIN profiles p ON p.did = t.target_did "
                f"WHERE t.item_uri IN ({marks})",
                batch,
            )
            for uri, handle, text, target_post, framing, kind, role in rows:
                out[uri] = {
                    "target": handle,
                    "reply": text,
                    "target_post": target_post,
                    "framing": framing,
                    "person": role == "response" and kind not in NON_PERSON,
                }
        return out
    finally:
        conn.close()


def export(db: Path, extra: Path | None, outdir: Path) -> int:
    gold = gold_labels(db, extra)
    ctx = contexts(db, sorted(gold))
    uris = sorted(u for u in gold if u in ctx)
    random.Random(SEED).shuffle(uris)  # noqa: S311 - order blinding, not secrecy
    outdir.mkdir(parents=True, exist_ok=True)
    key = {}
    with (outdir / "packet.jsonl").open("w") as packet:
        for n, uri in enumerate(uris, start=1):
            item_id = f"g{n:03d}"
            key[item_id] = uri
            c = ctx[uri]
            packet.write(
                json.dumps(
                    {
                        "id": item_id,
                        "target": c["target"],
                        "framing": c["framing"],
                        "target_post": c["target_post"],
                        "reply": c["reply"],
                    }
                )
                + "\n"
            )
    (outdir / "key.json").write_text(json.dumps(key, indent=1))
    (outdir / "codebook.md").write_text(codebook())
    print(f"exported {len(uris)} items to {outdir}", file=sys.stderr)
    return len(uris)


def read_judgments(path: Path) -> dict[str, str]:
    labels = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("label") in V2_LABELS:
            labels[row["id"]] = row["label"]
    return labels


def report(name: str, pairs: list[tuple[str, str]]) -> str:
    agree = sum(a == b for a, b in pairs) / len(pairs) if pairs else 0.0
    return (
        f"{name:<28} n={len(pairs):>3}  agree {agree:4.0%}  "
        f"kappa {cohens_kappa(pairs):.3f}  weighted {weighted_kappa(pairs):.3f}"
    )


def score(db: Path, extra: Path | None, outdir: Path, judges: list[Path]) -> None:
    gold = gold_labels(db, extra)
    key: dict[str, str] = json.loads((outdir / "key.json").read_text())
    ctx = contexts(db, sorted(gold))
    persons = {i for i, u in key.items() if ctx.get(u, {}).get("person")}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        bulk_by_uri = dict(
            conn.execute(
                "SELECT item_uri, label FROM valence_labels WHERE model = ?",
                (BULK_GOLD_TAG,),
            )
        )
    finally:
        conn.close()
    bulk = {i: bulk_by_uri[u] for i, u in key.items() if u in bulk_by_uri}
    named = {path.stem: read_judgments(path) for path in judges}
    common = persons & set(bulk)
    for labels in named.values():
        common &= set(labels)
    ids = sorted(common)
    print(f"persons-only items labeled by everyone: {len(ids)}")
    print(
        report(
            "bulk labeler (qwen v2)",
            [(map_v2_to_gold(bulk[i]), gold[key[i]]) for i in ids],
        )
    )
    for name, labels in named.items():
        print(report(name, [(map_v2_to_gold(labels[i]), gold[key[i]]) for i in ids]))
    names = list(named)
    for a in range(len(names)):
        for b in range(a + 1, len(names)):
            pairs = [(named[names[a]][i], named[names[b]][i]) for i in ids]
            print(
                f"{names[a]} vs {names[b]}: agree "
                f"{sum(x == y for x, y in pairs) / len(pairs):.0%}, weighted kappa "
                f"{weighted_kappa(pairs, order=V2_LABELS):.3f} (5-point)"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("export", "score"))
    parser.add_argument("--study-db", type=Path, required=True)
    parser.add_argument("--extra-gold", type=Path)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--judge", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "export":
        export(args.study_db, args.extra_gold, args.dir)
    else:
        score(args.study_db, args.extra_gold, args.dir, args.judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())

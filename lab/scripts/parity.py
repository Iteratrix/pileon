"""Compare the lab's live rows with `pileon-alert score` rows, value for value.

Usage: uv run python scripts/parity.py LAB_ROWS RUST_ROWS [REFERENCE]

Both pipelines read the same cache, so every signal should match exactly
(floats to 1e-12). With a reference, the lab's score is recomputed with
`Reference.score` and compared with Rust's. Prints mismatches and exits 1 if
there are any.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from pileon_lab.detector import LIVE_FAMILIES, Reference

SIGNALS = ["live_cut_s", "live_arrivals"] + [
    s for family in LIVE_FAMILIES.values() for s in family
]


def same(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def main() -> int:
    lab_path, rust_path = Path(sys.argv[1]), Path(sys.argv[2])
    reference = None
    if len(sys.argv) > 3:
        reference = Reference.from_json(Path(sys.argv[3]).read_text())
    lab = {r["incident_id"]: r for r in map(json.loads, lab_path.open())}
    rust = {r["incident_id"]: r for r in map(json.loads, rust_path.open())}
    shared = sorted(lab.keys() & rust.keys())
    print(
        f"{len(shared)} shared rows; lab only {len(lab.keys() - rust.keys())}, "
        f"rust only {len(rust.keys() - lab.keys())}"
    )
    bad = 0
    for surge_id in shared:
        diffs = [
            f"{s}: lab {lab[surge_id].get(s)!r} rust {rust[surge_id].get(s)!r}"
            for s in SIGNALS
            if not same(lab[surge_id].get(s), rust[surge_id].get(s))
        ]
        if reference is not None:
            score, _ = reference.score(lab[surge_id])
            if not same(score, rust[surge_id].get("score")):
                diffs.append(
                    f"score: lab {score!r} rust {rust[surge_id].get('score')!r}"
                )
        if diffs:
            bad += 1
            if bad <= 10:
                print(surge_id, "; ".join(diffs))
    print(f"{bad} of {len(shared)} rows differ")
    return 1 if bad or not shared else 0


if __name__ == "__main__":
    sys.exit(main())

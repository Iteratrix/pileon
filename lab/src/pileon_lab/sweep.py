"""De-identified sweep data and a first ranking of the dimensions.

The sweep page recomputes every curve in the browser, so it needs per-incident
rows, but nothing that points at a person: no incident ids, no dates, and
follower counts rounded to two significant figures. The ranking here is the
area under the ROC curve (AUC) of each dimension for one outcome: 0.5 means
the dimension does not separate pile-ons from other incidents at all.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

DIMENSIONS: dict[str, str] = {
    "reply_lift_median": "Replies on the target post vs the target's median post",
    "reply_lift_p90": "Replies vs the target's 90th-percentile post",
    "reply_vs_record": "Replies vs the target's previous high (last 200 posts)",
    "mix_shift": "(Replies+quotes)/(likes+reposts) vs the target's median post",
    "window_lift_median": "Arrivals in 48 h vs the target's median replies+quotes",
    "burst_ratio": "Arrivals in the 48 h after vs the 48 h before",
    "follower_ratio_log10": "Quoter followers / target followers (log10)",
    "target_followers": "Target followers",
    "follow_amplifier": "Share of arrivals who followed the quoter beforehand",
    "follow_amplifier_after": "Share of arrivals who followed the quoter afterwards",
    "follow_target": "Share of arrivals who followed the target beforehand",
    "target_follows": "Share of arrivals the target follows",
    "strangers_to_both": "Share of arrivals following neither side",
    "within_15m": "Share of arrivals in the first 15 minutes",
    "within_1h": "Share of arrivals in the first hour",
    "amplifier_follows_target": "Quoter followed the target beforehand",
    "target_follows_amplifier": "Target followed the quoter beforehand",
    "arrivals": "Distinct arrivals in 48 h (absolute)",
}

OUTCOMES = (
    "contempt",
    "critical_or_worse",
    "contempt_share",
    "supportive",
    "labeled",
    "group_labeled",
    "group_contempt",
)
CONTEXT = ("source", "target_post_found", "target_post_is_reply", "baseline_posts")


def round_sig(value: float, digits: int = 2) -> float:
    """Round to `digits` significant figures (0 stays 0)."""
    if value == 0:
        return 0.0
    return round(value, -math.floor(math.log10(abs(value))) + digits - 1)


def deidentify(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (*DIMENSIONS, *OUTCOMES, *CONTEXT):
        value = row.get(key)
        if key == "target_followers" and isinstance(value, int):
            value = round_sig(value)
        elif isinstance(value, float):
            value = round(value, 4)
        out[key] = value
    return out


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC: P(score of a positive > score of a negative), ties half."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    ranked = sorted(zip(scores, labels, strict=True), key=lambda pair: pair[0])
    rank_sum = 0.0
    i = 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        mid_rank = (i + j + 1) / 2
        rank_sum += mid_rank * sum(1 for k in range(i, j) if ranked[k][1])
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def ranking(
    rows: list[dict[str, Any]],
    *,
    outcome: str,
    threshold: float,
    min_labeled: int = 0,
) -> list[tuple[str, float, int]]:
    """Return (dimension, AUC, n) sorted by separation strength.

    Rows with fewer than `min_labeled` labeled responses are left out, not
    counted as negatives: a share outcome says nothing about them.
    """
    eligible = [r for r in rows if (r.get("labeled") or 0) >= min_labeled]
    result = []
    for dimension in DIMENSIONS:
        pairs = [
            (float(r[dimension]), float(r[outcome]) >= threshold)
            for r in eligible
            if r.get(dimension) is not None and r.get(outcome) is not None
        ]
        score = auc([p[0] for p in pairs], [p[1] for p in pairs])
        if score is not None:
            result.append((dimension, score, len(pairs)))
    return sorted(result, key=lambda item: abs(item[1] - 0.5), reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write de-identified sweep data")
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--outcome", default="contempt")
    parser.add_argument("--threshold", type=float, default=3)
    parser.add_argument("--min-labeled", type=int, default=0)
    parser.add_argument("--template", type=Path, help="page template to fill")
    parser.add_argument("--page", type=Path, help="where to write the filled page")
    args = parser.parse_args(argv)

    rows = [json.loads(line) for line in args.features.read_text().splitlines()]
    eligible = [r for r in rows if (r.get("labeled") or 0) >= args.min_labeled]
    positives = sum(1 for r in eligible if (r.get(args.outcome) or 0) >= args.threshold)
    print(
        f"{len(eligible)} incidents with {args.min_labeled}+ labeled; "
        f"{positives} with {args.outcome} >= {args.threshold}",
        file=sys.stderr,
    )
    for dimension, score, n in ranking(
        rows,
        outcome=args.outcome,
        threshold=args.threshold,
        min_labeled=args.min_labeled,
    ):
        print(f"  AUC {score:.3f}  n={n:>5}  {dimension}", file=sys.stderr)
    payload = {
        "dimensions": DIMENSIONS,
        "rows": [deidentify(r) for r in rows],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"))
    args.out.write_text(data)
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e3:.0f} kB)", file=sys.stderr)
    if args.template and args.page:
        page = fill_template(args.template.read_text(), data)
        args.page.parent.mkdir(parents=True, exist_ok=True)
        args.page.write_text(page)
        print(f"wrote {args.page}", file=sys.stderr)
    return 0


PLACEHOLDER = "__SWEEP_DATA__"


def fill_template(template: str, data: str) -> str:
    """Embed JSON in the page's data script; `</` is escaped so no value can
    close the script element."""
    if template.count(PLACEHOLDER) != 1:
        msg = f"template must contain {PLACEHOLDER} exactly once"
        raise ValueError(msg)
    return template.replace(PLACEHOLDER, data.replace("</", "<\\/"))


if __name__ == "__main__":
    sys.exit(main())

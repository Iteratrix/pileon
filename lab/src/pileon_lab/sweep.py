"""De-identified sweep data and a first ranking of the dimensions.

The sweep page recomputes every curve in the browser, so it needs per-incident
rows, but nothing that points at a person: no incident ids, no dates, and
follower counts rounded to two significant figures. The ranking here is the
area under the ROC curve (AUC) of each dimension for one outcome: 0.5 means
the dimension does not separate pile-ons from other incidents at all.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

DIMENSIONS: dict[str, str] = {
    "reply_lift_median": "Replies on the target post vs the target's median post",
    "reply_lift_p90": "Replies vs the target's 90th-percentile post",
    "reply_vs_record": "Replies vs the target's previous high (last 200 posts)",
    "mix_shift": "(Replies+quotes)/(likes+reposts) vs the target's median post",
    "like_reply_ratio": "Likes per reply on the target post",
    "like_reply_shift": "Likes per reply vs the target's median post",
    "arrivals_liked_target": "Share of arrivals who like the target post",
    "arrivals_liked_before": "Share of arrivals who liked it before the quote",
    "top_response_ratio": "Likes on the top reply or quote vs the target post",
    "responses_outliking_target": "Share of replies and quotes out-liking the target",
    "dunk_ratio": "Likes on the quoter's post vs the target post",
    "contempt_like_lift": "Likes per contempt reply vs per other reply (uses labels)",
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
    "like_group_labeled",
    "like_group_contempt",
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


Row = dict[str, Any]

SURGE_CLASSES = ("pileon", "rally", "crossfire", "spotlight")


def surge_class(row: Row, *, hostile_at: float, support_at: float) -> str:
    """Sort a judged incident into a surge class by its reply shares."""
    labeled = row.get("labeled") or 0
    hostile = (row.get("contempt") or 0) / labeled >= hostile_at
    support = (row.get("supportive") or 0) / labeled >= support_at
    if hostile:
        return "crossfire" if support else "pileon"
    return "rally" if support else "spotlight"


def ranking(
    rows: list[Row], *, positive: Callable[[Row], bool]
) -> list[tuple[str, float, int]]:
    """Return (dimension, AUC, n) sorted by separation strength."""
    result = []
    for dimension in DIMENSIONS:
        pairs = [
            (float(r[dimension]), positive(r))
            for r in rows
            if r.get(dimension) is not None
        ]
        score = auc([p[0] for p in pairs], [p[1] for p in pairs])
        if score is not None:
            result.append((dimension, score, len(pairs)))
    return sorted(result, key=lambda item: abs(item[1] - 0.5), reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write de-identified sweep data")
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--outcome",
        default="pileon",
        help="a surge class (pileon, rally, crossfire, spotlight) or a count field",
    )
    parser.add_argument("--threshold", type=float, default=3, help="for counts")
    parser.add_argument("--hostile-at", type=float, default=0.10)
    parser.add_argument("--support-at", type=float, default=0.25)
    parser.add_argument("--min-labeled", type=int, default=10, help="for classes")
    parser.add_argument("--template", type=Path, help="page template to fill")
    parser.add_argument("--page", type=Path, help="where to write the filled page")
    parser.add_argument(
        "--corpus-note",
        default="Incidents come from a private quote-post study database.",
        help="one or two sentences describing where the incidents come from",
    )
    args = parser.parse_args(argv)

    rows = [json.loads(line) for line in args.features.read_text().splitlines()]
    if args.outcome in SURGE_CLASSES:
        judged = [r for r in rows if (r.get("labeled") or 0) >= args.min_labeled]

        def positive(row: Row) -> bool:
            kind = surge_class(
                row, hostile_at=args.hostile_at, support_at=args.support_at
            )
            return bool(kind == args.outcome)

    else:
        judged = rows

        def positive(row: Row) -> bool:
            return bool((row.get(args.outcome) or 0) >= args.threshold)

    print(
        f"{len(judged)} incidents judged; "
        f"{sum(positive(r) for r in judged)} are {args.outcome}",
        file=sys.stderr,
    )
    for dimension, score, n in ranking(judged, positive=positive):
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
        page = fill_template(args.template.read_text(), data, note=args.corpus_note)
        args.page.parent.mkdir(parents=True, exist_ok=True)
        args.page.write_text(page)
        print(f"wrote {args.page}", file=sys.stderr)
    return 0


PLACEHOLDER = "__SWEEP_DATA__"
NOTE_PLACEHOLDER = "__CORPUS_NOTE__"


def fill_template(template: str, data: str, *, note: str = "") -> str:
    """Embed JSON in the page's data script and the corpus note in its footer.

    `</` in the data is escaped so no value can close the script element; the
    note is HTML-escaped.
    """
    if template.count(PLACEHOLDER) != 1:
        msg = f"template must contain {PLACEHOLDER} exactly once"
        raise ValueError(msg)
    page = template.replace(PLACEHOLDER, data.replace("</", "<\\/"))
    return page.replace(NOTE_PLACEHOLDER, html.escape(note))


if __name__ == "__main__":
    sys.exit(main())

"""Natural experiment: do surges slow after a target detaches a quote?

Run from lab/ after scripts/postgate_events.py:
    uv run python scripts/natural_detach.py [EVENTS] [all|r]
EVENTS is postgates-detached.jsonl (default) or postgates-quotes-disabled.jsonl.
Treated surges are matched on arrivals in each of the two hours before the
event and on cumulative arrivals; a placebo run checks the matching.
"""

import bisect
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pileon_lab.surges import read_surges, size_bucket

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "data/lab"
HOUR, WINDOW, LATE = 3600, 6 * 3600, 48 * 3600


def when(row):
    ts = row["ts"]
    if row.get("created_at"):
        try:
            c = (
                datetime.fromisoformat(row["created_at"].replace(" ", "T")).timestamp()
                * 1e6
            )
            if abs(c - ts) < HOUR * 1e6:
                return int(c)
        except ValueError:
            pass
    return ts


events = defaultdict(list)
SRC = sys.argv[1] if len(sys.argv) > 1 else "postgates-detached.jsonl"
KIND = sys.argv[2] if len(sys.argv) > 2 else "all"
with (D / "detach" / SRC).open() as source:
    for line in source:
        r = json.loads(line)
        events[r["post"]].append((when(r), set(r["detached"])))
for v in events.values():
    v.sort(key=lambda e: e[0])

treated, pool = [], defaultdict(list)
for s in read_surges(ROOT / "data/surges/network-2026-10-17.jsonl.zst"):
    if s.warmup or not s.target_posts:
        continue
    offs = sorted(
        a.offset_s
        for a in s.recorded
        if a.offset_s >= 0 and a.author != s.target and KIND in {"all", a.kind}
    )
    if len(offs) < 10:
        continue
    quotes = {q.uri for q in s.quote_posts}
    amp = max(s.quote_posts, key=lambda q: q.engagement).uri if s.quote_posts else None
    hit = None
    touched = False
    for uri, _ in s.target_posts:
        for t, det in events.get(uri, []):
            touched = True
            tau = (t - s.onset_us) / 1e6
            if 0 <= tau <= LATE and (det & quotes or "*" in det):
                if hit is None or tau < hit[0]:
                    hit = (tau, amp in det)
                break
    entry = (s.surge_id, offs, size_bucket(s.strangers))
    if hit:
        treated.append((*entry, hit[0], hit[1]))
    elif not touched:
        pool[entry[2]].append(entry)


def counts(offs, tau):
    lo2, lo, mid, hi = (
        bisect.bisect_left(offs, x)
        for x in (tau - 2 * HOUR, tau - HOUR, tau, tau + WINDOW)
    )
    return (
        (mid - lo, lo - lo2),
        mid,
        hi - mid,
    )  # (last hour, hour before), cumulative, after


def expected(bucket, tau, pre, cum, rng, k=30):
    cands = []
    for _, offs, _ in pool[bucket]:
        if offs[-1] < tau - HOUR:
            continue
        p, c, a = counts(offs, tau)
        if (
            abs(p[0] - pre[0]) <= max(1, 0.2 * pre[0])
            and abs(p[1] - pre[1]) <= max(1, 0.25 * pre[1])
            and 0.67 * cum <= c <= 1.5 * cum
        ):
            cands.append(a)
    if len(cands) < 5:
        return None
    return statistics.mean(rng.sample(cands, min(k, len(cands))))


def effect(cases, rng):
    obs = exp = 0.0
    n = 0
    per = []
    for _, offs, bucket, tau, _ in cases:
        pre, cum, after = counts(offs, tau)
        e = expected(bucket, tau, pre, cum, rng)
        if e is None or e == 0:
            continue
        obs += after
        exp += e
        n += 1
        per.append((after, e))
    return n, obs, exp, per


def boot(per, rng, draws=1000):
    vals = []
    for _ in range(draws):
        smp = [per[rng.randrange(len(per))] for _ in per]
        e = sum(x for _, x in smp)
        if e:
            vals.append(sum(o for o, _ in smp) / e)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


rng = random.Random(7)
print(
    f"treated surges (a quote detached within 48 h of onset): {len(treated)}; "
    f"amplifier detached: {sum(t[4] for t in treated)}"
)
for label, cases in [
    ("all", treated),
    ("amplifier detached", [t for t in treated if t[4]]),
    ("other quote detached", [t for t in treated if not t[4]]),
    ("detached within 2 h", [t for t in treated if t[3] <= 2 * HOUR]),
    ("detached after 2 h", [t for t in treated if t[3] > 2 * HOUR]),
]:
    n, obs, exp, per = effect(cases, rng)
    if n:
        lo, hi = boot(per, rng)
        print(
            f"{label:24} n={n:>4} arrivals next 6 h: observed {obs:.0f} "
            f"vs expected {exp:.0f}; ratio {obs / exp:.2f} (95% {lo:.2f}-{hi:.2f})"
        )
taus = [t[3] for t in treated]
placebo = []
for entries in pool.values():
    for sid, offs, b in rng.sample(entries, min(len(entries), 150)):
        placebo.append((sid, offs, b, rng.choice(taus), False))
n, obs, exp, per = effect(placebo, rng)
lo, hi = boot(per, rng)
print(
    f"{'placebo (no detach)':24} n={n:>4} ratio {obs / exp:.2f} (95% {lo:.2f}-{hi:.2f})"
)
print("median detach delay after onset, h:", round(statistics.median(taus) / HOUR, 2))

# Extract postgate events for the detach natural experiment.
# Run from lab/: uv run --with duckdb python scripts/postgate_events.py
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
GLOB = str(ROOT / "data/jetstream/*/postgates/*/*.jsonl.zst")
OUT = ROOT / "data/lab/detach"
OUT.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
SRC = (
    f"read_json('{GLOB}', format='newline_delimited', "
    "union_by_name=true, ignore_errors=true)"
)
for name, where, det in (
    (
        "postgates-detached.jsonl",
        "detached IS NOT NULL AND len(detached) > 0",
        "detached",
    ),
    ("postgates-quotes-disabled.jsonl", "quotes_disabled = true", None),
):
    cols = "post, ts, created_at" + (f", {det}" if det else "")
    rows = con.execute(f"SELECT {cols} FROM {SRC} WHERE {where}").fetchall()
    with (OUT / name).open("w") as f:
        for row in rows:
            detached = list(row[3]) if det else ["*"]
            created = str(row[2]) if row[2] else None
            f.write(
                json.dumps(
                    {
                        "post": row[0],
                        "ts": row[1],
                        "created_at": created,
                        "detached": detached,
                    }
                )
                + "\n"
            )
    print(name, len(rows))

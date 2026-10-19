from __future__ import annotations

from datetime import UTC, datetime

from pileon_lab.detector import Reference
from pileon_lab.initiators import Cutoffs, likely, nominate

DAY_US = 86_400_000_000
END = datetime(2026, 10, 18, tzinfo=UTC)
END_US = int(END.timestamp() * 1e6)
REFERENCE = Reference(
    "retro",
    {
        "like_reply_ratio": [float(i) for i in range(11)],
        "follower_ratio_log10": [float(i) for i in range(11)],
    },
    {},
)
CUTOFFS = Cutoffs(top=0.9, high=0.8, rally_approval=0.1)


def row(n: int, amplifier: str, *, likes: float, ratio: float, days_ago: int) -> dict:
    return {
        "incident_id": f"did:plc:t{n}:{n}",
        "amplifier": amplifier,
        "onset_us": END_US - days_ago * DAY_US,
        "like_reply_ratio": likes,
        "follower_ratio_log10": ratio,
    }


def test_likely_tiers_and_rally_carve_out() -> None:
    assert (
        likely(row(1, "a", likes=-1.0, ratio=11.0, days_ago=1), REFERENCE, CUTOFFS)
        == "top"
    )
    assert (
        likely(row(1, "a", likes=1.0, ratio=8.0, days_ago=1), REFERENCE, CUTOFFS)
        == "high"
    )
    assert (
        likely(row(1, "a", likes=5.0, ratio=5.0, days_ago=1), REFERENCE, CUTOFFS)
        is None
    )
    rally = row(1, "a", likes=11.0, ratio=11.0, days_ago=1)
    assert likely(rally, REFERENCE, CUTOFFS) is None


def test_nominate_counts_likely_pileons_in_the_window() -> None:
    hot = {"likes": -1.0, "ratio": 11.0}
    rows = [
        row(1, "did:plc:a", **hot, days_ago=1),
        row(2, "did:plc:a", **hot, days_ago=10),
        row(3, "did:plc:a", **hot, days_ago=59),
        row(4, "did:plc:a", **hot, days_ago=61),
        row(5, "did:plc:b", **hot, days_ago=1),
        row(6, "did:plc:b", likes=5.0, ratio=5.0, days_ago=2),
    ]
    found = nominate(rows, REFERENCE, CUTOFFS, min_count=2, end=END, days=60)
    assert [n.account for n in found] == ["did:plc:a"]
    assert found[0].incidents == ("did:plc:t3:3", "did:plc:t2:2", "did:plc:t1:1")
    assert found[0].tiers == {"top": 3}
    assert found[0].targets == 3
    assert (
        nominate(rows, REFERENCE, CUTOFFS, min_count=1, end=END, days=60)[1].account
        == "did:plc:b"
    )

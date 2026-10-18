from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pileon_lab.confirm import (
    median,
    merge,
    miss_estimate,
    wilson,
    write_confirm_packet,
)
from pileon_lab.label import Item, LabelStore

if TYPE_CHECKING:
    from pathlib import Path


def _item(uri: str) -> Item:
    return Item(
        uri=uri, surge_id="s", target_handle="t", text=uri, target_post="", framing=""
    )


def _write(path: Path, labels: dict[str, str]) -> None:
    path.write_text(
        "".join(
            json.dumps({"id": i, "label": label}) + "\n" for i, label in labels.items()
        )
    )


def test_median_keeps_hostility_only_when_a_judge_agrees() -> None:
    assert median(("hostile", "neutral", "supportive")) == "neutral"
    assert median(("hostile", "hostile", "neutral")) == "hostile"
    assert median(("abusive", "hostile", "neutral")) == "hostile"
    assert median(("hostile", "critical", "neutral")) == "critical"


def test_wilson_brackets_the_rate() -> None:
    lo, hi = wilson(11, 26)
    assert lo < 11 / 26 < hi
    assert wilson(0, 0) == (0.0, 1.0)


def test_packet_takes_unjudged_hostile_calls_and_decoys(tmp_path: Path) -> None:
    items = [_item(f"at://{n}") for n in range(10)]
    labels = {f"at://{n}": "neutral" for n in range(10)}
    labels |= {"at://0": "hostile", "at://1": "abusive", "at://2": "hostile"}
    labels["at://9"] = "parse_error"
    directed = {"at://0": "author", "at://1": "author", "at://2": "third_party"}

    counts = write_confirm_packet(
        items, labels, directed, tmp_path, judged={"at://1"}, decoys=3, seed=1
    )

    assert counts == {"flagged": 1, "decoy": 3}
    key = json.loads((tmp_path / "key.json").read_text())
    strata = json.loads((tmp_path / "strata.json").read_text())
    assert {key[i] for i, kind in strata.items() if kind == "flagged"} == {"at://0"}
    assert "at://9" not in key.values()
    packet = (tmp_path / "packet.jsonl").read_text()
    assert "label" not in packet
    assert "author" not in packet


def test_merge_stores_confirmed_labeler(tmp_path: Path) -> None:
    db = tmp_path / "labels.db"
    store = LabelStore(db)
    screen = "m:1+contrast"
    for uri, label in [
        ("at://a", "hostile"),
        ("at://b", "hostile"),
        ("at://c", "abusive"),
        ("at://d", "neutral"),
        ("at://e", "supportive"),
    ]:
        store.save_label(_item(uri), screen, label, 0.9, "{}")
    for uri in ("at://a", "at://b", "at://c"):
        store.save_direction(_item(uri), screen, "author", "{}")
    judged = tmp_path / "confirm"
    judged.mkdir()
    key = {"n1": "at://a", "n2": "at://b", "n3": "at://d"}
    (judged / "key.json").write_text(json.dumps(key))
    (judged / "strata.json").write_text(
        json.dumps({"n1": "flagged", "n2": "flagged", "n3": "decoy"})
    )
    _write(judged / "opus.jsonl", {"n1": "hostile", "n2": "neutral", "n3": "hostile"})
    _write(
        judged / "fable.jsonl", {"n1": "neutral", "n2": "supportive", "n3": "hostile"}
    )

    dest, lines = merge(store, screen, [judged])

    assert dest == "m:1+contrast@confirmed"
    assert store.labeled(dest) == {
        "at://a": "hostile",
        "at://b": "neutral",
        "at://c": "abusive",
        "at://d": "neutral",
        "at://e": "supportive",
    }
    assert store.directed(dest) == store.directed(screen)
    assert store.labeled(screen)["at://b"] == "hostile"
    assert "kept hostile (at least one judge agrees): 1" in lines[1]
    assert "both judges hostile 1" in lines[3]
    assert "confirm misses ~2" in lines[4]
    assert "1 calls not yet judged" in lines[5]
    store.close()


def test_miss_estimate_weights_each_decoy_stratum_by_its_size(tmp_path: Path) -> None:
    key = {f"n{i}": f"at://{i}" for i in range(6)}
    (tmp_path / "key.json").write_text(json.dumps(key))
    strata = {"n0": "toxic", "n1": "toxic", "n2": "decoy", "n3": "decoy", "n4": "decoy"}
    (tmp_path / "strata.json").write_text(json.dumps(strata | {"n5": "flagged"}))
    (tmp_path / "pools.json").write_text(json.dumps({"toxic": 10, "decoy": 1000}))
    judged = {f"at://{i}": ("neutral", "neutral") for i in range(6)}
    judged["at://0"] = ("hostile", "abusive")
    labels = dict.fromkeys(judged, "neutral")
    lines = miss_estimate(tmp_path, judged, labels, unflagged=1010)
    assert "toxic decoys: 2 judged of 10; both judges hostile 1" in lines[0]
    assert "decoy decoys: 3 judged of 1000; both judges hostile 0" in lines[1]
    assert lines[2].endswith("misses ~5 (95% CI 1-571)")

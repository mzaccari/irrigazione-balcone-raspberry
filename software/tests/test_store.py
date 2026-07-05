"""Test dello strato di persistenza (I/O atomico, coda comandi, log eventi)."""

from __future__ import annotations

import json

import store


def test_write_read_roundtrip(tmp_path):
    target = tmp_path / "state.json"
    data = {"a": 1, "nested": {"x": [1, 2, 3]}, "accenti": "erogazione"}
    store.write_json_atomic(target, data)
    assert store.read_json(target) == data


def test_write_atomic_leaves_no_tmp(tmp_path):
    target = tmp_path / "state.json"
    store.write_json_atomic(target, {"v": 1})
    store.write_json_atomic(target, {"v": 2})  # sovrascrive
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert store.read_json(target) == {"v": 2}


def test_read_json_or_missing(tmp_path):
    assert store.read_json_or(tmp_path / "manca.json", {"d": True}) == {"d": True}


def test_read_json_or_corrupt(tmp_path):
    target = tmp_path / "rotto.json"
    target.write_text("{ questo non e json ", encoding="utf-8")
    assert store.read_json_or(target, {"fallback": 1}) == {"fallback": 1}


def test_enqueue_and_drain_returns_all_in_order(tmp_path):
    cmd_dir = tmp_path / "commands"
    for i in range(5):
        store.enqueue_command(cmd_dir, {"type": "on", "pump": f"pompa_{i}"})
    drained = store.drain_commands(cmd_dir)
    assert [c["pump"] for c in drained] == [f"pompa_{i}" for i in range(5)]
    # la coda e vuota dopo il drain
    assert store.drain_commands(cmd_dir) == []


def test_drain_empty_or_missing_dir(tmp_path):
    assert store.drain_commands(tmp_path / "inesistente") == []


def test_drain_rejects_malformed(tmp_path):
    cmd_dir = tmp_path / "commands"
    rejected = cmd_dir / "rejected"
    store.enqueue_command(cmd_dir, {"type": "stop_all"})
    # file malformato con estensione .json
    bad = cmd_dir / "00000000000000000000_999999_bad.json"
    bad.write_text("non json", encoding="utf-8")

    drained = store.drain_commands(cmd_dir, rejected)

    assert [c["type"] for c in drained] == ["stop_all"]
    assert (rejected / bad.name).exists()
    # il malformato non resta nella coda principale
    assert not bad.exists()


def test_drain_rejects_non_object_json(tmp_path):
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir()
    rejected = cmd_dir / "rejected"
    # JSON valido ma non un oggetto -> va rifiutato
    arr = cmd_dir / "00000000000000000000_888888_arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.drain_commands(cmd_dir, rejected) == []
    assert (rejected / arr.name).exists()


def test_append_and_read_events(tmp_path):
    log = tmp_path / "events.jsonl"
    for i in range(3):
        store.append_event(log, {"n": i, "msg": "run"})
    events = store.read_events(log)
    assert [e["n"] for e in events] == [0, 1, 2]


def test_read_events_limit_and_tail(tmp_path):
    log = tmp_path / "events.jsonl"
    for i in range(10):
        store.append_event(log, {"n": i})
    events = store.read_events(log, limit=3)
    assert [e["n"] for e in events] == [7, 8, 9]


def test_read_events_skips_corrupt_lines(tmp_path):
    log = tmp_path / "events.jsonl"
    store.append_event(log, {"n": 0})
    with log.open("a", encoding="utf-8") as handle:
        handle.write("riga corrotta non json\n")
    store.append_event(log, {"n": 1})
    events = store.read_events(log)
    assert [e["n"] for e in events] == [0, 1]


def test_read_events_missing_file(tmp_path):
    assert store.read_events(tmp_path / "manca.jsonl") == []


def test_written_json_is_utf8_readable(tmp_path):
    target = tmp_path / "state.json"
    store.write_json_atomic(target, {"nota": "erogazione perpetua"})
    raw = target.read_text(encoding="utf-8")
    assert "erogazione perpetua" in json.loads(raw)["nota"]

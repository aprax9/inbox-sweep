"""`index` after the first run only applies changes: new, relabelled, removed."""

import json
import time

from inbox_sweep.backends.demo import DemoBackend
from inbox_sweep.cli import main
from inbox_sweep.model import STARRED, Message
from inbox_sweep.store import Store
from inbox_sweep.wizard import _set


def _index(capsys) -> str:
    assert main(["--config", "d/sweep.toml", "index"]) == 0
    return capsys.readouterr().out


def test_second_index_is_a_no_op_then_tracks_changes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    capsys.readouterr()
    with Store(tmp_path / "d" / "index.db") as store:
        total = store.message_count()

    out = _index(capsys)
    assert "0 new" in out and "0 relabelled" in out and "0 removed" in out

    box_path = tmp_path / "d" / "demo-mailbox.json"
    box = DemoBackend(box_path)
    first, second = list(box.iter_messages())[:2]
    box.add_message(Message(id="fresh-1", thread_id="t-fresh", date=int(time.time()),
                            from_email="new.friend@gmail.com", subject="Hi"))
    box.set_labels(first.id, add=[STARRED])
    data = json.loads(box_path.read_text(encoding="utf-8"))
    data["messages"] = [m for m in data["messages"] if m["id"] != second.id]
    box_path.write_text(json.dumps(data), encoding="utf-8")

    out = _index(capsys)
    assert "1 new" in out and "1 relabelled" in out and "1 removed" in out
    with Store(tmp_path / "d" / "index.db") as store:
        labels = store.labels_by_id()
        assert store.message_count() == total
        assert "fresh-1" in labels and second.id not in labels and STARRED in labels[first.id]
        assert second.id not in store.decisions()  # its decision went with it


def test_index_refuses_to_mix_accounts(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    cfg_path = tmp_path / "d" / "sweep.toml"
    cfg_path.write_text(_set(cfg_path.read_text(encoding="utf-8"), "account", "me", ["other@example.org"]),
                        encoding="utf-8")
    capsys.readouterr()
    assert main(["--config", "d/sweep.toml", "index"]) == 2
    assert "belongs to" in capsys.readouterr().err
    assert main(["--config", "d/sweep.toml", "index", "--fresh"]) == 0


def test_limited_index_never_prunes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    capsys.readouterr()
    assert main(["--config", "d/sweep.toml", "index", "--limit", "5"]) == 0
    assert "0 removed" in capsys.readouterr().out

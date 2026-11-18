"""The guard rails: live re-check, crash-safe undo log, locking, staleness, bad inputs."""

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from inbox_sweep.backends.demo import DemoBackend
from inbox_sweep.classify import classify
from inbox_sweep.cli import main
from inbox_sweep.config import Config
from inbox_sweep.locking import LockError, exclusive
from inbox_sweep.model import SENT, STARRED, TRASH, Message
from inbox_sweep.plan import PlanError, apply_plan, build_plan, load_plan, read_log, save_plan, undo, verify_plan
from inbox_sweep.store import SCHEMA_VERSION, Store, StoreError

ME = "you@example.com"


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.me = [ME]
    cfg.data_dir = str(tmp_path)
    cfg.apply_batch_size = 20
    backend = DemoBackend(tmp_path / "demo-mailbox.json", me=ME)
    messages = list(backend.iter_messages())
    decisions = classify(messages, cfg)
    plan = build_plan(messages, decisions, cfg, account=ME, backend="demo", indexed_at=int(time.time()))
    return cfg, backend, messages, decisions, plan


def test_live_check_skips_threads_protected_after_indexing(env):
    cfg, backend, messages, _, plan = env
    starred, replied = plan["trash"][0], plan["trash"][1]
    backend.set_labels(starred["messages"][0]["id"], add=[STARRED])       # starred on the phone
    backend.add_message(Message(id="late-reply", thread_id=replied["thread_id"], date=int(time.time()),
                                from_email=ME, subject="Re: " + replied["subject"], labels=[SENT]))
    result = apply_plan(plan, backend, cfg, messages)
    assert result["skipped_protected"] == 2
    trashed = backend.trashed_thread_ids()
    assert starred["thread_id"] not in trashed and replied["thread_id"] not in trashed
    assert result["trashed"] == len(plan["trash"]) - 2


def test_live_check_unavailable_falls_back_to_the_index(env, monkeypatch):
    cfg, backend, messages, _, plan = env
    monkeypatch.setattr(backend, "protected_thread_ids", lambda protect: None)
    notes = []
    result = apply_plan(plan, backend, cfg, messages, progress=notes.append)
    assert result["trashed"] == len(plan["trash"]) and any("can't re-check" in n for n in notes)


class Flaky(DemoBackend):
    """Trashes half of the third batch, then the network drops."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def trash(self, entries, progress=None):
        self.calls += 1
        if self.calls == 3:
            super().trash(entries[: len(entries) // 2])
            raise ConnectionError("network down")
        return super().trash(entries)


def test_crash_mid_batch_is_fully_undoable_and_resumable(env, tmp_path):
    cfg, _, messages, _, plan = env
    flaky = Flaky(tmp_path / "demo-mailbox.json", me=ME)
    with pytest.raises(ConnectionError):
        apply_plan(plan, flaky, cfg, messages)
    log = cfg.data_path / f"trash-log-{plan['fingerprint'][:10]}.jsonl"
    state = read_log(log)
    assert sum(r["phase"] == "done" for r in state.values()) == 2 * cfg.apply_batch_size
    assert sum(r["phase"] == "pending" for r in state.values()) == cfg.apply_batch_size
    half_done = flaky.trashed_thread_ids()
    assert len(half_done) == 2 * cfg.apply_batch_size + cfg.apply_batch_size // 2

    # undo restores everything, including the half batch that never got a "done" line
    restored = undo(log, flaky, cfg)
    assert restored["restored"] == restored["total"] == 3 * cfg.apply_batch_size
    assert flaky.trashed_thread_ids() == set()


def test_resume_after_crash_finishes_without_redoing(env, tmp_path):
    cfg, _, messages, _, plan = env
    flaky = Flaky(tmp_path / "demo-mailbox.json", me=ME)
    with pytest.raises(ConnectionError):
        apply_plan(plan, flaky, cfg, messages)
    result = apply_plan(plan, DemoBackend(tmp_path / "demo-mailbox.json", me=ME), cfg, messages)
    assert result["already"] == 2 * cfg.apply_batch_size
    assert result["already"] + result["trashed"] == len(plan["trash"])
    assert flaky.trashed_thread_ids() == {e["thread_id"] for e in plan["trash"]}


def test_log_survives_torn_lines_and_never_downgrades_done(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join([
        json.dumps({"phase": "pending", "thread_id": "a", "messages": [{"id": "1"}]}),
        json.dumps({"phase": "done", "thread_id": "a", "messages": [{"id": "1"}]}),
        json.dumps({"phase": "pending", "thread_id": "a", "messages": [{"id": "1"}]}),
        json.dumps({"thread_id": "old-format", "messages": []}),
        '{"phase": "pending", "thread_id": "b", "mess',  # crash mid-write
    ]))
    state = read_log(log)
    assert state["a"]["phase"] == "done" and state["old-format"]["phase"] == "done" and "b" not in state


def test_undo_without_a_log_is_a_clear_error(env, tmp_path):
    cfg, backend, *_ = env
    with pytest.raises(PlanError):
        undo(tmp_path / "missing.jsonl", backend, cfg)


def test_stale_plans_are_refused_unless_allowed(env):
    cfg, backend, messages, _, plan = env
    plan["indexed_at"] = int(time.time()) - 8 * 86400
    with pytest.raises(PlanError, match="days old"):
        apply_plan(plan, backend, cfg, messages)
    assert apply_plan(plan, backend, cfg, messages, allow_stale=True)["trashed"] == len(plan["trash"])


def test_wrong_account_is_refused(env):
    cfg, backend, messages, _, plan = env
    plan["account"] = "someone.else@example.com"
    with pytest.raises(PlanError, match="signed in as"):
        apply_plan(plan, backend, cfg, messages)
    assert backend.trashed_thread_ids() == set()


def test_verify_plan_catches_unknown_and_misfiled_messages(env):
    cfg, _, messages, decisions, plan = env
    plan["trash"][0]["messages"].append({"id": "not-in-index"})
    other = next(m for m in messages if m.thread_id != plan["trash"][1]["thread_id"])
    plan["trash"][1]["messages"].append({"id": other.id})
    problems = verify_plan(plan, messages, decisions, cfg)
    assert any("not in the index" in p for p in problems)
    assert any("another thread" in p for p in problems)


@pytest.mark.parametrize("content", ["not json", json.dumps({"version": 9, "trash": []}),
                                     json.dumps({"version": 1, "trash": [{"thread_id": "x"}]}),
                                     json.dumps([1, 2, 3])])
def test_malformed_plans_are_rejected(tmp_path, content):
    path = tmp_path / "plan.json"
    path.write_text(content)
    with pytest.raises(PlanError):
        load_plan(path)


def test_plan_save_is_atomic(tmp_path, env):
    *_, plan = env
    path = save_plan(plan, tmp_path / "plan.json")
    assert load_plan(path)["fingerprint"] == plan["fingerprint"]
    assert not list(tmp_path.glob("*.tmp"))


def test_newer_index_schema_is_refused(tmp_path):
    store = Store(tmp_path / "index.db")
    store.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.conn.commit()
    store.close()
    with pytest.raises(StoreError, match="newer"):
        Store(tmp_path / "index.db")


# -- locking --------------------------------------------------------------------------
def _write_lock(path, **info):
    payload = {"pid": os.getpid(), "host": socket.gethostname(), "command": "apply", "at": time.time()}
    payload.update(info)
    path.write_text(json.dumps(payload))


def test_second_run_is_locked_out(tmp_path):
    with exclusive(tmp_path, "apply"):
        with pytest.raises(LockError, match="apply"):
            with exclusive(tmp_path, "index"):
                pass
    with exclusive(tmp_path, "index"):  # released afterwards
        pass
    assert not (tmp_path / ".lock").exists()


def test_lock_of_a_dead_process_is_taken_over(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _write_lock(tmp_path / ".lock", pid=proc.pid)
    with exclusive(tmp_path, "apply"):
        pass


def test_lock_of_a_live_process_holds_even_when_old(tmp_path):
    _write_lock(tmp_path / ".lock", at=time.time() - 20 * 3600)  # a long first index, still running
    with pytest.raises(LockError):
        with exclusive(tmp_path, "apply"):
            pass


@pytest.mark.parametrize("age_hours,locked", [(1, True), (13, False)])
def test_unverifiable_lock_expires_after_12_hours(tmp_path, age_hours, locked):
    _write_lock(tmp_path / ".lock", host="another-machine", at=time.time() - age_hours * 3600)
    if locked:
        with pytest.raises(LockError):
            with exclusive(tmp_path, "apply"):
                pass
    else:
        with exclusive(tmp_path, "apply"):
            pass


def test_torn_lock_file_is_stale(tmp_path):
    (tmp_path / ".lock").write_text('{"pid": 12')
    with exclusive(tmp_path, "apply"):
        pass


def test_a_lock_we_no_longer_own_is_left_alone(tmp_path):
    with exclusive(tmp_path, "apply"):
        _write_lock(tmp_path / ".lock", token="someone-else")
    assert json.loads((tmp_path / ".lock").read_text())["token"] == "someone-else"


# -- CLI guards -----------------------------------------------------------------------
def test_cli_refuses_plans_and_decisions_from_an_older_index(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    capsys.readouterr()
    time.sleep(1.1)  # indexed_at has one-second resolution
    assert main(["--config", "d/sweep.toml", "index"]) == 0
    assert main(["--config", "d/sweep.toml", "apply", "d/plan.json", "--yes"]) == 2
    assert "wasn't made from the current index" in capsys.readouterr().err
    assert main(["--config", "d/sweep.toml", "plan"]) == 2
    assert "classify` again" in capsys.readouterr().err
    assert main(["--config", "d/sweep.toml", "classify"]) == 0
    assert main(["--config", "d/sweep.toml", "plan"]) == 0
    assert main(["--config", "d/sweep.toml", "apply", "d/plan.json", "--yes"]) == 0
    box = DemoBackend(tmp_path / "d" / "demo-mailbox.json")
    assert box.trashed_thread_ids()


def test_cli_lock_message(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    capsys.readouterr()
    with exclusive(tmp_path / "d", "apply"):
        assert main(["--config", "d/sweep.toml", "plan"]) == 2
    assert "Another inbox-sweep run" in capsys.readouterr().err


def test_trash_folder_mail_is_never_planned(env):
    cfg, backend, messages, decisions, _ = env
    victim = next(m for m in messages if m.thread_id in {d.thread_id for d in decisions if d.action == "trash"})
    victim.labels.append(TRASH)
    plan = build_plan(messages, decisions, cfg)
    assert victim.id not in {r["id"] for e in plan["trash"] for r in e["messages"]}


# -- review findings: partial indexes, tampered locators, the log after undo ----------------
def test_a_crashed_index_blocks_classify_plan_and_apply(tmp_path, capsys, monkeypatch):
    import inbox_sweep.cli as cli

    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    box = DemoBackend(tmp_path / "d" / "demo-mailbox.json")

    class Crashy(DemoBackend):
        def sync(self, known, **kw):
            for m in self.iter_messages():
                if SENT not in m.labels:
                    yield ("new", m)
            raise ConnectionError("network dropped")

    real_get = cli.get_backend
    monkeypatch.setattr(cli, "get_backend", lambda cfg, kind=None, interactive=True: Crashy(box.path))
    capsys.readouterr()
    assert main(["--config", "d/sweep.toml", "index", "--fresh"]) == 2
    for argv in (["classify"], ["plan"], ["apply", "d/plan.json", "--yes"]):
        assert main(["--config", "d/sweep.toml", *argv]) == 2
        assert "index is incomplete" in capsys.readouterr().err
    assert box.trashed_thread_ids() == set()
    monkeypatch.setattr(cli, "get_backend", real_get)
    assert main(["--config", "d/sweep.toml", "index"]) == 0  # finishing the index unblocks everything
    assert main(["--config", "d/sweep.toml", "classify"]) == 0


def test_plans_from_older_versions_without_an_index_time_are_refused(env):
    cfg, backend, messages, _, plan = env
    plan["indexed_at"] = None
    with pytest.raises(PlanError, match="doesn't record when"):
        apply_plan(plan, backend, cfg, messages)


def test_verify_plan_catches_edited_locators(tmp_path):
    from inbox_sweep.model import Message as M
    cfg = Config()
    cfg.me = [ME]
    msgs = [M(id="INBOX|7|2", thread_id="t", date=0, from_email="deals@shop.example", folder="INBOX", uid=2,
              message_id_header="<a@x>", list_unsubscribe="<https://x/u>")]
    decisions = classify(msgs, cfg, now=10 ** 10)
    plan = build_plan(msgs, decisions, cfg)
    assert verify_plan(plan, msgs, decisions, cfg) == []
    for key, value in (("uid", 99), ("folder", "Family"), ("uidvalidity", "8"), ("message_id_header", "<b@x>")):
        edited = json.loads(json.dumps(plan))
        edited["trash"][0]["messages"][0][key] = value
        assert any(key in p for p in verify_plan(edited, msgs, decisions, cfg)), key


def test_undo_is_recorded_so_reapplying_trashes_again_and_undo_twice_is_safe(env):
    cfg, backend, messages, _, plan = env
    first = apply_plan(plan, backend, cfg, messages)
    assert undo(first["log"], backend, cfg)["restored"] == first["trashed"]
    assert undo(first["log"], backend, cfg) == {"restored": 0, "total": 0}  # nothing left to restore
    again = apply_plan(plan, backend, cfg, messages)
    assert again["trashed"] == first["trashed"] and again["already"] == 0
    assert backend.trashed_thread_ids() == {e["thread_id"] for e in plan["trash"]}


def test_keep_label_is_not_redone_on_resume(env, tmp_path):
    cfg, _, messages, _, plan = env
    flaky = Flaky(tmp_path / "demo-mailbox.json", me=ME)
    calls = []
    flaky.label_messages = lambda msgs, label, progress=None: calls.append(len(msgs))
    with pytest.raises(ConnectionError):
        apply_plan(plan, flaky, cfg, messages)
    resumed = DemoBackend(tmp_path / "demo-mailbox.json", me=ME)
    resumed.label_messages = lambda msgs, label, progress=None: calls.append(len(msgs))
    apply_plan(plan, resumed, cfg, messages)
    assert len(calls) == 1


def test_undo_all_restores_every_run(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    box = DemoBackend(tmp_path / "d" / "demo-mailbox.json")
    assert main(["--config", "d/sweep.toml", "apply", "d/plan.json", "--yes"]) == 0
    first = box.trashed_thread_ids()
    # A second sweep with stricter rules writes a second log.
    assert main(["--config", "d/sweep.toml", "index"]) == 0
    assert main(["--config", "d/sweep.toml", "classify"]) == 0
    assert main(["--config", "d/sweep.toml", "plan"]) == 0
    assert main(["--config", "d/sweep.toml", "undo", "--all"]) == 0  # dry run
    assert main(["--config", "d/sweep.toml", "undo", "--all", "--yes"]) == 0
    assert box.trashed_thread_ids() == set() and first


def test_network_errors_are_reported_cleanly(tmp_path, capsys, monkeypatch):
    import inbox_sweep.cli as cli

    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0

    def boom(cfg, kind=None, interactive=True):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(cli, "get_backend", boom)
    capsys.readouterr()
    assert main(["--config", "d/sweep.toml", "index"]) == 2
    err = capsys.readouterr().err
    assert "connection reset" in err and "pick up where they stopped" in err

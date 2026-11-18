"""End to end on the demo backend: index -> classify -> plan -> apply -> undo."""

import json

import pytest

from inbox_sweep.backends.demo import DemoBackend
from inbox_sweep.classify import classify
from inbox_sweep.cli import main
from inbox_sweep.config import Config
from inbox_sweep.plan import PlanError, apply_plan, build_plan, undo, verify_plan
from inbox_sweep.store import Store


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.me = ["you@example.com"]
    cfg.data_dir = str(tmp_path)
    backend = DemoBackend(tmp_path / "demo-mailbox.json", me="you@example.com")
    store = Store(cfg.db_path)
    store.upsert_messages(backend.iter_messages())
    yield cfg, backend, store
    store.close()


def test_plan_apply_undo_roundtrip(env):
    cfg, backend, store = env
    messages = store.messages()
    decisions = classify(messages, cfg)
    store.save_decisions(decisions)
    plan = build_plan(messages, decisions, cfg, account="you@example.com", backend="demo")
    assert plan["summary"]["trash_threads"] > 100
    assert verify_plan(plan, messages, decisions, cfg) == []

    result = apply_plan(plan, backend, cfg, messages)
    assert result["trashed"] == plan["summary"]["trash_threads"]
    assert backend.trashed_thread_ids() == {e["thread_id"] for e in plan["trash"]}

    # Nothing protected went to Trash: every replied/sent thread survives.
    sent_threads = {m.thread_id for m in messages if m.from_email == "you@example.com"}
    assert not sent_threads & backend.trashed_thread_ids()

    # Re-running resumes (nothing left to do) instead of double-applying.
    again = apply_plan(plan, backend, cfg, messages)
    assert again["trashed"] == 0 and again["already"] == result["trashed"]

    restored = undo(result["log"], backend, cfg)
    assert restored["restored"] == result["trashed"]
    assert backend.trashed_thread_ids() == set()


def test_verify_rejects_a_tampered_plan(env):
    cfg, backend, store = env
    messages = store.messages()
    decisions = classify(messages, cfg)
    plan = build_plan(messages, decisions, cfg)
    mine = next(m for m in messages if m.from_email == "you@example.com")
    plan["trash"].append({"thread_id": mine.thread_id, "subject": mine.subject,
                          "messages": [{"id": mine.id}]})
    problems = verify_plan(plan, messages, decisions, cfg)
    assert problems and any("sent by you" in p or "replied" in p or "kept" in p for p in problems)


def test_apply_refuses_wrong_account(env):
    cfg, backend, store = env
    messages = store.messages()
    decisions = classify(messages, cfg)
    plan = build_plan(messages, decisions, cfg, account="someone.else@example.com")
    with pytest.raises(PlanError):
        apply_plan(plan, backend, cfg, messages)


def test_cli_demo_and_overrides(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    out = capsys.readouterr().out
    assert "Plan:" in out and (tmp_path / "d" / "review.html").exists()

    decisions = {"version": 1, "overrides": {"alumni@state-university.example": "keep"}}
    (tmp_path / "decisions.json").write_text(json.dumps(decisions))
    assert main(["--config", "d/sweep.toml", "classify", "--overrides", "decisions.json"]) == 0
    store = Store(tmp_path / "d" / "index.db")
    kept = [d for d in store.decisions().values() if d.reason == "you chose keep for this sender"]
    store.close()
    assert kept

    assert main(["--config", "d/sweep.toml", "plan"]) == 0
    assert main(["--config", "d/sweep.toml", "apply", "d/plan.json"]) == 0  # dry run
    assert "Dry run" in capsys.readouterr().out

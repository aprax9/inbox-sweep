"""Freeze decisions into a plan, check it, apply it, and undo it.

A plan is a JSON file listing exactly which threads go to Trash. Applying works
from that frozen list (never from a live search), writes a log line per batch so
an interrupted run resumes where it stopped, and the same log drives `undo`.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from inbox_sweep.backends.base import Backend, chunked
from inbox_sweep.classify import build_context, protection
from inbox_sweep.config import Config
from inbox_sweep.model import Decision, Message

TRASH_RETENTION_DAYS = 30  # Gmail empties Trash after 30 days; most hosts are similar


class PlanError(RuntimeError):
    pass


def _ref(m: Message, uidvalidity: dict[str, str]) -> dict:
    uv = m.id.split("|")[1] if m.id.count("|") == 2 else uidvalidity.get(m.folder, "")
    return {"id": m.id, "folder": m.folder, "uid": m.uid, "uidvalidity": uv,
            "message_id_header": m.message_id_header}


def build_plan(messages: list[Message], decisions: list[Decision], cfg: Config, *,
               account: str = "", backend: str = "", uidvalidity: dict[str, str] | None = None) -> dict:
    uidvalidity = uidvalidity or {}
    by_id = {m.id: m for m in messages}
    threads: dict[str, list[tuple[Message, Decision]]] = {}
    keep_ids = []
    for d in decisions:
        m = by_id.get(d.message_id)
        if not m:
            continue
        if d.action == "keep":
            keep_ids.append(m.id)
        else:
            threads.setdefault(d.thread_id, []).append((m, d))
    entries = []
    for tid, items in threads.items():
        items.sort(key=lambda md: md[0].date, reverse=True)
        newest, d = items[0]
        entries.append({
            "thread_id": tid,
            "sender": newest.from_email,
            "subject": newest.subject,
            "date": newest.date,
            "category": d.category,
            "reason": d.reason,
            "messages": [_ref(m, uidvalidity) for m, _ in items],
        })
    entries.sort(key=lambda e: -e["date"])
    fingerprint = hashlib.sha256("\n".join(sorted(e["thread_id"] for e in entries)).encode()).hexdigest()
    return {
        "version": 1,
        "created": int(time.time()),
        "account": account,
        "backend": backend,
        "keep_label": cfg.keep_label,
        "fingerprint": fingerprint,
        "summary": {
            "trash_threads": len(entries),
            "trash_messages": sum(len(e["messages"]) for e in entries),
            "keep_messages": len(keep_ids),
            "by_category": dict(Counter(e["category"] for e in entries).most_common()),
            "top_senders": Counter(e["sender"] for e in entries).most_common(20),
        },
        "keep_message_ids": keep_ids,
        "trash": entries,
    }


def save_plan(plan: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return path


def load_plan(path: str | Path) -> dict:
    plan = json.loads(Path(path).read_text())
    if plan.get("version") != 1 or "trash" not in plan:
        raise PlanError(f"{path} is not an inbox-sweep plan")
    return plan


def verify_plan(plan: dict, messages: list[Message], decisions: list[Decision], cfg: Config,
                now: int | None = None) -> list[str]:
    """Independent safety check. Returns human-readable problems; empty means safe.

    Re-derives absolute protections from the index and refuses any plan that
    would trash protected mail or split a thread that has kept messages.
    """
    now = int(now or time.time())
    ctx = build_context(messages, cfg)
    by_id = {m.id: m for m in messages}
    kept_threads = {d.thread_id for d in decisions if d.action == "keep"}
    problems = []
    for entry in plan["trash"]:
        if entry["thread_id"] in kept_threads:
            problems.append(f"thread {entry['thread_id']} ({entry['subject']!r}) also has kept messages")
        for ref in entry["messages"]:
            m = by_id.get(ref["id"])
            if m is None:
                continue
            level, why = protection(m, cfg, ctx, now)
            if level == "absolute":
                problems.append(f"{m.from_email} {m.subject!r}: {why}")
    return problems


def log_path_for(plan: dict, cfg: Config) -> Path:
    return cfg.data_path / f"trash-log-{plan['fingerprint'][:10]}.jsonl"


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def apply_plan(plan: dict, backend: Backend, cfg: Config, messages: list[Message], *,
               label_keep: bool = True, progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda _m: None)
    account = backend.account()
    if plan.get("account") and account and plan["account"].lower() != account.lower():
        raise PlanError(f"Plan is for {plan['account']} but the backend is signed in as {account}")

    if label_keep and plan.get("keep_label") and backend.supports_labels:
        keep_ids = set(plan.get("keep_message_ids", []))
        keep_msgs = [m for m in messages if m.id in keep_ids]
        say(f"Labelling {len(keep_msgs)} kept messages '{plan['keep_label']}'")
        backend.label_messages(keep_msgs, plan["keep_label"], progress=say)

    log = log_path_for(plan, cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    done = {e["thread_id"] for e in _read_log(log)}
    todo = [e for e in plan["trash"] if e["thread_id"] not in done]
    if done:
        say(f"Resuming: {len(done)} threads already in Trash")
    trashed = 0
    failed = 0
    with log.open("a") as fh:
        for batch in chunked(todo, cfg.apply_batch_size):
            ok = set(backend.trash(batch, progress=say))
            for entry in batch:
                if entry["thread_id"] in ok:
                    fh.write(json.dumps({"thread_id": entry["thread_id"], "messages": entry["messages"],
                                         "at": int(time.time())}) + "\n")
                    trashed += 1
                else:
                    failed += 1
            fh.flush()
            say(f"Trashed {len(done) + trashed}/{len(plan['trash'])} threads")
    return {"trashed": trashed, "already": len(done), "failed": failed, "log": str(log),
            "recoverable_until": time.strftime("%Y-%m-%d", time.localtime(time.time() + TRASH_RETENTION_DAYS * 86400))}


def undo(log: str | Path, backend: Backend, cfg: Config, progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda _m: None)
    entries = _read_log(Path(log))
    restored = 0
    for batch in chunked(entries, cfg.apply_batch_size):
        restored += len(backend.restore(batch, progress=say))
        say(f"Restored {restored}/{len(entries)} threads")
    return {"restored": restored, "total": len(entries)}

"""Freeze decisions into a plan, check it, apply it, and undo it.

A plan is a JSON file listing exactly which threads go to Trash. Applying:

1. refuses plans for another account, or built from a stale index;
2. re-checks the *live* mailbox for anything protected since indexing (newly
   starred, newly replied to, newly labelled) and skips those threads;
3. labels kept mail, then trashes in batches, writing each batch to the undo log
   *before* it is sent ("pending") and again after it succeeds ("done"), so a
   crash at any point leaves a log that `undo` can fully reverse and `apply`
   can resume from.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from inbox_sweep.backends.base import Backend, LiveProtection, chunked
from inbox_sweep.classify import build_context, protection
from inbox_sweep.config import Config
from inbox_sweep.model import TRASH, Decision, Message

TRASH_RETENTION_DAYS = 30  # Gmail empties Trash after 30 days; most hosts are similar


class PlanError(RuntimeError):
    pass


def _ref(m: Message, uidvalidity: dict[str, str]) -> dict:
    uv = ""
    if m.folder and m.id.startswith(m.folder + "|"):
        uv = m.id[len(m.folder) + 1:].split("|", 1)[0]  # "folder|uidvalidity|uid"
    uv = uv or uidvalidity.get(m.folder, "")
    return {"id": m.id, "folder": m.folder, "uid": m.uid, "uidvalidity": uv,
            "message_id_header": m.message_id_header}


def build_plan(messages: list[Message], decisions: list[Decision], cfg: Config, *,
               account: str = "", backend: str = "", uidvalidity: dict[str, str] | None = None,
               indexed_at: int | None = None) -> dict:
    uidvalidity = uidvalidity or {}
    by_id = {m.id: m for m in messages}
    threads: dict[str, list[tuple[Message, Decision]]] = {}
    keep_ids = []
    for d in decisions:
        m = by_id.get(d.message_id)
        if not m or TRASH in m.labels:  # mail already in Trash (indexed for `eval`) is never planned
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
        "indexed_at": indexed_at,
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves half a plan
    return path


def load_plan(path: str | Path) -> dict:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Can't read plan {path}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("version") != 1 or not isinstance(plan.get("trash"), list):
        raise PlanError(f"{path} is not an inbox-sweep plan")
    for entry in plan["trash"]:
        if not isinstance(entry, dict) or "thread_id" not in entry or not isinstance(entry.get("messages"), list):
            raise PlanError(f"{path} has a malformed entry")
    return plan


def verify_plan(plan: dict, messages: list[Message], decisions: list[Decision], cfg: Config,
                now: int | None = None) -> list[str]:
    """Independent safety check. Returns human-readable problems; empty means safe.

    Re-derives absolute protections from the index and refuses any plan that
    would trash protected mail, split a thread that has kept messages, or touch
    messages the index doesn't know.
    """
    now = int(now or time.time())
    ctx = build_context(messages, cfg)
    by_id = {m.id: m for m in messages}
    kept_threads = {d.thread_id for d in decisions if d.action == "keep"}
    problems = []
    for entry in plan["trash"]:
        if entry["thread_id"] in kept_threads:
            problems.append(f"thread {entry['thread_id']} ({entry.get('subject', '')!r}) also has kept messages")
        for ref in entry["messages"]:
            m = by_id.get(ref.get("id"))
            if m is None:
                problems.append(f"message {ref.get('id')} is not in the index (re-run `inbox-sweep plan`)")
                continue
            if m.thread_id != entry["thread_id"]:
                problems.append(f"message {m.id} belongs to another thread")
            # The locators a backend acts on must be the index's, not whatever the file says.
            expected = _ref(m, {})
            changed = [k for k in ("folder", "uid", "message_id_header") if k in ref and ref[k] != expected[k]]
            if expected["uidvalidity"] and str(ref.get("uidvalidity") or expected["uidvalidity"]) != expected["uidvalidity"]:
                changed.append("uidvalidity")
            if changed:
                problems.append(f"message {m.id}: the plan's {', '.join(changed)} doesn't match the index "
                                "(re-run `inbox-sweep plan`)")
            level, why = protection(m, cfg, ctx, now)
            if level == "absolute":
                problems.append(f"{m.from_email} {m.subject!r}: {why}")
    return problems


def log_path_for(plan: dict, cfg: Config) -> Path:
    return cfg.data_path / f"trash-log-{plan['fingerprint'][:10]}.jsonl"


def read_log(path: Path) -> dict[str, dict]:
    """Thread id -> {"phase": "pending"|"done"|"restored", "messages": [...]}; later lines win,
    except that "pending" never replaces "done" (it can only be a re-sent batch).

    Lines without a thread id (markers) and a torn last line (crash mid-write) are ignored.
    """
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = rec.get("thread_id")
        if not tid:
            continue
        phase = rec.get("phase", "done")  # v0.1 logs only had done lines
        if out.get(tid, {}).get("phase") == "done" and phase == "pending":
            continue
        out[tid] = {"phase": phase, "messages": rec.get("messages") or out.get(tid, {}).get("messages", [])}
    return out


def _markers(path: Path) -> set[str]:
    found = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and not rec.get("thread_id") and rec.get("phase"):
                found.add(rec["phase"])
    return found


def _durable(fh) -> None:
    """Flush to disk: the log must survive a power cut, or undo can't see what moved."""
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def live_protection(cfg: Config, messages: list[Message]) -> LiveProtection:
    """Exactly the absolute protections classify uses, asked of the live mailbox.

    The keep label is deliberately not one of them: it is an audit trail of what an
    earlier run kept, and honouring it would stop retention rules from ever
    trashing mail on a later run. Star mail, or list a label under [protect]
    labels, to protect it.
    """
    ctx = build_context(messages, cfg)
    return LiveProtection(me=sorted(ctx.me), people=cfg.protect_people, labels=list(cfg.protect_labels),
                          starred=cfg.protect_starred, sent=cfg.protect_replied_threads)


def apply_plan(plan: dict, backend: Backend, cfg: Config, messages: list[Message], *,
               label_keep: bool = True, live_check: bool = True, allow_stale: bool = False,
               progress: Callable[[str], None] | None = None, now: float | None = None) -> dict:
    say = progress or (lambda _m: None)
    now = now or time.time()
    account = backend.account()
    if plan.get("account") and account and plan["account"].lower() != account.lower():
        raise PlanError(f"Plan is for {plan['account']} but the backend is signed in as {account}")
    indexed_at = plan.get("indexed_at")
    if not indexed_at and not allow_stale:
        raise PlanError("This plan doesn't record when its index was built (an older version made it). "
                        "Run `inbox-sweep index`, `classify` and `plan` again.")
    max_age = cfg.max_index_age_days * 86400
    if indexed_at and max_age and now - indexed_at > max_age and not allow_stale:
        days = int((now - indexed_at) / 86400)
        raise PlanError(f"This plan was built from an index {days} days old. Run `inbox-sweep index` "
                        "(incremental, fast), `classify` and `plan` again, or pass --allow-stale.")

    log = log_path_for(plan, cfg)
    log.parent.mkdir(parents=True, exist_ok=True)
    if label_keep and plan.get("keep_label") and backend.supports_labels:
        if "keep-labelled" in _markers(log):
            say("Kept mail was already labelled by an earlier run of this plan")
        else:
            keep_ids = set(plan.get("keep_message_ids", []))
            keep_msgs = [m for m in messages if m.id in keep_ids]
            say(f"Labelling {len(keep_msgs)} kept messages '{plan['keep_label']}'")
            backend.label_messages(keep_msgs, plan["keep_label"], progress=say)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"phase": "keep-labelled", "count": len(keep_msgs), "at": int(time.time())}) + "\n")
                _durable(fh)

    state = read_log(log)
    done = {tid for tid, rec in state.items() if rec["phase"] == "done"}
    todo = [e for e in plan["trash"] if e["thread_id"] not in done]
    if done:
        say(f"Resuming: {len(done)} threads already in Trash")

    skipped: list[str] = []
    if live_check and todo:
        protected_now = backend.protected_thread_ids(live_protection(cfg, messages))
        if protected_now is None:
            say("This backend can't re-check the live mailbox; relying on the index")
        else:
            skipped = [e["thread_id"] for e in todo if e["thread_id"] in protected_now]
            if skipped:
                say(f"Skipping {len(skipped)} threads protected since indexing (starred, replied to or labelled)")
            todo = [e for e in todo if e["thread_id"] not in protected_now]

    trashed = 0
    failed = 0
    with log.open("a", encoding="utf-8") as fh:
        for batch in chunked(todo, cfg.apply_batch_size):
            for entry in batch:  # intent first: undo can reverse a crash mid-batch
                fh.write(json.dumps({"phase": "pending", "thread_id": entry["thread_id"],
                                     "messages": entry["messages"], "at": int(time.time())}) + "\n")
            _durable(fh)
            ok = set(backend.trash(batch, progress=say))
            for entry in batch:
                if entry["thread_id"] in ok:
                    fh.write(json.dumps({"phase": "done", "thread_id": entry["thread_id"],
                                         "messages": entry["messages"], "at": int(time.time())}) + "\n")
                    trashed += 1
                else:
                    failed += 1
            _durable(fh)
            say(f"Trashed {len(done) + trashed}/{len(plan['trash'])} threads")
    return {"trashed": trashed, "already": len(done), "failed": failed, "skipped_protected": len(skipped),
            "log": str(log),
            "recoverable_until": time.strftime("%Y-%m-%d", time.localtime(now + TRASH_RETENTION_DAYS * 86400))}


def undo(log, backend: Backend, cfg: Config, progress: Callable[[str], None] | None = None) -> dict:
    """Restore every thread in the log(s), including ones a crash left as pending.

    `log` is one path or a list. Restored threads are recorded in the log, so undo
    can be re-run safely and applying the same plan again trashes them again.
    """
    say = progress or (lambda _m: None)
    paths = [Path(p) for p in (log if isinstance(log, (list, tuple)) else [log])]
    for path in paths:
        if not path.exists():
            raise PlanError(f"No such log: {path}")
    restored = total = 0
    for path in paths:
        state = read_log(path)
        entries = [{"thread_id": tid, "messages": rec["messages"]} for tid, rec in state.items()
                   if rec["phase"] in ("pending", "done")]
        total += len(entries)
        with path.open("a", encoding="utf-8") as fh:
            for batch in chunked(entries, cfg.apply_batch_size):
                ok = backend.restore(batch, progress=say)
                for tid in ok:
                    fh.write(json.dumps({"phase": "restored", "thread_id": tid, "at": int(time.time())}) + "\n")
                _durable(fh)
                restored += len(ok)
                say(f"Restored {restored}/{total} threads")
    return {"restored": restored, "total": total}

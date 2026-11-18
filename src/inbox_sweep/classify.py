"""Decide keep or trash for every indexed message. Runs locally in seconds.

Precedence, highest first:
  1. Absolute protection: threads you replied in, mail you sent, starred mail,
     protected labels, protected addresses. Nothing overrides these.
  2. Your review decisions per sender (keep all / trash all).
  3. Soft protection: people you've written to, personal-address senders,
     protected domains/names/keywords (non-bulk mail only), the recent working set.
  4. The optional AI verdict for ambiguous senders.
  5. Retention: how long each content category is worth keeping.
Finally, a thread is kept whole if any message in it is kept.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field

from inbox_sweep.categories import categorize
from inbox_sweep.config import FOREVER, Config
from inbox_sweep.headers import domain_matches, is_bulk, is_machine, is_personal_domain
from inbox_sweep.model import SENT, STARRED, Decision, Message

DAY = 86400
AI_CATEGORIES = {"other_automated", "human_unknown"}


@dataclass
class Context:
    me: set[str]
    correspondents: set[str]
    replied_threads: set[str]
    personal_domains: frozenset[str] = field(default_factory=frozenset)


def build_context(messages: list[Message], cfg: Config) -> Context:
    me = {m.lower() for m in cfg.me}
    # Every address that appears on SENT mail is one of yours (aliases included).
    for m in messages:
        if SENT in m.labels and m.from_email:
            me.add(m.from_email)
    sent = [m for m in messages if m.from_email in me]
    correspondents = {addr for m in sent for addr in (m.to + m.cc)} - me
    replied_threads = {m.thread_id for m in sent}
    return Context(me, correspondents, replied_threads, frozenset(cfg.personal_domains))


def _sender_matches(sender: str, keys: list[str]) -> bool:
    for key in keys:
        key = key.lower()
        if "@" in key and not key.startswith("@"):
            if sender == key:
                return True
        elif domain_matches(sender, [key]):
            return True
    return False


def _name_hit(text: str, words: list[str]) -> str:
    for word in words:
        if word and re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.IGNORECASE):
            return word
    return ""


def protection(msg: Message, cfg: Config, ctx: Context, now: int) -> tuple[str, str]:
    """Return (level, reason) where level is "absolute", "soft" or ""."""
    sender = msg.from_email
    if msg.from_email in ctx.me:
        return "absolute", "sent by you"
    if cfg.protect_replied_threads and msg.thread_id in ctx.replied_threads:
        return "absolute", "thread you replied in"
    if cfg.protect_starred and STARRED in msg.labels:
        return "absolute", "starred"
    for label in cfg.protect_labels:
        if msg.has_label(label):
            return "absolute", f"in your '{label}' label"
    emails = [p.lower() for p in cfg.protect_people if "@" in p]
    if sender in emails or any(addr in emails for addr in msg.to + msg.cc):
        return "absolute", "protected person"

    exempt = _sender_matches(sender, cfg.never_protect)
    bulk = is_bulk(msg)
    if not exempt:
        names = [p for p in cfg.protect_people if "@" not in p]
        hit = _name_hit(msg.from_name + " " + sender.split("@")[0], names)
        if hit:
            return "soft", f"protected name '{hit}'"
        if not bulk:
            if cfg.protect_correspondents and sender in ctx.correspondents:
                return "soft", "someone you've written to"
            if cfg.protect_domains and domain_matches(sender, cfg.protect_domains):
                return "soft", "protected domain (a person, not a mailing)"
            if not is_machine(msg) and is_personal_domain(msg.from_domain, ctx.personal_domains):
                return "soft", "a person on a personal address"
        hit = _name_hit(msg.subject + " " + msg.from_name, cfg.protect_keywords)
        if hit and not bulk:
            return "soft", f"protected keyword '{hit}'"
    if cfg.recent_days and now - msg.date < cfg.recent_days * DAY:
        return "soft", f"from the last {cfg.recent_days} days"
    return "", ""


def _override_for(sender: str, overrides: dict[str, str]) -> str:
    if sender in overrides:
        return overrides[sender]
    domain = "@" + sender.rsplit("@", 1)[-1]
    return overrides.get(domain, "")


def classify(messages: list[Message], cfg: Config, *, overrides: dict[str, str] | None = None,
             ai: dict[str, dict] | None = None, now: int | None = None) -> list[Decision]:
    now = int(now or time.time())
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    ai = ai or {}
    ctx = build_context(messages, cfg)
    decisions: dict[str, Decision] = {}

    for msg in messages:
        category = categorize(msg)
        level, why = protection(msg, cfg, ctx, now)
        override = _override_for(msg.from_email, overrides)

        def decide(action: str, reason: str, prot: str = "") -> None:
            decisions[msg.id] = Decision(msg.id, msg.thread_id, action, category, reason, prot)

        if level == "absolute":
            decide("keep", why, level)
            continue
        if override == "keep":
            decide("keep", "you chose keep for this sender", "soft")
            continue
        if override == "trash":
            decide("trash", "you chose trash for this sender")
            continue
        if level == "soft":
            decide("keep", why, level)
            continue
        verdict = ai.get(msg.from_email)
        if verdict and category in AI_CATEGORIES and verdict.get("action") in ("keep", "trash"):
            decide(verdict["action"], f"AI: {verdict.get('reason', '')}".strip())
            continue
        days = cfg.retention.get(category, FOREVER)
        age = (now - msg.date) / DAY
        if days == FOREVER:
            decide("keep", f"{category}: kept forever")
        elif age >= days:
            decide("trash", category if days == 0 else f"{category} older than {days} days")
        else:
            decide("keep", f"{category}: younger than {days} days")

    # Keep threads whole: one kept message keeps its thread.
    kept_threads = {d.thread_id for d in decisions.values() if d.action == "keep"}
    for d in decisions.values():
        if d.action == "trash" and d.thread_id in kept_threads:
            d.action = "keep"
            d.reason = "another message in this thread is kept"
    return list(decisions.values())


def summarize(messages: list[Message], decisions: list[Decision]) -> dict:
    by_id = {m.id: m for m in messages}
    trash_threads = {d.thread_id for d in decisions if d.action == "trash"}
    keep_threads = {d.thread_id for d in decisions if d.action == "keep"}
    cats = Counter(d.category for d in decisions if d.action == "trash")
    senders = Counter(by_id[d.message_id].from_email for d in decisions if d.action == "trash")
    return {
        "messages": len(decisions),
        "keep_messages": sum(d.action == "keep" for d in decisions),
        "trash_messages": sum(d.action == "trash" for d in decisions),
        "keep_threads": len(keep_threads),
        "trash_threads": len(trash_threads),
        "trash_by_category": dict(cats.most_common()),
        "top_trash_senders": senders.most_common(15),
    }

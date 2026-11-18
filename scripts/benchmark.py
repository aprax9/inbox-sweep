"""How fast is the local pipeline? Everything after `index` runs on your machine.

    python scripts/benchmark.py --messages 100000
    python scripts/benchmark.py --messages 100000 --max-seconds 120   # CI gate

Builds a synthetic mailbox by replicating the demo mailbox (realistic mix of
people, newsletters, receipts, codes) with varied senders and dates, then times
each local step. Provider speed (Gmail quota, Graph throttling) is separate:
see the README.
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import sys
import tempfile
import time
from pathlib import Path

from inbox_sweep.backends.demo import generate_mailbox
from inbox_sweep.classify import classify, summarize
from inbox_sweep.config import Config
from inbox_sweep.plan import build_plan, verify_plan
from inbox_sweep.review import build_review_data
from inbox_sweep.store import Store

ME = "you@example.com"
NOW = 1_800_000_000


def synthetic(n: int, seed: int = 1):
    base = generate_mailbox(ME, now=NOW)
    rng = random.Random(seed)
    out, copy = [], 0
    while len(out) < n:
        for m in base:
            if len(out) >= n:
                break
            sender = m.from_email
            if sender != ME and rng.random() < 0.3:  # more distinct senders, like a real inbox
                local, domain = sender.split("@", 1)
                sender = f"{local}@{copy % 400}.{domain}"
            out.append(dataclasses.replace(m, id=f"{m.id}-{copy}", thread_id=f"{m.thread_id}-{copy}",
                                           from_email=sender, date=m.date - rng.randint(0, 30 * 86400),
                                           labels=list(m.labels), to=list(m.to)))
        copy += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=100_000)
    ap.add_argument("--max-seconds", type=float, help="exit 1 if the whole pipeline takes longer")
    args = ap.parse_args()

    cfg = Config()
    cfg.me = [ME]
    timings: list[tuple[str, float]] = []

    def step(name, fn):
        started = time.perf_counter()
        result = fn()
        timings.append((name, time.perf_counter() - started))
        return result

    messages = synthetic(args.messages)
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "index.db")
        step("store: write index", lambda: store.upsert_messages(messages))
        loaded = step("store: load index", store.messages)
        decisions = step("classify", lambda: classify(loaded, cfg, now=NOW))
        step("store: save decisions", lambda: store.save_decisions(decisions))
        plan = step("plan", lambda: build_plan(loaded, decisions, cfg))
        problems = step("verify plan", lambda: verify_plan(plan, loaded, decisions, cfg, now=NOW))
        step("review page data", lambda: build_review_data(loaded, decisions, {}, ME))
        step("summary", lambda: summarize(loaded, decisions))
        store.close()
    assert not problems, problems[:3]

    total = sum(t for _, t in timings)
    n = len(messages)
    print(f"{n:,} messages, {plan['summary']['trash_threads']:,} threads planned for Trash\n")
    for name, t in timings:
        print(f"  {name:<22} {t:7.2f}s  {n / t if t else 0:>12,.0f} msgs/s")
    print(f"  {'total':<22} {total:7.2f}s")
    if args.max_seconds and total > args.max_seconds:
        print(f"\nSlower than the {args.max_seconds:.0f}s budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure how well the rules match what you actually want to keep.

Truth can come from:
- a label or folder you applied by hand during an earlier cleanup (`--truth-label`):
  labelled = keep, everything else = trash. Index with --include-trash to score
  the mail you trashed as well.
- the decisions.json you downloaded from the review page (`--truth-file`): your
  per-sender calls, so this measures how much you had to correct the defaults.
- a CSV sample you labelled yourself (`--export-sample`, then `--truth-file`).
- the built-in labeled corpus (`--corpus`).

The number that matters most is **false trash**: mail you would keep that the
rules would trash. It should be zero. "Clutter" is mail you'd trash that the
rules keep: annoying, but safe.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from inbox_sweep.classify import classify
from inbox_sweep.config import Config
from inbox_sweep.model import Decision, Message


def truth_from_label(messages: list[Message], label: str) -> dict[str, str]:
    target = label.lower()
    return {m.id: "keep" if any(l.lower() == target for l in m.labels) else "trash" for m in messages}


def truth_from_file(path: str | Path, messages: list[Message]) -> dict[str, str]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        out = {}
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                truth = (row.get("truth") or "").strip().lower()
                if truth in ("keep", "trash") and row.get("id"):
                    out[row["id"]] = truth
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    overrides = {k.lower(): v for k, v in (data.get("overrides", data) or {}).items() if v in ("keep", "trash")}
    out = {}
    for m in messages:
        verdict = overrides.get(m.from_email) or overrides.get("@" + m.from_domain)
        if verdict:
            out[m.id] = verdict
    return out


def _hide(messages: list[Message], label: str | None) -> list[Message]:
    if not label:
        return messages
    target = label.lower()
    return [dataclasses.replace(m, labels=[l for l in m.labels if l.lower() != target]) for m in messages]


def evaluate(messages: list[Message], cfg: Config, truth: dict[str, str], *, hide_label: str | None = None,
             now: int | None = None, scenario: dict[str, str] | None = None) -> dict:
    """Classify with the truth label hidden, then compare against truth."""
    run_cfg = dataclasses.replace(cfg)
    if hide_label:
        run_cfg.protect_labels = [l for l in cfg.protect_labels if l.lower() != hide_label.lower()]
    decisions = classify(_hide(messages, hide_label), run_cfg, now=now)
    return score(messages, decisions, truth, scenario=scenario)


def score(messages: list[Message], decisions: list[Decision], truth: dict[str, str],
          scenario: dict[str, str] | None = None) -> dict:
    by_id = {m.id: m for m in messages}
    pred = {d.message_id: d for d in decisions}
    counts = Counter()
    by_category: dict[str, Counter] = defaultdict(Counter)
    false_trash_senders, clutter_senders = Counter(), Counter()
    by_scenario: dict[str, Counter] = defaultdict(Counter)
    for mid, want in truth.items():
        d = pred.get(mid)
        if d is None or mid not in by_id:
            continue
        got = d.action
        key = ("keep" if want == "keep" else "trash") + "->" + got
        counts[key] += 1
        by_category[d.category][key] += 1
        if scenario and mid in scenario:
            by_scenario[scenario[mid]][key] += 1
        if want == "keep" and got == "trash":
            false_trash_senders[by_id[mid].from_email] += 1
        elif want == "trash" and got == "keep":
            clutter_senders[by_id[mid].from_email] += 1
    want_keep = counts["keep->keep"] + counts["keep->trash"]
    want_trash = counts["trash->trash"] + counts["trash->keep"]
    total = want_keep + want_trash
    return {
        "scored": total,
        "want_keep": want_keep,
        "want_trash": want_trash,
        "false_trash": counts["keep->trash"],
        "clutter": counts["trash->keep"],
        "false_trash_rate": counts["keep->trash"] / want_keep if want_keep else 0.0,
        "clutter_rate": counts["trash->keep"] / want_trash if want_trash else 0.0,
        "accuracy": (counts["keep->keep"] + counts["trash->trash"]) / total if total else 0.0,
        "by_category": {c: dict(v) for c, v in sorted(by_category.items())},
        "by_scenario": {s: dict(v) for s, v in sorted(by_scenario.items())},
        "false_trash_senders": false_trash_senders.most_common(20),
        "clutter_senders": clutter_senders.most_common(20),
    }


def export_sample(messages: list[Message], decisions: list[Decision], n: int, path: str | Path,
                  seed: int = 7) -> int:
    """Write a stratified sample (by category and proposal) for you to label in a spreadsheet."""
    rng = random.Random(seed)
    by_id = {m.id: m for m in messages}
    strata: dict[tuple[str, str], list[Decision]] = defaultdict(list)
    for d in decisions:
        if d.message_id in by_id:
            strata[(d.category, d.action)].append(d)
    picked: list[Decision] = []
    keys = sorted(strata)
    per = max(1, n // max(1, len(keys)))
    for key in keys:
        pool = strata[key]
        rng.shuffle(pool)
        picked += pool[:per]
    rng.shuffle(picked)
    picked = picked[:n]
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "date", "sender", "subject", "category", "proposed", "truth"])
        for d in picked:
            m = by_id[d.message_id]
            w.writerow([m.id, m.date, m.from_email, m.subject, d.category, d.action, ""])
    return len(picked)

"""inbox-sweep: clean an inbox down to what's worth keeping for 20 years.

index -> classify -> review -> plan -> apply (-> undo). `doctor` checks setup,
`eval` measures accuracy, `demo` tries everything on a fake mailbox.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path

from inbox_sweep import __version__
from inbox_sweep.backends import BackendError, get_backend
from inbox_sweep.categories import CATEGORY_HELP
from inbox_sweep.classify import classify, summarize
from inbox_sweep.config import CONFIG_TEMPLATE, Config, ConfigError, load_config
from inbox_sweep.locking import LockError, exclusive
from inbox_sweep.plan import PlanError, apply_plan, build_plan, load_plan, save_plan, undo, verify_plan
from inbox_sweep.queries import build_queries
from inbox_sweep.review import build_review_data, write_review
from inbox_sweep.store import Store, StoreError
from inbox_sweep import unsubscribe as unsub


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _config(args) -> Config:
    path = args.config
    if path is None and Path("sweep.toml").exists():
        path = "sweep.toml"
    cfg = load_config(path)
    if args.backend:
        cfg.backend = args.backend
    cfg.private = cfg.private or bool(getattr(args, "private", False))
    if path is None and cfg.backend != "demo":
        raise ConfigError("No sweep.toml found. Run `inbox-sweep init`, or try `inbox-sweep demo`.")
    return cfg


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _decisions(store: Store):
    decisions = list(store.decisions().values())
    if not decisions:
        raise PlanError("No decisions yet. Run `inbox-sweep classify` first.")
    return decisions


PRIVATE_NOTE = "(private mode: senders and subjects hidden. Review them in the local review page.)"


def _print_summary(s: dict, private: bool = False) -> None:
    print(f"Messages: {s['messages']:,}  keep {s['keep_messages']:,}  trash {s['trash_messages']:,}")
    print(f"Threads:  keep {s['keep_threads']:,}  trash {s['trash_threads']:,}")
    if s["trash_by_category"]:
        print("\nTrash by category:")
        for cat, n in s["trash_by_category"].items():
            print(f"  {n:>7,}  {cat:<16} {CATEGORY_HELP.get(cat, '')}")
    if private:
        print("\n" + PRIVATE_NOTE)
    elif s["top_trash_senders"]:
        print("\nTop senders going to Trash:")
        for sender, n in s["top_trash_senders"]:
            print(f"  {n:>7,}  {sender}")


def _problems(problems: list[str], private: bool) -> None:
    if private:
        say(f"  {len(problems)} problems (details hidden in private mode)")
        return
    for p in problems[:20]:
        say("  " + p)


# -- commands -------------------------------------------------------------------
def cmd_init(args) -> int:
    from inbox_sweep.wizard import interview, render_config

    path = Path(args.config or "sweep.toml")
    if path.exists() and not args.force:
        say(f"{path} already exists (use --force to overwrite)")
        return 1
    if args.me or args.no_input or not sys.stdin.isatty():
        if not args.me:
            text = CONFIG_TEMPLATE
        else:
            from inbox_sweep.wizard import suggest
            me = [m.strip() for m in args.me.split(",") if m.strip()]
            guess = suggest(me[0])
            text = render_config(provider=args.provider or guess["provider"], me=me,
                                 imap_host=args.imap_host or guess["imap_host"], imap_port=guess["imap_port"],
                                 imap_tls=guess["imap_tls"],
                                 people=[p.strip() for p in (args.protect or "").split(",") if p.strip()],
                                 domains=[d.strip() for d in (args.domains or "").split(",") if d.strip()],
                                 private=args.private)
    else:
        text = render_config(**interview())
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    say(f"Wrote {path}. Next: `inbox-sweep doctor`, then `inbox-sweep index`.")
    return 0


def cmd_doctor(args) -> int:
    from inbox_sweep.doctor import FAIL, render, run_checks

    checks = run_checks(args.config, online=not args.offline)
    print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def cmd_auth(args) -> int:
    cfg = _config(args)
    if cfg.backend not in ("gmail", "outlook"):
        raise ConfigError("`auth` is for the gmail and outlook backends; IMAP uses an app password.")
    backend = get_backend(cfg)
    say(f"Signed in as {backend.account()}")
    return 0


def cmd_index(args) -> int:
    cfg = _config(args)
    with exclusive(cfg.data_path, "index"):
        backend = get_backend(cfg)
        with _store(cfg) as store:
            account = backend.account()
            prev_account, prev_backend = store.get_meta("account"), store.get_meta("backend")
            if store.message_count() and not args.fresh and (
                    (prev_account and account and prev_account != account) or
                    (prev_backend and prev_backend != backend.name)):
                raise ConfigError(f"The index in {cfg.data_path} belongs to {prev_account} via {prev_backend}. "
                                  "Use --fresh, or another [paths] data_dir.")
            if args.fresh:
                store.clear_messages()
            known = store.labels_by_id()
            say(f"Indexing {account} via {backend.name} ({'incremental' if known else 'full'}, headers only)...")
            progress = None if cfg.private else say  # backend progress can name folders
            started = time.monotonic()
            counts: Counter = Counter()
            new_buf: list = []
            relabel_buf: list = []
            removed: list = []
            for event in backend.sync(known, query=args.query, limit=args.limit, progress=progress,
                                      include_trash=args.include_trash):
                kind = event[0]
                if kind == "new":
                    new_buf.append(event[1])
                    if len(new_buf) >= 1000:
                        counts["new"] += store.upsert_messages(new_buf)
                        new_buf.clear()
                elif kind == "labels":
                    relabel_buf.append((event[1], event[2]))
                    if len(relabel_buf) >= 5000:
                        counts["labels"] += store.update_labels(relabel_buf)
                        relabel_buf.clear()
                elif kind == "removed":
                    removed.append(event[1])
            counts["new"] += store.upsert_messages(new_buf)
            counts["labels"] += store.update_labels(relabel_buf)
            counts["removed"] += store.delete_messages(removed)
            elapsed = time.monotonic() - started
            store.set_meta("account", account)
            store.set_meta("backend", backend.name)
            store.set_meta("indexed_at", int(time.time()))
            store.set_meta("includes_trash", bool(args.include_trash))
            if hasattr(backend, "uidvalidities"):
                store.set_meta("uidvalidity", backend.uidvalidities())
            backend.close()
            rate = counts["new"] / elapsed if elapsed > 0 else 0
            print(f"Index updated in {elapsed:.0f}s: {counts['new']:,} new ({rate:.1f}/s), "
                  f"{counts['labels']:,} relabelled, {counts['removed']:,} removed. "
                  f"{store.message_count():,} messages indexed.")
    return 0


def cmd_classify(args) -> int:
    cfg = _config(args)
    with exclusive(cfg.data_path, "classify"), _store(cfg) as store:
        messages = store.messages()
        if not messages:
            raise PlanError("The index is empty. Run `inbox-sweep index` first.")
        if args.overrides:
            try:
                data = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"Can't read {args.overrides}: {exc}") from exc
            overrides = data.get("overrides", data) if isinstance(data, dict) else {}
            store.set_overrides({k: v for k, v in overrides.items() if v in ("keep", "trash")}, replace=True)
            say(f"Loaded {len(store.overrides())} sender decisions from {args.overrides}")

        def ai_verdicts() -> dict:
            return {s: v for s, v in store.ai_decisions().items() if v["action"] in ("keep", "trash")}

        started = time.monotonic()
        decisions = classify(messages, cfg, overrides=store.overrides(), ai=ai_verdicts())
        if args.ai or cfg.ai_enabled:
            from inbox_sweep.ai import describe_egress, run_ai
            say(describe_egress(cfg))
            run_ai(store, cfg, messages, decisions, progress=say, max_groups=args.ai_max)
            decisions = classify(messages, cfg, overrides=store.overrides(), ai=ai_verdicts())
        store.save_decisions(decisions)
        store.set_meta("classified_index_at", store.get_meta("indexed_at"))
        _print_summary(summarize(messages, decisions), cfg.private)
        print(f"\nClassified {len(messages):,} messages in {time.monotonic() - started:.1f}s. "
              "Next: `inbox-sweep review --open`, then `inbox-sweep plan`.")
    return 0


def cmd_stats(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        _print_summary(summarize(store.messages(), _decisions(store)), cfg.private)
    return 0


def cmd_review(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        data = build_review_data(store.messages(), _decisions(store), store.overrides(), store.get_meta("account", ""))
        path = write_review(args.out or cfg.data_path / "review.html", data)
    print(f"Review page: {path.resolve()}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def _require_fresh_decisions(store: Store) -> None:
    if store.get_meta("indexed_at") != store.get_meta("classified_index_at"):
        raise PlanError("The index changed since you last classified. Run `inbox-sweep classify` again.")


def cmd_plan(args) -> int:
    cfg = _config(args)
    with exclusive(cfg.data_path, "plan"), _store(cfg) as store:
        messages, decisions = store.messages(), _decisions(store)
        _require_fresh_decisions(store)
        plan = build_plan(messages, decisions, cfg, account=store.get_meta("account", ""),
                          backend=store.get_meta("backend", cfg.backend),
                          uidvalidity=store.get_meta("uidvalidity", {}),
                          indexed_at=store.get_meta("indexed_at"))
        problems = verify_plan(plan, messages, decisions, cfg)
        if problems:
            say("Refusing to write a plan that touches protected mail:")
            _problems(problems, cfg.private)
            return 2
        path = save_plan(plan, args.out or cfg.data_path / "plan.json")
    s = plan["summary"]
    print(f"Plan: {s['trash_threads']:,} threads ({s['trash_messages']:,} messages) to Trash; "
          f"{s['keep_messages']:,} messages kept.")
    print(f"Saved {path}. Dry run: `inbox-sweep apply {path}`. Then add --yes.")
    return 0


def cmd_apply(args) -> int:
    cfg = _config(args)
    plan = load_plan(args.plan)
    with exclusive(cfg.data_path, "apply"), _store(cfg) as store:
        messages, decisions = store.messages(), _decisions(store)
        if plan.get("indexed_at") and plan.get("indexed_at") != store.get_meta("indexed_at"):
            raise PlanError("The index changed since this plan was made. Run `inbox-sweep classify` and `plan` again.")
        problems = verify_plan(plan, messages, decisions, cfg)
        if problems:
            say("Plan failed the safety check; nothing was changed:")
            _problems(problems, cfg.private)
            return 2
        s = plan["summary"]
        print(f"Account: {plan.get('account') or '(unknown)'}  backend: {plan.get('backend')}")
        print(f"Will move {s['trash_threads']:,} threads ({s['trash_messages']:,} messages) to Trash "
              f"and label {s['keep_messages']:,} kept messages '{plan.get('keep_label')}'.")
        if cfg.private:
            for cat, n in s["by_category"].items():
                print(f"  {n:>7,}  {cat}")
        else:
            for e in plan["trash"][:10]:
                print(f"  - {e['sender']}: {e['subject'][:70]}  [{e['category']}]")
        if not args.yes:
            print("\nDry run. Nothing changed. Re-run with --yes to apply.")
            return 0
        backend = get_backend(cfg, plan.get("backend") or None)
        started = time.monotonic()
        result = apply_plan(plan, backend, cfg, messages, label_keep=not args.no_label,
                            live_check=not args.no_live_check, allow_stale=args.allow_stale,
                            progress=None if cfg.private else say)
        backend.close()
    elapsed = time.monotonic() - started
    print(f"Moved {result['trashed']:,} threads to Trash in {elapsed:.0f}s "
          f"({result['already']:,} done earlier, {result['failed']:,} failed, "
          f"{result['skipped_protected']:,} skipped because they're protected now).")
    print(f"Recoverable from Trash until about {result['recoverable_until']}.")
    print(f"Undo: inbox-sweep undo {result['log']} --yes")
    return 0 if not result["failed"] else 1


def cmd_undo(args) -> int:
    cfg = _config(args)
    if not args.yes:
        print(f"This restores every thread listed in {args.log}. Re-run with --yes.")
        return 0
    with exclusive(cfg.data_path, "undo"):
        backend = get_backend(cfg)
        result = undo(args.log, backend, cfg, progress=say)
        backend.close()
    print(f"Restored {result['restored']:,} of {result['total']:,} threads.")
    return 0 if result["restored"] == result["total"] else 1


def cmd_unsubscribe(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        cands = unsub.candidates(store.messages(), _decisions(store))
    one_click = [c for c in cands if c["method"] == "one-click"]
    print(f"{len(cands)} senders are entirely going to Trash and have unsubscribe headers; "
          f"{len(one_click)} support one-click.")
    if cfg.private:
        print(PRIVATE_NOTE)
    else:
        for c in cands[:50]:
            print(f"  {c['messages']:>5}  {c['method']:<9} {c['sender']}")
    if not args.yes:
        print("\nDry run. Re-run with --yes to send one-click unsubscribes (others are listed for you).")
        return 0
    result = unsub.run(cands, cfg.data_path / "unsubscribe-log.jsonl", progress=None if cfg.private else say)
    print(f"Unsubscribed {result['ok']}, failed {result['failed']}, manual {result['manual']}.")
    return 0


def _print_eval(r: dict, private: bool, show_scenarios: bool = False) -> None:
    print(f"Scored {r['scored']:,} messages ({r['want_keep']:,} you'd keep, {r['want_trash']:,} you'd trash)")
    print(f"  False trash (kept mail the rules would trash): {r['false_trash']:,} "
          f"({r['false_trash_rate']:.2%})   <- should be 0")
    print(f"  Clutter (junk the rules would keep):            {r['clutter']:,} ({r['clutter_rate']:.2%})")
    print(f"  Agreement:                                      {r['accuracy']:.2%}")
    rows = [(c, v) for c, v in r["by_category"].items() if v.get("keep->trash") or v.get("trash->keep")]
    if rows:
        print("\nWhere they disagree, by category (false trash / clutter):")
        for c, v in rows:
            print(f"  {c:<16} {v.get('keep->trash', 0):>6} / {v.get('trash->keep', 0)}")
    if show_scenarios:
        bad = [(s, v) for s, v in r["by_scenario"].items() if v.get("keep->trash") or v.get("trash->keep")]
        if bad:
            print("\nScenarios with mistakes:")
            for s, v in bad:
                print(f"  {s}: {v}")
    if not private:
        for title, key in (("False-trash senders", "false_trash_senders"), ("Clutter senders", "clutter_senders")):
            if r[key]:
                print(f"\n{title}:")
                for sender, n in r[key][:10]:
                    print(f"  {n:>6}  {sender}")


def cmd_eval(args) -> int:
    from inbox_sweep import evaluate as ev

    if args.corpus:
        from inbox_sweep.corpus import corpus_config, labeled_corpus
        msgs, truth, scen = labeled_corpus()
        report = ev.evaluate(msgs, corpus_config(), truth, scenario=scen)
        print(json.dumps(report, indent=1)) if args.json else _print_eval(report, False, show_scenarios=True)
        return 1 if args.max_false_trash is not None and report["false_trash"] > args.max_false_trash else 0
    cfg = _config(args)
    with _store(cfg) as store:
        messages = store.messages()
        if not messages:
            raise PlanError("The index is empty. Run `inbox-sweep index` first.")
        if args.export_sample:
            out = Path(args.out or cfg.data_path / "sample.csv")
            n = ev.export_sample(messages, _decisions(store), args.export_sample, out)
            print(f"Wrote {n} messages to {out}. Fill in the 'truth' column with keep or trash, "
                  f"then run `inbox-sweep eval --truth-file {out}`.")
            return 0
        if args.truth_label:
            if not store.get_meta("includes_trash"):
                say("Note: Trash isn't indexed, so only your kept mail is scored. For both sides, run "
                    "`inbox-sweep index --include-trash --fresh` first (Trash itself is never touched).")
            truth = ev.truth_from_label(messages, args.truth_label)
            report = ev.evaluate(messages, cfg, truth, hide_label=args.truth_label)
        elif args.truth_file:
            report = ev.evaluate(messages, cfg, ev.truth_from_file(args.truth_file, messages))
        else:
            raise ConfigError("Choose --truth-label, --truth-file, --export-sample or --corpus.")
    print(json.dumps(report, indent=1)) if args.json and not cfg.private else _print_eval(report, cfg.private)
    return 1 if args.max_false_trash is not None and report["false_trash"] > args.max_false_trash else 0


def cmd_queries(args) -> int:
    cfg = _config(args)
    q = build_queries(cfg)
    if args.json:
        print(json.dumps(q, indent=2))
        return 0
    print(f"Keep label: {q['label']}  (search form: label:{q['label_query']})\n")
    print("1. Tag every match of each query with the keep label, one at a time:")
    for i, item in enumerate(q["protect"], 1):
        print(f"\n  [{i}] {item['name']}: {item['why']}\n      {item['query']}")
    print(f"\n2. Move everything else to Trash:\n      {q['trash']}")
    print("\n3. Verify (each should find nothing new):")
    for v in q["verify"]:
        print(f"      {v}")
    for note in q["notes"]:
        print(f"\nNote: {note}")
    return 0


def cmd_demo(args) -> int:
    base = Path(args.dir)
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / "sweep.toml"
    cfg_text = (CONFIG_TEMPLATE
                .replace('type = "gmail"', 'type = "demo"', 1)
                .replace('data_dir = ".inbox-sweep"', f'data_dir = "{base.as_posix()}"', 1))
    cfg_path.write_text(cfg_text, encoding="utf-8")
    ns = argparse.Namespace(config=str(cfg_path), backend=None, fresh=True, query=None, limit=None,
                            include_trash=False, overrides=None, ai=False, ai_max=None, out=None,
                            open=args.open, private=getattr(args, "private", False))
    for step in (cmd_index, cmd_classify, cmd_review, cmd_plan):
        code = step(ns)
        if code:
            return code
    print(f"\nTry: inbox-sweep --config {cfg_path} apply {base / 'plan.json'} --yes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inbox-sweep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"inbox-sweep {__version__}")
    p.add_argument("--config", help="path to sweep.toml (default: ./sweep.toml)")
    p.add_argument("--backend", choices=["gmail", "outlook", "imap", "demo"], help="override [backend] type")
    p.add_argument("--private", action="store_true",
                   help="print counts only, never senders or subjects (for AI agents running the CLI)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="answer a few questions and write sweep.toml")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-input", action="store_true", help="write the plain template without asking")
    s.add_argument("--me", help="your addresses, comma-separated (skips the questions)")
    s.add_argument("--provider", choices=["gmail", "outlook", "imap"])
    s.add_argument("--imap-host")
    s.add_argument("--protect", help="protected people, comma-separated")
    s.add_argument("--domains", help="protected domains, comma-separated")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="check setup and say how to fix problems")
    s.add_argument("--offline", action="store_true", help="skip network checks")
    s.set_defaults(func=cmd_doctor)

    sub.add_parser("auth", help="sign in to Gmail or Outlook (once)").set_defaults(func=cmd_auth)

    s = sub.add_parser("index", help="read headers into the local index (incremental after the first run)")
    s.add_argument("--query", help="limit to a search (Gmail syntax for gmail and Gmail-over-IMAP)")
    s.add_argument("--limit", type=int)
    s.add_argument("--fresh", action="store_true", help="drop the existing index first")
    s.add_argument("--include-trash", action="store_true",
                   help="also index Trash (for `eval` only; plans never touch it)")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("classify", help="decide keep/trash for every message")
    s.add_argument("--overrides", help="decisions.json from the review page")
    s.add_argument("--ai", action="store_true", help="ask a language model (local by default) about ambiguous senders")
    s.add_argument("--ai-max", type=int, help="cap the number of sender groups sent")
    s.set_defaults(func=cmd_classify)

    sub.add_parser("stats", help="show the current decisions").set_defaults(func=cmd_stats)

    s = sub.add_parser("review", help="write the per-sender review page")
    s.add_argument("--out")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("plan", help="freeze decisions into plan.json (safety-checked)")
    s.add_argument("--out")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("apply", help="label kept mail and move the plan's threads to Trash")
    s.add_argument("plan")
    s.add_argument("--yes", action="store_true", help="actually do it (default is a dry run)")
    s.add_argument("--no-label", action="store_true", help="skip labelling kept mail")
    s.add_argument("--allow-stale", action="store_true", help="apply a plan built from an old index")
    s.add_argument("--no-live-check", action="store_true",
                   help="don't re-check the live mailbox for newly protected threads")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("undo", help="restore threads from an apply log")
    s.add_argument("log")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("unsubscribe", help="one-click unsubscribe from senders being fully trashed")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_unsubscribe)

    s = sub.add_parser("eval", help="measure the rules against mail you've already sorted")
    s.add_argument("--truth-label", help="a label/folder from a manual cleanup: labelled = keep")
    s.add_argument("--truth-file", help="decisions.json from the review page, or a labelled sample CSV")
    s.add_argument("--export-sample", type=int, metavar="N", help="write N messages to label by hand")
    s.add_argument("--corpus", action="store_true", help="score the built-in labeled corpus (no account)")
    s.add_argument("--out")
    s.add_argument("--json", action="store_true")
    s.add_argument("--max-false-trash", type=int, help="exit 1 if false trash exceeds this (for CI)")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("queries", help="Gmail searches for browser mode, from sweep.toml")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_queries)

    s = sub.add_parser("demo", help="try the whole pipeline on a fake mailbox")
    s.add_argument("--dir", default=".inbox-sweep-demo")
    s.add_argument("--open", action="store_true", help="open the review page")
    s.set_defaults(func=cmd_demo)
    return p


def _safe_streams() -> None:
    """Subjects can hold any character; never crash printing one to a cp1252 console or pipe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, BackendError, PlanError, LockError, StoreError, RuntimeError) as exc:
        say(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        say("interrupted; `apply` resumes where it stopped")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

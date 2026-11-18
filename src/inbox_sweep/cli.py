"""Command line: index -> classify -> review -> plan -> apply (-> undo)."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from inbox_sweep import __version__
from inbox_sweep.backends import BackendError, get_backend
from inbox_sweep.categories import CATEGORY_HELP
from inbox_sweep.classify import classify, summarize
from inbox_sweep.config import CONFIG_TEMPLATE, Config, ConfigError, load_config
from inbox_sweep.plan import PlanError, apply_plan, build_plan, load_plan, save_plan, undo, verify_plan
from inbox_sweep.queries import build_queries
from inbox_sweep.review import build_review_data, write_review
from inbox_sweep.store import Store
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


def _print_summary(s: dict) -> None:
    print(f"Messages: {s['messages']:,}  keep {s['keep_messages']:,}  trash {s['trash_messages']:,}")
    print(f"Threads:  keep {s['keep_threads']:,}  trash {s['trash_threads']:,}")
    if s["trash_by_category"]:
        print("\nTrash by category:")
        for cat, n in s["trash_by_category"].items():
            print(f"  {n:>7,}  {cat:<16} {CATEGORY_HELP.get(cat, '')}")
    if s["top_trash_senders"]:
        print("\nTop senders going to Trash:")
        for sender, n in s["top_trash_senders"]:
            print(f"  {n:>7,}  {sender}")


# -- commands -------------------------------------------------------------------
def cmd_init(args) -> int:
    path = Path(args.config or "sweep.toml")
    if path.exists() and not args.force:
        say(f"{path} already exists (use --force to overwrite)")
        return 1
    path.write_text(CONFIG_TEMPLATE)
    path.chmod(0o600)
    say(f"Wrote {path}. Edit [account] me and [protect], then run `inbox-sweep index`.")
    return 0


def cmd_auth(args) -> int:
    cfg = _config(args)
    backend = get_backend(cfg, "gmail")
    say(f"Signed in as {backend.account()}")
    return 0


def cmd_index(args) -> int:
    cfg = _config(args)
    backend = get_backend(cfg)
    with _store(cfg) as store:
        if args.fresh:
            store.clear_messages()
        account = backend.account()
        say(f"Indexing {account} via {backend.name} (headers only)...")
        buffer, total = [], 0
        for msg in backend.iter_messages(query=args.query, limit=args.limit, progress=say):
            buffer.append(msg)
            if len(buffer) >= 1000:
                total += store.upsert_messages(buffer)
                buffer.clear()
        total += store.upsert_messages(buffer)
        store.set_meta("account", account)
        store.set_meta("backend", backend.name)
        if hasattr(backend, "uidvalidities"):
            store.set_meta("uidvalidity", backend.uidvalidities())
        backend.close()
        print(f"Indexed {total:,} messages ({store.message_count():,} in the index).")
    return 0


def cmd_classify(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        messages = store.messages()
        if not messages:
            raise PlanError("The index is empty. Run `inbox-sweep index` first.")
        if args.overrides:
            data = json.loads(Path(args.overrides).read_text())
            overrides = data.get("overrides", data)
            store.set_overrides({k: v for k, v in overrides.items() if v in ("keep", "trash")}, replace=True)
            say(f"Loaded {len(store.overrides())} sender decisions from {args.overrides}")

        def ai_verdicts() -> dict:
            return {s: v for s, v in store.ai_decisions().items() if v["action"] in ("keep", "trash")}

        decisions = classify(messages, cfg, overrides=store.overrides(), ai=ai_verdicts())
        if args.ai or cfg.ai_enabled:
            from inbox_sweep.ai import run_ai
            run_ai(store, cfg, messages, decisions, progress=say, max_groups=args.ai_max)
            decisions = classify(messages, cfg, overrides=store.overrides(), ai=ai_verdicts())
        store.save_decisions(decisions)
        _print_summary(summarize(messages, decisions))
        print("\nNext: `inbox-sweep review --open`, then `inbox-sweep plan`.")
    return 0


def cmd_stats(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        _print_summary(summarize(store.messages(), _decisions(store)))
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


def cmd_plan(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        messages, decisions = store.messages(), _decisions(store)
        plan = build_plan(messages, decisions, cfg, account=store.get_meta("account", ""),
                          backend=store.get_meta("backend", cfg.backend),
                          uidvalidity=store.get_meta("uidvalidity", {}))
        problems = verify_plan(plan, messages, decisions, cfg)
        if problems:
            say("Refusing to write a plan that touches protected mail:")
            for p in problems[:20]:
                say("  " + p)
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
    with _store(cfg) as store:
        messages, decisions = store.messages(), _decisions(store)
        problems = verify_plan(plan, messages, decisions, cfg)
        if problems:
            say("Plan failed the safety check; nothing was changed:")
            for p in problems[:20]:
                say("  " + p)
            return 2
        s = plan["summary"]
        print(f"Account: {plan.get('account') or '(unknown)'}  backend: {plan.get('backend')}")
        print(f"Will move {s['trash_threads']:,} threads ({s['trash_messages']:,} messages) to Trash "
              f"and label {s['keep_messages']:,} kept messages '{plan.get('keep_label')}'.")
        for e in plan["trash"][:10]:
            print(f"  - {e['sender']}: {e['subject'][:70]}  [{e['category']}]")
        if not args.yes:
            print("\nDry run. Nothing changed. Re-run with --yes to apply.")
            return 0
        backend = get_backend(cfg, plan.get("backend") or None)
        result = apply_plan(plan, backend, cfg, messages, label_keep=not args.no_label, progress=say)
        backend.close()
    print(f"Moved {result['trashed']:,} threads to Trash ({result['already']:,} done earlier, {result['failed']:,} failed).")
    print(f"Recoverable from Trash until about {result['recoverable_until']}.")
    print(f"Undo: inbox-sweep undo {result['log']} --yes")
    return 0 if not result["failed"] else 1


def cmd_undo(args) -> int:
    cfg = _config(args)
    if not args.yes:
        print(f"This restores every thread listed in {args.log}. Re-run with --yes.")
        return 0
    backend = get_backend(cfg)
    result = undo(args.log, backend, cfg, progress=say)
    backend.close()
    print(f"Restored {result['restored']:,} of {result['total']:,} threads.")
    return 0


def cmd_unsubscribe(args) -> int:
    cfg = _config(args)
    with _store(cfg) as store:
        cands = unsub.candidates(store.messages(), _decisions(store))
    one_click = [c for c in cands if c["method"] == "one-click"]
    print(f"{len(cands)} senders are entirely going to Trash and have unsubscribe headers; "
          f"{len(one_click)} support one-click.")
    for c in cands[:50]:
        print(f"  {c['messages']:>5}  {c['method']:<9} {c['sender']}")
    if not args.yes:
        print("\nDry run. Re-run with --yes to send one-click unsubscribes (others are listed for you).")
        return 0
    result = unsub.run(cands, cfg.data_path / "unsubscribe-log.jsonl", progress=say)
    print(f"Unsubscribed {result['ok']}, failed {result['failed']}, manual {result['manual']}.")
    return 0


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
                .replace('type = "gmail"', 'type = "demo"')
                .replace('data_dir = ".inbox-sweep"', f'data_dir = "{base.as_posix()}"'))
    cfg_path.write_text(cfg_text)
    ns = argparse.Namespace(config=str(cfg_path), backend=None, fresh=True, query=None, limit=None,
                            overrides=None, ai=False, ai_max=None, out=None, open=args.open)
    cmd_index(ns)
    cmd_classify(ns)
    cmd_review(ns)
    cmd_plan(ns)
    print(f"\nTry: inbox-sweep --config {cfg_path} apply {base / 'plan.json'} --yes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inbox-sweep", description=__doc__)
    p.add_argument("--version", action="version", version=f"inbox-sweep {__version__}")
    p.add_argument("--config", help="path to sweep.toml (default: ./sweep.toml)")
    p.add_argument("--backend", choices=["gmail", "imap", "demo"], help="override [backend] type")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter sweep.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    sub.add_parser("auth", help="sign in to Gmail (opens a browser once)").set_defaults(func=cmd_auth)

    s = sub.add_parser("index", help="read headers into the local index")
    s.add_argument("--query", help="limit to a search (Gmail syntax for gmail and Gmail-over-IMAP)")
    s.add_argument("--limit", type=int)
    s.add_argument("--fresh", action="store_true", help="drop the existing index first")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("classify", help="decide keep/trash for every message")
    s.add_argument("--overrides", help="decisions.json from the review page")
    s.add_argument("--ai", action="store_true", help="ask Claude about ambiguous senders")
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
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("undo", help="restore threads from an apply log")
    s.add_argument("log")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("unsubscribe", help="one-click unsubscribe from senders being fully trashed")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_unsubscribe)

    s = sub.add_parser("queries", help="Gmail searches for browser mode, from sweep.toml")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_queries)

    s = sub.add_parser("demo", help="try the whole pipeline on a fake mailbox")
    s.add_argument("--dir", default=".inbox-sweep-demo")
    s.add_argument("--open", action="store_true", help="open the review page")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, BackendError, PlanError, RuntimeError) as exc:
        say(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        say("interrupted; `apply` resumes where it stopped")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

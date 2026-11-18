"""End-to-end check against a real IMAP server (Dovecot in CI).

Seeds a small mailbox, then drives the real CLI: doctor, index (twice, the
second must be a no-op), classify, plan, a message starred *after* planning,
apply, undo. Asserts exactly what moved, that protected mail never moved, and
that message bodies survive the round trip.

    python scripts/ci_imap_e2e.py --host 127.0.0.1 --port 143 --user me@ci.test --password secret

Use a throwaway account: it empties the mailbox first.
"""

from __future__ import annotations

import argparse
import contextlib
import imaplib
import io
import os
import re
import sys
import tempfile
from pathlib import Path

from inbox_sweep.cli import main
from inbox_sweep.wizard import _set, render_config

LIST_HEADERS = ("List-Unsubscribe: <https://lists.example/u>", "List-Id: <news.lists.example>", "Precedence: bulk")


def raw(sender: str, subject: str, msgid: str, *, to: str, refs: str = "", extra=()) -> bytes:
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {subject}", "Date: Tue, 10 Mar 2015 09:00:00 +0000",
             f"Message-ID: {msgid}"]
    if refs:
        lines += [f"In-Reply-To: {refs}", f"References: {refs}"]
    lines += list(extra)
    return ("\r\n".join(lines) + "\r\n\r\nBody of " + msgid + "\r\n").encode()


def seed(me: str) -> list[tuple[str, str, bytes, str]]:
    """(folder, flags, message, expected) where expected is trash | keep | starred-later."""
    return [
        ("INBOX", "", raw("Weekly Reads <digest@weekly-reads.example>", "This week's essays", "<news1@ci>",
                          to=me, extra=LIST_HEADERS), "trash"),
        ("INBOX", "", raw("Shoe Shop <deals@shoe-shop.example>", "40% off this weekend", "<promo1@ci>",
                          to=me, extra=LIST_HEADERS[:1]), "trash"),
        ("INBOX", "", raw("Example Bank <no-reply@bank.example>", "Your verification code is 482913",
                          "<code1@ci>", to=me), "trash"),
        ("INBOX", "", raw("Startup News <hello@startup-news.example>", "Funding roundup", "<news2@ci>",
                          to=me, extra=LIST_HEADERS), "starred-later"),
        ("INBOX", "", raw("Mom <mom@family.example>", "Dinner Sunday?", "<fam1@ci>", to=me), "keep"),
        ("INBOX", "", raw("Sam <sam.friend@gmail.com>", "Photos from the trip", "<trip@ci>", to=me), "keep"),
        ("Sent", "\\Seen", raw(f"Me <{me}>", "Re: Photos from the trip", "<trip-re@ci>",
                               to="sam.friend@gmail.com", refs="<trip@ci>"), "keep"),
        ("Taxes", "", raw("Revenue Agency <noreply@revenue.gov.example>", "Your 2014 notice of assessment",
                          "<tax1@ci>", to=me), "keep"),
        ("INBOX", "\\Flagged", raw("Shoe Shop <deals@shoe-shop.example>", "Your exclusive code", "<promo2@ci>",
                                   to=me, extra=LIST_HEADERS[:1]), "keep"),
    ]


class Mailbox:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.c = imaplib.IMAP4(host, port)
        self.c.login(user, password)

    def ok(self, typ, data):
        assert typ == "OK", data
        return data

    def reset(self, folders: list[str]) -> None:
        for folder in folders:
            self.c.create(folder)  # NO if it exists; fine
            self.ok(*self.c.select(folder))
            typ, data = self.c.uid("SEARCH", None, "ALL")
            if data and data[0]:
                self.ok(*self.c.uid("STORE", b",".join(data[0].split()).decode(), "+FLAGS.SILENT", "(\\Deleted)"))
                self.ok(*self.c.expunge())

    def append(self, folder: str, flags: str, message: bytes) -> None:
        when = imaplib.Time2Internaldate(1425978000)  # 2015-03-10, matching the Date header
        self.ok(*self.c.append(folder, f"({flags})" if flags else None, when, message))

    def ids(self, folder: str) -> set[str]:
        self.ok(*self.c.select(folder, readonly=True))
        typ, data = self.c.uid("SEARCH", None, "ALL")
        if not data or not data[0]:
            return set()
        uids = b",".join(data[0].split()).decode()
        typ, rows = self.c.uid("FETCH", uids, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        found = set()
        for row in rows:
            if isinstance(row, tuple):
                m = re.search(rb"<[^>]+>", row[1])
                if m:
                    found.add(m.group().decode())
        return found

    def body(self, folder: str, msgid: str) -> bytes:
        self.ok(*self.c.select(folder, readonly=True))
        typ, data = self.c.uid("SEARCH", None, "HEADER", "Message-ID", msgid)
        uid = data[0].split()[0].decode()
        typ, rows = self.c.uid("FETCH", uid, "(BODY.PEEK[TEXT])")
        return next(r[1] for r in rows if isinstance(r, tuple))

    def flag(self, folder: str, msgid: str) -> None:
        self.ok(*self.c.select(folder))
        typ, data = self.c.uid("SEARCH", None, "HEADER", "Message-ID", msgid)
        self.ok(*self.c.uid("STORE", data[0].split()[0].decode(), "+FLAGS", "(\\Flagged)"))


def run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    text = out.getvalue() + err.getvalue()
    print(f"$ inbox-sweep {' '.join(argv)}  -> {code}\n{text}")
    return code, text


def check(condition: bool, what: str) -> None:
    if not condition:
        print(f"FAILED: {what}")
        sys.exit(1)
    print(f"ok: {what}")


def main_e2e() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=143)
    ap.add_argument("--user", default="me@ci.test")
    ap.add_argument("--password", default="secret")
    args = ap.parse_args()

    box = Mailbox(args.host, args.port, args.user, args.password)
    messages = seed(args.user)
    box.reset(["INBOX", "Sent", "Taxes", "Trash"])
    for folder, flags, message, _ in messages:
        box.append(folder, flags, message)
    expected = {re.search(rb"Message-ID: (<[^>]+>)", raw_).group(1).decode(): want for _, _, raw_, want in messages}
    original = {f: box.ids(f) for f in ("INBOX", "Sent", "Taxes", "Trash")}
    check(sum(len(v) for v in original.values()) == len(messages) and not original["Trash"], "mailbox seeded")

    work = Path(tempfile.mkdtemp())
    config = work / "sweep.toml"
    text = render_config(provider="imap", me=[args.user], imap_host=args.host, imap_port=args.port,
                         imap_tls="none", imap_user=args.user, people=["mom@family.example"], labels=["Taxes"])
    text = _set(text, "paths", "data_dir", str(work / "data"))
    config.write_text(text, encoding="utf-8")
    config.chmod(0o600)
    os.environ["INBOX_SWEEP_IMAP_PASSWORD"] = args.password
    c = ["--config", str(config)]

    code, out = run(*c, "doctor")
    check(code == 0, "doctor passes")
    code, out = run(*c, "index")
    check(code == 0 and f"{len(messages)} new" in out, "first index reads every message")
    code, out = run(*c, "index")
    check(code == 0 and "0 new" in out and "0 relabelled" in out and "0 removed" in out, "second index is a no-op")
    code, out = run(*c, "classify")
    check(code == 0, "classify")
    plan = work / "data" / "plan.json"
    code, out = run(*c, "plan")
    check(code == 0 and plan.exists(), "plan")
    planned = sum(1 for v in expected.values() if v in ("trash", "starred-later"))
    check(f"Plan: {planned} threads" in out, f"plan has the {planned} expected threads")

    box.flag("INBOX", "<news2@ci>")  # starred on the phone after planning
    code, out = run(*c, "apply", str(plan), "--yes")
    check(code == 0 and "1 skipped because they're protected now" in out, "apply skips the newly starred thread")

    after = {f: box.ids(f) for f in ("INBOX", "Sent", "Taxes", "Trash")}
    want_trash = {m for m, v in expected.items() if v == "trash"}
    check(after["Trash"] == want_trash, "exactly the expected messages are in Trash")
    for m, v in expected.items():
        if v != "trash":
            check(m not in after["Trash"], f"{m} ({v}) was not touched")
    check(after["Sent"] == original["Sent"] and after["Taxes"] == original["Taxes"], "Sent and Taxes untouched")

    log = next((work / "data").glob("trash-log-*.jsonl"))
    code, out = run(*c, "undo", str(log), "--yes")
    check(code == 0 and f"Restored {len(want_trash)} of {len(want_trash)}" in out, "undo restores every thread")
    restored = {f: box.ids(f) for f in ("INBOX", "Sent", "Taxes", "Trash")}
    check(restored == original, "mailbox is back to its original state")
    check(box.body("INBOX", "<promo1@ci>") == b"Body of <promo1@ci>\r\n", "message bodies survive the round trip")
    print("\nIMAP end-to-end: all checks passed")


if __name__ == "__main__":
    main_e2e()

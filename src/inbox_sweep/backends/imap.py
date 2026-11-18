"""IMAP backend: works with Gmail (app password), Fastmail, iCloud, Yahoo and most hosts.

Only headers are fetched (BODY.PEEK, so nothing is marked read). On Gmail the
X-GM-EXT-1 extension gives real thread ids and labels and the scan uses the
"All Mail" folder; elsewhere every folder except Trash, Junk and Drafts is scanned
and threads are rebuilt from References / In-Reply-To headers.

Trash means moving to the server's Trash folder (special-use \\Trash). Nothing is
ever expunged outside the plan's own messages.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
import time
from email import message_from_bytes
from typing import Iterator

from inbox_sweep.backends.base import Backend, BackendError, Progress, chunked
from inbox_sweep.headers import decode, parse_address_list, parse_date, parse_from
from inbox_sweep.model import DRAFT, INBOX, SENT, STARRED, Message

FETCH_FIELDS = (
    "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
    "LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE AUTO-SUBMITTED"
)
FETCH_CHUNK = 300

GMAIL_LABEL_MAP = {
    "\\inbox": INBOX, "\\sent": SENT, "\\starred": STARRED, "\\important": "IMPORTANT",
    "\\draft": DRAFT, "\\trash": "TRASH", "\\spam": "SPAM",
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_TOKEN_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"|([^\s()"]+)')


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: bytes) -> str:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return raw.decode("utf-8", "replace")


def parse_list_line(line: bytes) -> tuple[set[str], str] | None:
    """Parse one LIST response line into (lower-cased flags, mailbox name)."""
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    flags = {f.lower() for f in m.group("flags").decode().split()}
    return flags, _unquote(m.group("name"))


def parse_label_list(raw: bytes) -> list[str]:
    return [
        (q.replace(b'\\"', b'"').replace(b"\\\\", b"\\") if q is not None else a).decode("utf-8", "replace")
        for q, a in ((m.group(1), m.group(2)) for m in _TOKEN_RE.finditer(raw))
    ]


def parse_fetch_response(data: list) -> list[dict]:
    """Turn imaplib's FETCH output into one dict per message.

    imaplib returns tuples (b'<seq> (... BODY[...] {n}', header_bytes) and, when a
    server sends items after the literal, extra bytes like b' FLAGS (\\Seen))'.
    Those trailing pieces are joined back onto the record's metadata.
    """
    records: list[dict] = []
    current: dict | None = None
    for item in data:
        if isinstance(item, tuple):
            current = {"meta": item[0], "header": item[1]}
            records.append(current)
        elif isinstance(item, bytes) and current is not None:
            current["meta"] += b" " + item
    out = []
    for rec in records:
        meta = rec["meta"]
        uid = re.search(rb"UID (\d+)", meta)
        if not uid:
            continue
        flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
        thrid = re.search(rb"X-GM-THRID (\d+)", meta)
        msgid = re.search(rb"X-GM-MSGID (\d+)", meta)
        labels = re.search(rb'X-GM-LABELS \(((?:[^()"]|"(?:[^"\\]|\\.)*")*)\)', meta)
        internal = imaplib.Internaldate2tuple(meta)
        out.append({
            "uid": int(uid.group(1)),
            "flags": [f.lower() for f in flags.group(1).decode().split()] if flags else [],
            "thrid": thrid.group(1).decode() if thrid else "",
            "msgid": msgid.group(1).decode() if msgid else "",
            "labels": parse_label_list(labels.group(1)) if labels else [],
            "internaldate": int(time.mktime(internal)) if internal else 0,
            "header": rec["header"] or b"",
        })
    return out


def thread_root(headers, fallback: str) -> str:
    refs = (headers.get("References") or "").split()
    root = refs[0] if refs else (headers.get("In-Reply-To") or "").strip() or (headers.get("Message-ID") or "").strip()
    if not root:
        return fallback
    return "t-" + hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:16]


def record_to_message(rec: dict, folder: str, uidvalidity: str, gmail: bool,
                      folder_flags: set[str]) -> Message:
    headers = message_from_bytes(rec["header"])
    name, email = parse_from(headers.get("From"))
    labels: list[str] = []
    if gmail:
        for lab in rec["labels"]:
            labels.append(GMAIL_LABEL_MAP.get(lab.lower(), lab))
    else:
        if folder.upper() == "INBOX":
            labels.append(INBOX)
        elif "\\sent" in folder_flags:
            labels.append(SENT)
        else:
            labels.append(folder)  # treat folders like labels so [protect] labels can match them
    if "\\flagged" in rec["flags"]:
        labels.append(STARRED)
    if "\\draft" in rec["flags"]:
        labels.append(DRAFT)
    fallback_id = f"{folder}|{uidvalidity}|{rec['uid']}"
    msg_id = f"gm-{rec['msgid']}" if gmail and rec["msgid"] else fallback_id
    thread = f"gt-{rec['thrid']}" if gmail and rec["thrid"] else thread_root(headers, fallback_id)
    return Message(
        id=msg_id,
        thread_id=thread,
        date=rec["internaldate"] or parse_date(headers.get("Date")),
        from_email=email,
        from_name=name,
        to=parse_address_list(headers.get("To")),
        cc=parse_address_list(headers.get("Cc")),
        subject=decode(headers.get("Subject")),
        labels=sorted(set(labels)),
        list_unsubscribe=headers.get("List-Unsubscribe", "") or "",
        list_unsubscribe_post=headers.get("List-Unsubscribe-Post", "") or "",
        list_id=headers.get("List-Id", "") or "",
        precedence=headers.get("Precedence", "") or "",
        auto_submitted=headers.get("Auto-Submitted", "") or "",
        message_id_header=(headers.get("Message-ID") or "").strip(),
        folder=folder,
        uid=rec["uid"],
    )


class ImapBackend(Backend):
    name = "imap"

    def __init__(self, host: str, port: int, user: str, password: str):
        self.user = user
        try:
            self.imap = imaplib.IMAP4_SSL(host, port)
            self.imap.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise BackendError(f"IMAP login failed for {user}@{host}: {exc}") from exc
        caps = self.imap.capability()[1][0].decode().upper().split()
        self.caps = set(caps)
        self.gmail = "X-GM-EXT-1" in self.caps
        self.supports_labels = self.gmail
        self.folders = self._list_folders()
        self.trash_folder = self._special("\\trash", ("Trash", "[Gmail]/Trash", "Deleted Items", "Deleted Messages"))
        if not self.trash_folder:
            raise BackendError("Could not find a Trash folder on this server.")
        self.all_mail = self._special("\\all", ()) if self.gmail else ""
        self._uidvalidity: dict[str, str] = {}

    # -- folder helpers --------------------------------------------------------
    def _list_folders(self) -> list[tuple[set[str], str]]:
        typ, data = self.imap.list()
        if typ != "OK":
            raise BackendError("LIST failed")
        out = []
        for line in data:
            if isinstance(line, tuple):  # mailbox name sent as a literal: (b'... {12}', b'name')
                name = line[1].replace(b"\\", b"\\\\").replace(b'"', b'\\"')
                line = re.sub(rb"\{\d+\}$", b"", line[0].rstrip()) + b'"' + name + b'"'
            parsed = parse_list_line(line)
            if parsed:
                out.append(parsed)
        return out

    def _special(self, flag: str, names: tuple[str, ...]) -> str:
        for flags, name in self.folders:
            if flag in flags:
                return name
        lowered = {name.lower(): name for _, name in self.folders}
        for candidate in names:
            if candidate.lower() in lowered:
                return lowered[candidate.lower()]
        return ""

    def _scan_folders(self) -> list[tuple[set[str], str]]:
        if self.gmail and self.all_mail:
            return [(f, n) for f, n in self.folders if n == self.all_mail]
        skip_flags = {"\\trash", "\\junk", "\\drafts", "\\noselect", "\\nonexistent", "\\all"}
        skip_names = {"trash", "junk", "spam", "drafts", "deleted items", "deleted messages", "junk e-mail"}
        return [(f, n) for f, n in self.folders
                if not (f & skip_flags) and n.lower() not in skip_names]

    def _select(self, folder: str, readonly: bool) -> str:
        typ, _ = self.imap.select(quote(folder), readonly=readonly)
        if typ != "OK":
            raise BackendError(f"Cannot open folder {folder}")
        uv = self.imap.response("UIDVALIDITY")[1]
        value = uv[0].decode() if uv and uv[0] else ""
        if value:
            self._uidvalidity[folder] = value
        return value

    # -- Backend API --------------------------------------------------------------
    def account(self) -> str:
        return self.user

    def iter_messages(self, query=None, limit=None, progress: Progress = None) -> Iterator[Message]:
        say = progress or (lambda _m: None)
        items = f"(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FETCH_FIELDS})]"
        items += " X-GM-THRID X-GM-MSGID X-GM-LABELS)" if self.gmail else ")"
        yielded = 0
        for flags, folder in self._scan_folders():
            uidvalidity = self._select(folder, readonly=True)
            if self.gmail and query:
                typ, data = self.imap.uid("SEARCH", "X-GM-RAW", quote(query))
            else:
                typ, data = self.imap.uid("SEARCH", None, "ALL")
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            uids.reverse()  # newest first
            say(f"{folder}: {len(uids)} messages")
            read = 0
            for group in chunked(uids, FETCH_CHUNK):
                typ, data = self.imap.uid("FETCH", b",".join(group).decode(), items)
                if typ != "OK":
                    raise BackendError(f"FETCH failed in {folder}")
                for rec in parse_fetch_response(data):
                    msg = record_to_message(rec, folder, uidvalidity, self.gmail, flags)
                    if DRAFT in msg.labels:
                        continue
                    yield msg
                    yielded += 1
                    if limit and yielded >= limit:
                        return
                read += len(group)
                say(f"{folder}: read {read}/{len(uids)}")

    def uidvalidities(self) -> dict[str, str]:
        """UIDVALIDITY per folder seen while indexing; stored so later moves can be checked."""
        return dict(self._uidvalidity)

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        if not self.gmail:
            if progress:
                progress("This IMAP server has no labels; skipping the keep label.")
            return
        self._select(self.all_mail, readonly=False)
        uids = [str(m.uid) for m in messages if m.folder == self.all_mail]
        for group in chunked(uids, 500):
            typ, _ = self.imap.uid("STORE", ",".join(group), "+X-GM-LABELS", f"({quote(label)})")
            if typ != "OK":
                raise BackendError("Adding the keep label failed")

    def _move(self, uids: list[str], dest: str) -> None:
        uidset = ",".join(uids)
        if "MOVE" in self.caps:
            typ, _ = self.imap.uid("MOVE", uidset, quote(dest))
        elif "UIDPLUS" in self.caps:
            typ, _ = self.imap.uid("COPY", uidset, quote(dest))
            if typ == "OK":
                typ, _ = self.imap.uid("STORE", uidset, "+FLAGS.SILENT", "(\\Deleted)")
            if typ == "OK":
                typ, _ = self.imap.uid("EXPUNGE", uidset)
        else:
            raise BackendError("Server supports neither MOVE nor UIDPLUS; refusing to expunge blindly.")
        if typ != "OK":
            raise BackendError(f"Moving messages to {dest} failed")

    def trash(self, entries, progress: Progress = None) -> list[str]:
        by_folder: dict[str, list[tuple[str, dict]]] = {}
        for entry in entries:
            for ref in entry["messages"]:
                by_folder.setdefault(ref["folder"], []).append((entry["thread_id"], ref))
        done_threads: set[str] = set()
        failed_threads: set[str] = set()
        for folder, refs in by_folder.items():
            current = self._select(folder, readonly=False)
            for tid, ref in refs:
                expected = ref.get("uidvalidity") or ""
                if expected and current and expected != current:
                    raise BackendError(f"{folder} changed since indexing (UIDVALIDITY). Run `inbox-sweep index` again.")
            for group in chunked(refs, 200):
                try:
                    self._move([str(ref["uid"]) for _, ref in group], self.trash_folder)
                    done_threads.update(tid for tid, _ in group)
                except BackendError:
                    failed_threads.update(tid for tid, _ in group)
        return [e["thread_id"] for e in entries
                if e["thread_id"] in done_threads and e["thread_id"] not in failed_threads]

    def restore(self, entries, progress: Progress = None) -> list[str]:
        self._select(self.trash_folder, readonly=False)
        restored = []
        for entry in entries:
            ok = True
            for ref in entry["messages"]:
                if self.gmail and ref["id"].startswith("gm-"):
                    typ, data = self.imap.uid("SEARCH", "X-GM-MSGID", ref["id"][3:])
                elif ref.get("message_id_header"):
                    typ, data = self.imap.uid("SEARCH", None, "HEADER", "Message-ID", quote(ref["message_id_header"]))
                else:
                    ok = False
                    continue
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                if not uids:
                    ok = False
                    continue
                # Gmail restores to the Inbox (like the web UI's "Move to Inbox");
                # other servers get the message back in its original folder.
                dest = "INBOX" if self.gmail else ref["folder"]
                try:
                    self._move([u.decode() for u in uids], dest)
                except BackendError:
                    ok = False
            if ok:
                restored.append(entry["thread_id"])
        return restored

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass

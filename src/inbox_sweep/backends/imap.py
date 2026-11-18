"""IMAP backend: Gmail (app password), Fastmail, iCloud, Yahoo, Proton Mail Bridge, most hosts.

Only headers are fetched (BODY.PEEK, so nothing is marked read). On Gmail the
X-GM-EXT-1 extension gives real thread ids and labels and the scan uses "All Mail";
elsewhere every folder except Trash, Junk and Drafts is scanned and threads are
rebuilt from References / In-Reply-To headers.

Re-indexing is incremental: one cheap FETCH of flags (and Gmail labels) per
folder, then headers only for messages not yet indexed. There is no per-message
quota, so this is the fastest way to index a big Gmail inbox.

Trash means moving to the server's Trash folder (special-use \\Trash). Nothing is
ever expunged except the plan's own messages, and only where the server supports
MOVE or UIDPLUS.
"""

from __future__ import annotations

import hashlib
import imaplib
import re
import ssl
import time
from email import message_from_bytes
from typing import Callable, Iterator

from inbox_sweep.backends.base import Backend, BackendError, LiveProtection, Progress, chunked
from inbox_sweep.headers import decode, parse_address_list, parse_date, parse_from
from inbox_sweep.model import DRAFT, INBOX, SENT, STARRED, TRASH, Message

FETCH_FIELDS = (
    "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
    "LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE AUTO-SUBMITTED"
)
HEADER_CHUNK = 300
META_CHUNK = 2000
MOVE_CHUNK = 500
GMAIL_ITEMS = " X-GM-THRID X-GM-MSGID X-GM-LABELS"

GMAIL_LABEL_MAP = {
    "\\inbox": INBOX, "\\sent": SENT, "\\starred": STARRED, "\\important": "IMPORTANT",
    "\\draft": DRAFT, "\\trash": TRASH, "\\spam": "SPAM",
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_TOKEN_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"|([^\s()"]+)')
_RECORD_START = re.compile(rb"^\d+ \(")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


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


def uid_sets(uids: list[int], max_chars: int = 900) -> list[str]:
    """Compress UIDs into IMAP sequence sets ("1:5,9,12:20"), each at most max_chars long."""
    out, parts, length = [], [], 0
    ordered = sorted(set(uids))
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        part = str(ordered[i]) if i == j else f"{ordered[i]}:{ordered[j]}"
        if parts and length + len(part) + 1 > max_chars:
            out.append(",".join(parts))
            parts, length = [], 0
        parts.append(part)
        length += len(part) + 1
        i = j + 1
    if parts:
        out.append(",".join(parts))
    return out


def parse_fetch_response(data: list) -> list[dict]:
    """Turn imaplib's FETCH output into one dict per message.

    Records with a header literal arrive as tuples (b'<seq> (... {n}', header_bytes),
    possibly followed by trailing bytes like b' FLAGS (\\Seen))' when the server sends
    items after the literal. Records without a literal arrive as plain bytes.
    """
    records: list[dict] = []
    current: dict | None = None
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            meta, literal = item[0], item[1]
            if current is not None and not _RECORD_START.match(meta):
                current["meta"] += b" " + meta  # a second literal inside the same record
                continue
            current = {"meta": meta, "header": literal}
            records.append(current)
        elif isinstance(item, bytes):
            if _RECORD_START.match(item):
                current = {"meta": item, "header": b""}
                records.append(current)
            elif current is not None:
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


def message_labels(rec: dict, folder: str, folder_flags: set[str], gmail: bool, trash_folder: str) -> list[str]:
    labels: list[str] = []
    if gmail:
        labels += [GMAIL_LABEL_MAP.get(lab.lower(), lab) for lab in rec["labels"]]
    elif folder.upper() == "INBOX":
        labels.append(INBOX)
    elif "\\sent" in folder_flags:
        labels.append(SENT)
    elif folder != trash_folder:
        labels.append(folder)  # folders act as labels so [protect] labels can match them
    if folder == trash_folder:
        labels.append(TRASH)
    if "\\flagged" in rec["flags"]:
        labels.append(STARRED)
    if "\\draft" in rec["flags"]:
        labels.append(DRAFT)
    return sorted(set(labels))


def message_id_for(rec: dict, folder: str, uidvalidity: str, gmail: bool) -> str:
    if gmail and rec["msgid"]:
        return f"gm-{rec['msgid']}"
    return f"{folder}|{uidvalidity}|{rec['uid']}"


def record_to_message(rec: dict, folder: str, uidvalidity: str, gmail: bool,
                      folder_flags: set[str], trash_folder: str = "") -> Message:
    headers = message_from_bytes(rec["header"])
    name, email = parse_from(headers.get("From"))
    msg_id = message_id_for(rec, folder, uidvalidity, gmail)
    fallback = f"{folder}|{uidvalidity}|{rec['uid']}"
    thread = f"gt-{rec['thrid']}" if gmail and rec["thrid"] else thread_root(headers, fallback)
    return Message(
        id=msg_id,
        thread_id=thread,
        date=rec["internaldate"] or parse_date(headers.get("Date")),
        from_email=email,
        from_name=name,
        to=parse_address_list(headers.get("To")),
        cc=parse_address_list(headers.get("Cc")),
        subject=decode(headers.get("Subject")),
        labels=message_labels(rec, folder, folder_flags, gmail, trash_folder),
        list_unsubscribe=headers.get("List-Unsubscribe", "") or "",
        list_unsubscribe_post=headers.get("List-Unsubscribe-Post", "") or "",
        list_id=headers.get("List-Id", "") or "",
        precedence=headers.get("Precedence", "") or "",
        auto_submitted=headers.get("Auto-Submitted", "") or "",
        message_id_header=(headers.get("Message-ID") or "").strip(),
        folder=folder,
        uid=rec["uid"],
    )


def is_local_host(host: str) -> bool:
    host = host.lower().strip("[]")
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def connect(host: str, port: int, tls: str = "ssl", verify: bool = True, ca_file: str = "",
            timeout: float = 60) -> imaplib.IMAP4:
    """Open an IMAP connection. Plaintext or unverified TLS only to this machine."""
    if tls not in ("ssl", "starttls", "none"):
        raise BackendError("[imap] tls must be 'ssl', 'starttls' or 'none'")
    if (tls == "none" or not verify) and not is_local_host(host):
        raise BackendError("Unencrypted or unverified IMAP is only allowed to this machine "
                           "(e.g. a local bridge). Use tls = 'ssl' or 'starttls' with verification.")
    ctx = ssl.create_default_context(cafile=ca_file or None)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        if tls == "ssl":
            return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        if tls == "starttls":
            conn.starttls(ssl_context=ctx)
        return conn
    except (OSError, imaplib.IMAP4.error) as exc:
        raise BackendError(f"Could not connect to {host}:{port} ({tls}): {exc}") from exc


class ImapBackend(Backend):
    name = "imap"

    def __init__(self, host: str, port: int, user: str, password: str, tls: str = "ssl",
                 verify: bool = True, ca_file: str = "",
                 connect_fn: Callable[[], imaplib.IMAP4] | None = None):
        self.user = user
        self.imap = connect_fn() if connect_fn else connect(host, port, tls, verify, ca_file)
        try:
            self.imap.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise BackendError(f"IMAP login failed for {user}@{host}: {exc}") from exc
        typ, caps = self.imap.capability()
        self.caps = set(caps[0].decode().upper().split()) if typ == "OK" and caps and caps[0] else set()
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
            if line is None:
                continue
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

    def _flags_of(self, folder: str) -> set[str]:
        for flags, name in self.folders:
            if name == folder:
                return flags
        return set()

    def _scan_folders(self, include_trash: bool = False) -> list[tuple[set[str], str]]:
        if self.gmail and self.all_mail:
            names = [self.all_mail] + ([self.trash_folder] if include_trash else [])
            return [(f, n) for f, n in self.folders if n in names]
        skip_flags = {"\\junk", "\\drafts", "\\noselect", "\\nonexistent", "\\all"}
        skip_names = {"junk", "spam", "drafts", "junk e-mail"}
        if not include_trash:
            skip_flags.add("\\trash")
            skip_names |= {"trash", "deleted items", "deleted messages"}
        out = []
        for f, n in self.folders:
            if n == self.trash_folder and include_trash:
                out.append((f, n))
            elif not (f & skip_flags) and n.lower() not in skip_names and n != self.trash_folder:
                out.append((f, n))
        return out

    def _select(self, folder: str, readonly: bool) -> str:
        typ, _ = self.imap.select(quote(folder), readonly=readonly)
        if typ != "OK":
            raise BackendError(f"Cannot open folder {folder}")
        uv = self.imap.response("UIDVALIDITY")[1]
        value = uv[0].decode() if uv and uv[0] else ""
        if value:
            self._uidvalidity[folder] = value
        return value

    def _search(self, query: str | None = None, *criteria: str) -> list[int]:
        if query and self.gmail:
            typ, data = self.imap.uid("SEARCH", "X-GM-RAW", quote(query))
        elif criteria:
            typ, data = self.imap.uid("SEARCH", None, *criteria)
        else:
            typ, data = self.imap.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(u) for u in data[0].split()]

    def _fetch(self, uids: list[int], items: str, chunk: int) -> list[dict]:
        out: list[dict] = []
        for group in chunked(sorted(uids), chunk):
            for uidset in uid_sets(group):
                typ, data = self.imap.uid("FETCH", uidset, items)
                if typ != "OK":
                    raise BackendError("FETCH failed")
                out.extend(parse_fetch_response(data))
        return out

    def _header_items(self) -> str:
        return f"(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FETCH_FIELDS})]" + (GMAIL_ITEMS + ")" if self.gmail else ")")

    def _meta_items(self) -> str:
        return "(UID FLAGS" + (GMAIL_ITEMS + ")" if self.gmail else ")")

    # -- Backend API --------------------------------------------------------------
    def account(self) -> str:
        return self.user.lower()

    def iter_messages(self, query=None, limit=None, progress: Progress = None,
                      include_trash: bool = False) -> Iterator[Message]:
        for event in self.sync({}, query=query, limit=limit, progress=progress, include_trash=include_trash):
            if event[0] == "new":
                yield event[1]

    def sync(self, known, query=None, limit=None, progress: Progress = None, include_trash: bool = False):
        say = progress or (lambda _m: None)
        seen: set[str] = set()
        new_total = 0
        for flags, folder in self._scan_folders(include_trash):
            uidvalidity = self._select(folder, readonly=True)
            uids = self._search(query)
            if not uids:
                continue
            new_uids: list[int] = []
            for rec in self._fetch(uids, self._meta_items(), META_CHUNK):
                mid = message_id_for(rec, folder, uidvalidity, self.gmail)
                labels = message_labels(rec, folder, flags, self.gmail, self.trash_folder)
                if DRAFT in labels:
                    continue
                seen.add(mid)
                if mid in known:
                    if sorted(labels) != sorted(known[mid]):
                        yield ("labels", mid, labels)
                else:
                    new_uids.append(rec["uid"])
            new_uids.sort(reverse=True)  # newest first
            if limit:
                new_uids = new_uids[:max(0, limit - new_total)]
            say(f"{folder}: {len(uids)} messages, {len(new_uids)} new")
            started, read = time.monotonic(), 0
            for group in chunked(new_uids, HEADER_CHUNK):
                for rec in self._fetch(group, self._header_items(), HEADER_CHUNK):
                    msg = record_to_message(rec, folder, uidvalidity, self.gmail, flags, self.trash_folder)
                    if DRAFT in msg.labels:
                        continue
                    yield ("new", msg)
                    new_total += 1
                read += len(group)
                say(f"{folder}: read {read}/{len(new_uids)} ({read / max(time.monotonic() - started, 1e-9):.0f}/s)")
            if limit and new_total >= limit:
                return
        if query is None and not limit:
            for mid in known.keys() - seen:
                yield ("removed", mid)

    def uidvalidities(self) -> dict[str, str]:
        """UIDVALIDITY per folder seen while indexing; stored so later moves can be checked."""
        return dict(self._uidvalidity)

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        if not self.gmail:
            if progress:
                progress("This IMAP server has no labels; skipping the keep label.")
            return
        self._select(self.all_mail, readonly=False)
        uids = [m.uid for m in messages if m.folder == self.all_mail]
        for uidset in uid_sets(uids):
            typ, _ = self.imap.uid("STORE", uidset, "+X-GM-LABELS", f"({quote(label)})")
            if typ != "OK":
                raise BackendError("Adding the keep label failed")

    def _move(self, uids: list[int], dest: str) -> None:
        for uidset in uid_sets(uids):
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
            for _, ref in refs:
                expected = ref.get("uidvalidity") or ""
                if expected and current and expected != current:
                    raise BackendError(f"{folder} changed since indexing (UIDVALIDITY). Run `inbox-sweep index` again.")
            for group in chunked(refs, MOVE_CHUNK):
                try:
                    self._move([int(ref["uid"]) for _, ref in group], self.trash_folder)
                    done_threads.update(tid for tid, _ in group)
                except BackendError:
                    failed_threads.update(tid for tid, _ in group)
        return [e["thread_id"] for e in entries
                if e["thread_id"] in done_threads and e["thread_id"] not in failed_threads]

    def _trash_index(self) -> dict[str, int]:
        """Map Gmail message ids or Message-ID headers to UIDs in the Trash folder (one pass)."""
        uids = self._search()
        if not uids:
            return {}
        if self.gmail:
            return {f"gm-{r['msgid']}": r["uid"] for r in self._fetch(uids, "(UID X-GM-MSGID)", META_CHUNK) if r["msgid"]}
        out = {}
        for rec in self._fetch(uids, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])", HEADER_CHUNK):
            mid = (message_from_bytes(rec["header"]).get("Message-ID") or "").strip()
            if mid:
                out[mid] = rec["uid"]
        return out

    def restore(self, entries, progress: Progress = None) -> list[str]:
        self._select(self.trash_folder, readonly=False)
        index = self._trash_index()
        moves: dict[str, list[int]] = {}
        located: dict[str, bool] = {}
        for entry in entries:
            ok = True
            for ref in entry["messages"]:
                key = ref["id"] if self.gmail and ref["id"].startswith("gm-") else ref.get("message_id_header", "")
                uid = index.get(key)
                if uid is None:
                    ok = False
                    continue
                # Gmail restores to the Inbox (like the web UI's "Move to Inbox");
                # other servers get the message back in its original folder.
                moves.setdefault("INBOX" if self.gmail else ref["folder"], []).append(uid)
            located[entry["thread_id"]] = ok
        failed_dest: set[str] = set()
        for dest, uids in moves.items():
            try:
                self._move(uids, dest)
            except BackendError:
                failed_dest.add(dest)
        if failed_dest:
            return []
        return [tid for tid, ok in located.items() if ok]

    def _roots(self, uids: list[int], folder: str = "", uidvalidity: str = "") -> set[str]:
        """Thread ids for these uids in the selected folder, computed exactly as index does."""
        if not uids:
            return set()
        if self.gmail:
            return {f"gt-{r['thrid']}" for r in self._fetch(uids, "(UID X-GM-THRID)", META_CHUNK) if r["thrid"]}
        items = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)])"
        return {thread_root(message_from_bytes(r["header"]), f"{folder}|{uidvalidity}|{r['uid']}")
                for r in self._fetch(uids, items, HEADER_CHUNK)}

    def protected_thread_ids(self, protect: LiveProtection) -> set[str] | None:
        threads: set[str] = set()
        if self.gmail:
            from inbox_sweep.backends.gmail_api import protection_queries
            self._select(self.all_mail, readonly=True)
            for query in protection_queries(protect):
                threads |= self._roots(self._search(query))
            return threads
        for flags, folder in self._scan_folders():
            uv = self._select(folder, readonly=True)
            if protect.sent and "\\sent" in flags:
                threads |= self._roots(self._search(), folder, uv)  # every thread you sent in
                continue
            if folder in protect.labels:
                threads |= self._roots(self._search(), folder, uv)  # a protected folder
                continue
            uids: set[int] = set()
            if protect.starred:
                uids.update(self._search(None, "FLAGGED"))
            for person in protect.people:
                uids.update(self._search(None, "FROM", quote(person)))
                uids.update(self._search(None, "TO", quote(person)))
            for me in protect.me:  # mail *to* you is everything; only mail *from* you counts
                uids.update(self._search(None, "FROM", quote(me)))
            threads |= self._roots(sorted(uids), folder, uv)
        return threads

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass

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
MOVE or UIDPLUS. Before anything moves, each message's identity is checked on the
server (Gmail's X-GM-MSGID, or the Message-ID header), so a renumbered folder or a
stale plan can never move the wrong message. Where the server reports COPYUID,
the message's new UID in Trash is logged, so undo is exact.
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import re
import ssl
import time
from email.message import Message as EmailMessage
from email.parser import HeaderParser
from typing import Callable, Iterator

from inbox_sweep.backends.base import Backend, BackendError, LiveProtection, Progress, chunked
from inbox_sweep.headers import decode, parse_address_list, parse_date, parse_from
from inbox_sweep.model import DRAFT, INBOX, SENT, STARRED, TRASH, Message, label_matches

# imaplib refuses response lines over 1 MB, and one UID SEARCH over ~125,000
# messages is longer than that. The responses are still bounded by what we ask for.
imaplib._MAXLINE = max(getattr(imaplib, "_MAXLINE", 0), 64 * 1024 * 1024)

FETCH_FIELDS = (
    "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
    "LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE AUTO-SUBMITTED"
)
HEADER_CHUNK = 300
META_CHUNK = 2000
MOVE_CHUNK = 500
GMAIL_ITEMS = " X-GM-THRID X-GM-MSGID X-GM-LABELS"
MAX_RECONNECTS = 3

GMAIL_LABEL_MAP = {
    "\\inbox": INBOX, "\\sent": SENT, "\\starred": STARRED, "\\important": "IMPORTANT",
    "\\draft": DRAFT, "\\trash": TRASH, "\\spam": "SPAM",
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_TOKEN_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"|([^\s()"]+)')
_RECORD_START = re.compile(rb"^\d+ \(")
_HEADER_LITERAL = re.compile(rb"(?:BODY|BINARY)\[[^\]]*\](?:<\d+>)?\s*\{\d+\}$|RFC822\.HEADER\s*\{\d+\}$", re.I)
_QUOTED = re.compile(rb'"(?:[^"\\]|\\.)*"')
_LABELS = re.compile(rb'X-GM-LABELS \(((?:[^()"]|"(?:[^"\\]|\\.)*")*)\)')
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: bytes) -> str:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return raw.decode("utf-8", "replace")


# -- modified UTF-7 (RFC 3501 section 5.1.3): how IMAP spells non-ASCII folder names ----------
def decode_mutf7(name: str) -> str:
    """"Fam&AO0-lia" -> "Família". Malformed sequences are left as they are."""
    if "&" not in name:
        return name
    out, i = [], 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i + 1)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            try:
                out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def encode_mutf7(text: str) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            data = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            out.append("&" + data.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in text:
        if " " <= ch <= "~":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def folder_label(name: str, delim: str = "/") -> str:
    """A folder's name as a label: decoded, with "/" as the hierarchy separator."""
    label = decode_mutf7(name)
    if delim and delim != "/":
        label = label.replace(delim, "/")
    return label


# -- response parsing -------------------------------------------------------------------
def parse_list_entry(line: bytes) -> tuple[set[str], str, str] | None:
    """Parse one LIST response line into (lower-cased flags, wire mailbox name, delimiter)."""
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    flags = {f.lower() for f in m.group("flags").decode().split()}
    delim = m.group("delim")
    return flags, _unquote(m.group("name")), "" if delim == b"NIL" else _unquote(delim)


def parse_list_line(line: bytes) -> tuple[set[str], str] | None:
    entry = parse_list_entry(line)
    return (entry[0], entry[1]) if entry else None


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


def expand_uid_set(text: str) -> list[int]:
    """"5:7,9" -> [5, 6, 7, 9], in the order written (COPYUID pairs sets by position)."""
    out: list[int] = []
    for part in text.split(","):
        if ":" in part:
            a, b = (int(x) for x in part.split(":"))
            out.extend(range(a, b + 1) if a <= b else range(a, b - 1, -1))
        elif part:
            out.append(int(part))
    return out


def _as_quoted(raw: bytes) -> bytes:
    return b'"' + raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def parse_fetch_response(data: list) -> list[dict]:
    """Turn imaplib's FETCH output into one dict per message.

    Records with a header literal arrive as tuples (b'<seq> (... {n}', header_bytes),
    possibly followed by trailing bytes like b' FLAGS (\\Seen))'. A literal that
    isn't the header (a Gmail label sent as a literal) is folded back into the
    metadata as a quoted string. UID, FLAGS and Gmail ids are read only outside
    label names and quoted strings, so a label called "UID 99" can't fool it.
    """
    records: list[dict] = []
    current: dict | None = None
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            meta, literal = item[0], item[1] or b""
            if current is None or _RECORD_START.match(meta):
                current = {"meta": b"", "header": b""}
                records.append(current)
                piece = meta.rstrip()
            else:
                piece = b" " + meta.rstrip()
            if _HEADER_LITERAL.search(piece):
                current["meta"] += piece
                current["header"] = literal
            else:
                current["meta"] += re.sub(rb"\{\d+\}$", b"", piece) + _as_quoted(literal)
        elif isinstance(item, bytes):
            if _RECORD_START.match(item):
                current = {"meta": item, "header": b""}
                records.append(current)
            elif current is not None:
                current["meta"] += b" " + item
    out = []
    for rec in records:
        meta = rec["meta"]
        labels = _LABELS.search(meta)
        without_labels = _LABELS.sub(b"", meta)
        scan = _QUOTED.sub(b'""', without_labels)
        uid = re.search(rb"[ (]UID (\d+)", scan)
        if not uid:
            continue
        flags = re.search(rb"[ (]FLAGS \(([^)]*)\)", scan)
        thrid = re.search(rb"[ (]X-GM-THRID (\d+)", scan)
        msgid = re.search(rb"[ (]X-GM-MSGID (\d+)", scan)
        internal = imaplib.Internaldate2tuple(without_labels)
        out.append({
            "uid": int(uid.group(1)),
            "flags": [f.lower() for f in flags.group(1).decode("ascii", "replace").split()] if flags else [],
            "thrid": thrid.group(1).decode() if thrid else "",
            "msgid": msgid.group(1).decode() if msgid else "",
            "labels": parse_label_list(labels.group(1)) if labels else [],
            "internaldate": int(time.mktime(internal)) if internal else 0,
            "header": rec["header"] or b"",
        })
    return out


def parse_headers(raw: bytes) -> EmailMessage:
    """Header block -> message object whose values are always str.

    Raw 8-bit headers (common in older or non-English mail) are read as UTF-8,
    or Latin-1 if that fails, instead of producing undecodable Header objects.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return HeaderParser().parsestr(text, headersonly=True)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def thread_root(headers, fallback: str) -> str:
    refs = _text(headers.get("References")).split()
    root = refs[0] if refs else _text(headers.get("In-Reply-To")).strip() or _text(headers.get("Message-ID")).strip()
    if not root:
        return fallback
    return "t-" + hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()[:16]


def message_labels(rec: dict, folder: str, folder_flags: set[str], gmail: bool, trash_folder: str,
                   label: str | None = None) -> list[str]:
    labels: list[str] = []
    if gmail:
        labels += [GMAIL_LABEL_MAP.get(lab.lower(), decode_mutf7(lab)) for lab in rec["labels"]]
    elif folder.upper() == "INBOX":
        labels.append(INBOX)
    elif "\\sent" in folder_flags:
        labels.append(SENT)
    elif folder != trash_folder:
        labels.append(label if label is not None else folder_label(folder))  # folders act as labels
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
                      folder_flags: set[str], trash_folder: str = "", label: str | None = None) -> Message:
    headers = parse_headers(rec["header"])
    name, email = parse_from(_text(headers.get("From")))
    msg_id = message_id_for(rec, folder, uidvalidity, gmail)
    fallback = f"{folder}|{uidvalidity}|{rec['uid']}"
    thread = f"gt-{rec['thrid']}" if gmail and rec["thrid"] else thread_root(headers, fallback)
    return Message(
        id=msg_id,
        thread_id=thread,
        date=rec["internaldate"] or parse_date(_text(headers.get("Date"))),
        from_email=email,
        from_name=name,
        to=parse_address_list(_text(headers.get("To"))),
        cc=parse_address_list(_text(headers.get("Cc"))),
        subject=decode(_text(headers.get("Subject"))),
        labels=message_labels(rec, folder, folder_flags, gmail, trash_folder, label),
        list_unsubscribe=_text(headers.get("List-Unsubscribe")),
        list_unsubscribe_post=_text(headers.get("List-Unsubscribe-Post")),
        list_id=_text(headers.get("List-Id")),
        precedence=_text(headers.get("Precedence")),
        auto_submitted=_text(headers.get("Auto-Submitted")),
        message_id_header=_text(headers.get("Message-ID")).strip(),
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


class _Session:
    """The imaplib connection, reopened (and the folder re-selected) if the network drops.

    Every command we send is safe to repeat: searches and fetches read, STORE adds a
    flag, and MOVE/EXPUNGE of a UID that already moved does nothing.
    """

    def __init__(self, open_fn: Callable[[], imaplib.IMAP4]):
        object.__setattr__(self, "_open", open_fn)
        object.__setattr__(self, "_imap", open_fn())
        object.__setattr__(self, "_selected", None)
        object.__setattr__(self, "reconnects", 0)

    def __setattr__(self, name, value):
        setattr(self._imap, name, value)  # e.g. imaplib's `literal`

    def __getattr__(self, name):
        target = getattr(self._imap, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            literal = getattr(self._imap, "literal", None)
            try:
                result = getattr(self._imap, name)(*args, **kwargs)
            except (imaplib.IMAP4.abort, OSError):
                if name == "logout" or self.reconnects >= MAX_RECONNECTS:
                    raise
                self._reopen()
                if literal is not None:
                    self._imap.literal = literal
                result = getattr(self._imap, name)(*args, **kwargs)
            if name == "select" and result and result[0] == "OK":
                object.__setattr__(self, "_selected", (args[0] if args else kwargs.get("mailbox"),
                                                       kwargs.get("readonly", False)))
            return result

        return call

    def _reopen(self) -> None:
        object.__setattr__(self, "reconnects", self.reconnects + 1)
        try:
            self._imap.shutdown()
        except Exception:
            pass
        object.__setattr__(self, "_imap", self._open())
        if self._selected:
            self._imap.select(self._selected[0], readonly=self._selected[1])


class ImapBackend(Backend):
    name = "imap"

    def __init__(self, host: str, port: int, user: str, password: str, tls: str = "ssl",
                 verify: bool = True, ca_file: str = "",
                 connect_fn: Callable[[], imaplib.IMAP4] | None = None):
        self.user = user

        def open_session() -> imaplib.IMAP4:
            conn = connect_fn() if connect_fn else connect(host, port, tls, verify, ca_file)
            try:
                conn.login(user, password)
            except imaplib.IMAP4.error as exc:
                raise BackendError(f"IMAP login failed for {user} at {host}: {exc}") from exc
            return conn

        self.imap = _Session(open_session)
        typ, caps = self.imap.capability()
        self.caps = set(caps[0].decode().upper().split()) if typ == "OK" and caps and caps[0] else set()
        self.gmail = "X-GM-EXT-1" in self.caps
        self.supports_labels = self.gmail
        self._delims: dict[str, str] = {}
        self.folders = self._list_folders()
        self.trash_folder = self._special("\\trash", ("Trash", "[Gmail]/Trash", "Deleted Items", "Deleted Messages"))
        if not self.trash_folder:
            raise BackendError("Could not find a Trash folder on this server.")
        # Servers without special-use flags: recognise Sent by name, so replies count as yours.
        sent = self._special("\\sent", ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "[Gmail]/Sent Mail"))
        for flags, name in self.folders:
            if name == sent:
                flags.add("\\sent")
        self.all_mail = self._special("\\all", ()) if self.gmail else ""
        self._uidvalidity: dict[str, str] = {}
        self._gm_cache: dict[str, tuple[str, dict[str, int]]] = {}

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
                line = re.sub(rb"\{\d+\}$", b"", line[0].rstrip()) + _as_quoted(line[1])
            entry = parse_list_entry(line)
            if entry:
                flags, name, delim = entry
                self._delims[name] = delim
                out.append((flags, name))
        return out

    def label_of(self, folder: str) -> str:
        return folder_label(folder, self._delims.get(folder, "/"))

    def _special(self, flag: str, names: tuple[str, ...]) -> str:
        """The folder with this special-use flag, else one with a well-known name.

        Names match the whole label or, for one-word names, its last part, so
        Courier-style "INBOX.Trash" is found as "Trash".
        """
        for flags, name in self.folders:
            if flag in flags:
                return name
        by_label = {self.label_of(name).casefold(): name for _, name in self.folders}
        by_leaf: dict[str, str] = {}
        for _, name in self.folders:
            by_leaf.setdefault(self.label_of(name).rsplit("/", 1)[-1].casefold(), name)
        for candidate in names:
            key = candidate.casefold()
            if key in by_label:
                return by_label[key]
            if "/" not in key and key in by_leaf:
                return by_leaf[key]
        return ""

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
            elif (not (f & skip_flags) and self.label_of(n).rsplit("/", 1)[-1].casefold() not in skip_names
                  and n != self.trash_folder):
                out.append((f, n))
        return out

    def _select(self, folder: str, readonly: bool) -> str:
        typ, _ = self.imap.select(quote(folder), readonly=readonly)
        if typ != "OK":
            raise BackendError(f"Cannot open folder {self.label_of(folder)}")
        uv = self.imap.response("UIDVALIDITY")[1]
        value = uv[0].decode() if uv and uv[0] else ""
        if value:
            self._uidvalidity[folder] = value
        return value

    def _uid_search(self, *atoms: str, value: str | None = None) -> list[int]:
        """UID SEARCH <atoms> [value]. A non-ASCII value goes as a UTF-8 literal."""
        if value is None:
            typ, data = self.imap.uid("SEARCH", *atoms)
        else:
            try:
                value.encode("ascii")
                typ, data = self.imap.uid("SEARCH", *atoms, quote(value))
            except UnicodeEncodeError:
                self.imap.literal = value.encode("utf-8")
                typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", *atoms)
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(u) for u in data[0].split()]

    def _search(self, query: str | None = None) -> list[int]:
        if query and self.gmail:
            return self._uid_search("X-GM-RAW", value=query)
        return self._uid_search("ALL")

    def _fetch(self, uids: list[int], items: str, chunk: int) -> Iterator[dict]:
        for group in chunked(sorted(uids), chunk):
            for uidset in uid_sets(group):
                typ, data = self.imap.uid("FETCH", uidset, items)
                if typ != "OK":
                    raise BackendError("FETCH failed")
                yield from parse_fetch_response(data)

    def _header_items(self) -> str:
        return f"(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FETCH_FIELDS})]" + (GMAIL_ITEMS + ")" if self.gmail else ")")

    def _meta_items(self) -> str:
        return "(UID FLAGS" + (GMAIL_ITEMS + ")" if self.gmail else ")")

    # -- identity: what the server holds at a UID right now ------------------------------
    def _gm_index(self, folder: str) -> dict[str, int]:
        """X-GM-MSGID -> UID for every message in `folder` (already selected), cached."""
        uv = self._uidvalidity.get(folder, "")
        cached = self._gm_cache.get(folder)
        if cached is not None and cached[0] == uv:
            return cached[1]
        mapping = {r["msgid"]: r["uid"] for r in self._fetch(self._search(), "(UID X-GM-MSGID)", META_CHUNK)
                   if r["msgid"]}
        self._gm_cache[folder] = (uv, mapping)
        return mapping

    def _identities(self, uids: list[int]) -> dict[int, str]:
        """UID -> X-GM-MSGID (Gmail) or Message-ID header, for the selected folder."""
        if not uids:
            return {}
        if self.gmail:
            return {r["uid"]: r["msgid"] for r in self._fetch(uids, "(UID X-GM-MSGID)", META_CHUNK)}
        items = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
        return {r["uid"]: _text(parse_headers(r["header"]).get("Message-ID")).strip()
                for r in self._fetch(uids, items, HEADER_CHUNK)}

    def _identity_of(self, ref: dict) -> str:
        if self.gmail:
            return ref["id"][3:] if ref.get("id", "").startswith("gm-") else ""
        return ref.get("message_id_header") or ""

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
            label = self.label_of(folder)
            uids = self._search(query)
            if not uids:
                continue
            new_uids: list[int] = []
            for rec in self._fetch(uids, self._meta_items(), META_CHUNK):
                mid = message_id_for(rec, folder, uidvalidity, self.gmail)
                labels = message_labels(rec, folder, flags, self.gmail, self.trash_folder, label)
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
            say(f"{label}: {len(uids)} messages, {len(new_uids)} new")
            started, read = time.monotonic(), 0
            for group in chunked(new_uids, HEADER_CHUNK):
                for rec in self._fetch(group, self._header_items(), HEADER_CHUNK):
                    msg = record_to_message(rec, folder, uidvalidity, self.gmail, flags, self.trash_folder, label)
                    if DRAFT in msg.labels:
                        continue
                    yield ("new", msg)
                    new_total += 1
                read += len(group)
                say(f"{label}: read {read}/{len(new_uids)} ({read / max(time.monotonic() - started, 1e-9):.0f}/s)")
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
        index = self._gm_index(self.all_mail)  # current UIDs, never the ones stored at index time
        uids = [index[m.id[3:]] for m in messages if m.id.startswith("gm-") and m.id[3:] in index]
        for uidset in uid_sets(uids):
            typ, _ = self.imap.uid("STORE", uidset, "+X-GM-LABELS", f"({quote(encode_mutf7(label))})")
            if typ != "OK":
                raise BackendError("Adding the keep label failed")

    def _copyuid(self) -> tuple[str, dict[int, int]]:
        """Read (and clear) COPYUID response codes: destination UIDVALIDITY and src -> dst UIDs."""
        _, data = self.imap.response("COPYUID")
        uv, mapping = "", {}
        for item in data or []:
            if not item:
                continue
            parts = (item.decode("ascii", "replace") if isinstance(item, bytes) else str(item)).split()
            if len(parts) != 3:
                continue
            try:
                src, dst = expand_uid_set(parts[1]), expand_uid_set(parts[2])
            except ValueError:
                continue
            if len(src) == len(dst):
                uv = parts[0]
                mapping.update(zip(src, dst))
        return uv, mapping

    def _move(self, uids: list[int], dest: str) -> tuple[str, dict[int, int]]:
        """Move UIDs of the selected folder to `dest`. Returns COPYUID data when the server sends it."""
        self._copyuid()  # discard anything left over
        uv, mapping = "", {}
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
                raise BackendError(f"Moving messages to {self.label_of(dest)} failed")
            got_uv, got = self._copyuid()
            uv = got_uv or uv
            mapping.update(got)
        return uv, mapping

    def _record_trash_uids(self, moved: list[tuple[str, dict, int]], copy: tuple[str, dict[int, int]]) -> None:
        uv, mapping = copy
        for _, ref, uid in moved:
            if uid in mapping:
                ref["trash_uid"] = mapping[uid]  # written to the undo log
                ref["trash_uidvalidity"] = uv

    def trash(self, entries, progress: Progress = None) -> list[str]:
        refs = [(e["thread_id"], ref) for e in entries for ref in e["messages"]]
        failed: set[str] = set()
        if self.gmail:
            # Resolve each planned message by Gmail's permanent id: UIDs can be renumbered.
            self._select(self.all_mail, readonly=False)
            index = self._gm_index(self.all_mail)
            todo = [(tid, ref, index[self._identity_of(ref)]) for tid, ref in refs if self._identity_of(ref) in index]
            for group in chunked(todo, MOVE_CHUNK):
                try:
                    self._record_trash_uids(group, self._move([uid for _, _, uid in group], self.trash_folder))
                except BackendError:
                    failed.update(tid for tid, _, _ in group)
                    continue
                for _, ref, _ in group:
                    index.pop(self._identity_of(ref), None)
            self._gm_cache.pop(self.trash_folder, None)
            return [e["thread_id"] for e in entries if e["thread_id"] not in failed]

        by_folder: dict[str, list[tuple[str, dict]]] = {}
        for tid, ref in refs:
            by_folder.setdefault(ref["folder"], []).append((tid, ref))
        # Check every folder and every message before moving anything.
        mismatched: set[str] = set()
        checked: dict[str, list[tuple[str, dict, int]]] = {}
        for folder, items in by_folder.items():
            current = self._select(folder, readonly=True)
            for _, ref in items:
                expected = str(ref.get("uidvalidity") or "")
                if expected and current and expected != current:
                    raise BackendError(f"{self.label_of(folder)} changed since indexing (UIDVALIDITY). "
                                       "Run `inbox-sweep index` again.")
            there = self._identities([int(ref["uid"]) for _, ref in items])
            for tid, ref in items:
                uid = int(ref["uid"])
                if uid not in there:
                    continue  # no longer in this folder: moved or deleted since; nothing to do
                if there[uid] != self._identity_of(ref):
                    mismatched.add(tid)  # a different message holds this UID now: never touch it
                    continue
                checked.setdefault(folder, []).append((tid, ref, uid))
        if mismatched and progress:
            progress(f"{len(mismatched)} threads skipped: the server's copy no longer matches the index")
        for folder, items in checked.items():
            self._select(folder, readonly=False)
            group_items = [item for item in items if item[0] not in mismatched]
            for group in chunked(group_items, MOVE_CHUNK):
                try:
                    self._record_trash_uids(group, self._move([uid for _, _, uid in group], self.trash_folder))
                except BackendError:
                    failed.update(tid for tid, _, _ in group)
        return [e["thread_id"] for e in entries if e["thread_id"] not in failed | mismatched]

    def _locate_in_trash(self, refs: list[tuple[str, dict]], missing: list[int], located: dict[int, int]) -> None:
        """Find messages that have no usable logged Trash UID (crash mid-batch, no COPYUID)."""
        used = set(located.values())
        if self.gmail:
            index = self._gm_index(self.trash_folder)
            for i in missing:
                uid = index.get(self._identity_of(refs[i][1]))
                if uid is not None and uid not in used:
                    located[i] = uid
                    used.add(uid)
            return
        wanted: dict[str, list[int]] = {}
        for i in missing:
            mid = self._identity_of(refs[i][1])
            if mid:  # a message without a Message-ID can only be found by its logged UID
                wanted.setdefault(mid, []).append(i)
        for mid, idxs in wanted.items():
            try:
                found = self._uid_search("HEADER", "Message-ID", value=mid)
            except (imaplib.IMAP4.error, UnicodeError):
                continue
            exact = [u for u, have in sorted(self._identities(found).items()) if have == mid and u not in used]
            for i, uid in zip(idxs, exact):  # duplicates: one Trash copy per logged message
                located[i] = uid
                used.add(uid)

    def restore(self, entries, progress: Progress = None) -> list[str]:
        uv = self._select(self.trash_folder, readonly=False)
        refs = [(e["thread_id"], ref) for e in entries for ref in e["messages"]]
        located: dict[int, int] = {}
        recorded = {i: int(ref["trash_uid"]) for i, (_, ref) in enumerate(refs)
                    if ref.get("trash_uid") and str(ref.get("trash_uidvalidity", "")) == uv}
        if recorded:
            there = self._identities(sorted(set(recorded.values())))
            for i, uid in recorded.items():
                if uid in there and there[uid] == self._identity_of(refs[i][1]):
                    located[i] = uid
        missing = [i for i in range(len(refs)) if i not in located]
        if missing:
            self._locate_in_trash(refs, missing, located)
        by_dest: dict[str, list[int]] = {}
        for i in located:
            # Gmail restores to the Inbox (like the web UI's "Move to Inbox");
            # other servers get the message back in its original folder.
            dest = "INBOX" if self.gmail else (refs[i][1].get("folder") or "INBOX")
            by_dest.setdefault(dest, []).append(i)
        moved: set[int] = set()
        for dest, idxs in by_dest.items():
            uids = [located[i] for i in idxs]
            try:
                self._move(uids, dest)
            except BackendError:
                if dest == "INBOX":
                    continue
                try:
                    self._move(uids, "INBOX")  # the original folder is gone
                except BackendError:
                    continue
            moved.update(idxs)
        trash_index = self._gm_cache.get(self.trash_folder)
        if trash_index:
            for i in moved:
                trash_index[1].pop(self._identity_of(refs[i][1]), None)
        self._gm_cache.pop(self.all_mail, None)
        failed = {refs[i][0] for i in range(len(refs)) if i not in moved}
        if failed and progress:
            progress(f"{len(failed)} threads could not be found in Trash")
        return [e["thread_id"] for e in entries if e["thread_id"] not in failed]

    def _roots(self, uids: list[int], folder: str = "", uidvalidity: str = "") -> set[str]:
        """Thread ids for these uids in the selected folder, computed exactly as index does."""
        if not uids:
            return set()
        if self.gmail:
            return {f"gt-{r['thrid']}" for r in self._fetch(uids, "(UID X-GM-THRID)", META_CHUNK) if r["thrid"]}
        items = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES)])"
        return {thread_root(parse_headers(r["header"]), f"{folder}|{uidvalidity}|{r['uid']}")
                for r in self._fetch(uids, items, HEADER_CHUNK)}

    def _protected_folder(self, folder: str, protect: LiveProtection) -> bool:
        label = self.label_of(folder)
        return any(label_matches(label, wanted) for wanted in protect.labels)

    def protected_thread_ids(self, protect: LiveProtection) -> set[str] | None:
        threads: set[str] = set()
        if self.gmail:
            from inbox_sweep.backends.gmail_api import protection_queries
            self._select(self.all_mail, readonly=True)
            plain = LiveProtection(me=protect.me, people=protect.people, labels=(),
                                   starred=protect.starred, sent=protect.sent)
            for query in protection_queries(plain):
                threads |= self._roots(self._search(query))
            # Labels are folders on Gmail: read each protected one directly (exact, any language).
            for flags, folder in self.folders:
                if folder in (self.all_mail, self.trash_folder) or "\\noselect" in flags:
                    continue
                if self._protected_folder(folder, protect):
                    self._select(folder, readonly=True)
                    threads |= self._roots(self._search())
            return threads
        for flags, folder in self._scan_folders():
            uv = self._select(folder, readonly=True)
            if protect.sent and "\\sent" in flags:
                threads |= self._roots(self._search(), folder, uv)  # every thread you sent in
                continue
            if self._protected_folder(folder, protect):
                threads |= self._roots(self._search(), folder, uv)  # a protected folder
                continue
            uids: set[int] = set()
            if protect.starred:
                uids.update(self._uid_search("FLAGGED"))
            for person in protect.people:
                uids.update(self._uid_search("FROM", value=person))
                uids.update(self._uid_search("TO", value=person))
            for me in protect.me:  # mail *to* you is everything; only mail *from* you counts
                uids.update(self._uid_search("FROM", value=me))
            threads |= self._roots(sorted(uids), folder, uv)
        return threads

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass

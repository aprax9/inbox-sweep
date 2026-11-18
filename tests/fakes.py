"""In-memory stand-ins for the Gmail API and an IMAP server, faithful enough to
exercise the real backend code paths (paging, batching, throttling, MOVE, ...)."""

from __future__ import annotations

import re
from collections import Counter

try:  # only the Gmail fakes need these; the IMAP fakes work without the [gmail] extra
    import httplib2
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover
    httplib2 = None

    class HttpError(Exception):
        pass


def http_error(status: int, content: bytes = b"") -> HttpError:
    return HttpError(httplib2.Response({"status": str(status)}), content or b'{"error": "x"}')


# ---------------------------------------------------------------------------- Gmail
class Req:
    def __init__(self, fn, cost_key: str, svc: "FakeGmail"):
        self.fn, self.cost_key, self.svc = fn, cost_key, svc

    def execute(self):
        self.svc.calls[self.cost_key] += 1
        return self.fn()


class FakeGmail:
    def __init__(self, messages: list[dict], labels: list[dict] | None = None, email: str = "me@gmail.com",
                 reject_trash_label: bool = False, throttle_once: set[str] | None = None,
                 strict_ids: bool = False):
        self.msgs = {m["id"]: m for m in messages}
        self.labels_ = {l["id"]: dict(l) for l in (labels or [])}
        for sys_label in ("INBOX", "SENT", "STARRED", "TRASH", "SPAM", "UNREAD", "DRAFT", "CHAT",
                          "CATEGORY_PROMOTIONS", "IMPORTANT"):
            self.labels_.setdefault(sys_label, {"id": sys_label, "name": sys_label, "type": "system"})
        self.email = email
        self.reject_trash_label = reject_trash_label
        self.throttle_once = set(throttle_once or ())
        self.strict_ids = strict_ids  # batchModify rejects a call naming a missing id
        self.calls: Counter = Counter()

    # resource chain used by the backend
    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    def threads(self):
        return _Threads(self)

    def labels(self):
        return _Labels(self)

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": self.email}, "getProfile", self)

    def new_batch_http_request(self, callback):
        return _Batch(self, callback)

    # helpers
    def matches(self, m: dict, query: str) -> bool:
        def one(term: str) -> bool:
            term = term.strip()
            labels = m["labelIds"]
            headers = {h["name"].lower(): h["value"].lower() for h in m["payload"]["headers"]}
            if term == "is:starred":
                return "STARRED" in labels
            if term == "in:sent":
                return "SENT" in labels
            if term.startswith("label:"):
                want = term[6:].strip('"')
                return any(self.labels_.get(l, {}).get("name", l).lower().replace(" ", "-") == want for l in labels)
            if term.startswith("from:"):
                return term[5:] in headers.get("from", "")
            if term.startswith("to:"):
                return term[3:] in headers.get("to", "")
            return term.lower() in headers.get("subject", "")
        return any(one(t) for t in query.split(" OR "))


class _Messages:
    def __init__(self, svc: FakeGmail):
        self.svc = svc

    def list(self, userId, maxResults=100, includeSpamTrash=False, q=None, labelIds=None, pageToken=None):
        def run():
            items = sorted(self.svc.msgs.values(), key=lambda m: -int(m["internalDate"]))
            if not includeSpamTrash:
                items = [m for m in items if not ({"TRASH", "SPAM"} & set(m["labelIds"]))]
            if labelIds:
                items = [m for m in items if set(labelIds) <= set(m["labelIds"])]
            if q:
                items = [m for m in items if self.svc.matches(m, q)]
            start = int(pageToken or 0)
            page = items[start:start + maxResults]
            out = {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page]}
            if start + maxResults < len(items):
                out["nextPageToken"] = str(start + maxResults)
            return out
        return Req(run, "messages.list", self.svc)

    def get(self, userId, id, format="full", metadataHeaders=None):
        def run():
            if id in self.svc.throttle_once:
                self.svc.throttle_once.discard(id)
                raise http_error(429)
            m = self.svc.msgs.get(id)
            if m is None:
                raise http_error(404)
            if format == "minimal":
                return {"id": m["id"], "threadId": m["threadId"], "labelIds": list(m["labelIds"])}
            return {**m, "labelIds": list(m["labelIds"])}
        return Req(run, "messages.get", self.svc)

    def batchModify(self, userId, body):
        def run():
            ids = body["ids"]
            if len(ids) > 1000:
                raise http_error(400, b"too many ids")
            add, remove = body.get("addLabelIds", []), body.get("removeLabelIds", [])
            if self.svc.reject_trash_label and "TRASH" in add + remove:
                raise http_error(400, b"Invalid label: TRASH")
            if self.svc.strict_ids and any(i not in self.svc.msgs for i in ids):
                raise http_error(404, b"Requested entity was not found.")
            for i in ids:
                m = self.svc.msgs.get(i)
                if m:
                    m["labelIds"] = sorted((set(m["labelIds"]) | set(add)) - set(remove))
            return {}
        return Req(run, "messages.batchModify", self.svc)

    def _set(self, mid: str, trashed: bool):
        m = self.svc.msgs.get(mid)
        if m is None:
            raise http_error(404)
        labels = set(m["labelIds"])
        m["labelIds"] = sorted(labels | {"TRASH"} if trashed else labels - {"TRASH"})
        return {"id": mid}

    def trash(self, userId, id):
        return Req(lambda: self._set(id, True), "messages.trash", self.svc)

    def untrash(self, userId, id):
        return Req(lambda: self._set(id, False), "messages.untrash", self.svc)


class _Threads:
    def __init__(self, svc: FakeGmail):
        self.svc = svc

    def _set(self, tid: str, trashed: bool):
        hit = False
        for m in self.svc.msgs.values():
            if m["threadId"] == tid:
                hit = True
                labels = set(m["labelIds"])
                m["labelIds"] = sorted(labels | {"TRASH"} if trashed else labels - {"TRASH"})
        if not hit:
            raise http_error(404)
        return {"id": tid}

    def trash(self, userId, id):
        return Req(lambda: self._set(id, True), "threads.trash", self.svc)

    def untrash(self, userId, id):
        return Req(lambda: self._set(id, False), "threads.untrash", self.svc)


class _Labels:
    def __init__(self, svc: FakeGmail):
        self.svc = svc

    def list(self, userId):
        return Req(lambda: {"labels": list(self.svc.labels_.values())}, "labels.list", self.svc)

    def create(self, userId, body):
        def run():
            lid = f"Label_{len(self.svc.labels_) + 1}"
            self.svc.labels_[lid] = {"id": lid, "name": body["name"], "type": "user"}
            return self.svc.labels_[lid]
        return Req(run, "labels.create", self.svc)


class _Batch:
    def __init__(self, svc: FakeGmail, callback):
        self.svc, self.callback, self.items = svc, callback, []

    def add(self, request, request_id):
        self.items.append((request_id, request))

    def execute(self):
        self.svc.calls["batch"] += 1
        for rid, req in self.items:
            try:
                self.callback(rid, req.execute(), None)
            except HttpError as exc:
                self.callback(rid, None, exc)


def gmail_message(mid: str, thread: str, sender: str, subject: str, ts: int, labels=("INBOX",), to="me@gmail.com",
                  list_id: str = "") -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if list_id:
        headers.append({"name": "List-Id", "value": list_id})
    return {"id": mid, "threadId": thread, "internalDate": str(ts * 1000), "labelIds": list(labels),
            "snippet": "body text that must not be stored", "payload": {"headers": headers}}


# ---------------------------------------------------------------------------- IMAP
class FakeIMAP:
    """A tiny IMAP server answering in imaplib's tuple/bytes shapes."""

    def __init__(self, folders: dict[str, dict], gmail: bool = False, caps=("IMAP4REV1", "MOVE", "UIDPLUS")):
        self.folders = folders  # name -> {"flags": [...], "uidvalidity": int, "next": int, "msgs": {uid: msg}}
        for f in folders.values():
            f.setdefault("msgs", {})
            f.setdefault("next", max(f["msgs"], default=0) + 1)
            f.setdefault("uidvalidity", 1)
            f.setdefault("flags", [])
        self.gmail = gmail
        self.caps = list(caps) + (["X-GM-EXT-1"] if gmail else [])
        self.selected: str | None = None
        self.commands: Counter = Counter()

    def capability(self):
        return "OK", [" ".join(self.caps).encode()]

    def login(self, user, password):
        if password != "secret":
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"ok"]

    def list(self):
        lines = []
        for name, f in self.folders.items():
            flags = " ".join(["\\HasNoChildren"] + f["flags"])
            lines.append(('(' + flags + ') "/" "' + name + '"').encode())
        return "OK", lines

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"').replace('\\"', '"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name]["msgs"])).encode()]

    def response(self, code):
        if code == "UIDVALIDITY" and self.selected:
            return code, [str(self.folders[self.selected]["uidvalidity"]).encode()]
        return code, [None]

    def logout(self):
        return "BYE", [b"bye"]

    # -- helpers
    def _uids(self, uidset: str) -> list[int]:
        msgs = self.folders[self.selected]["msgs"]
        out = []
        for part in uidset.split(","):
            if ":" in part:
                a, b = part.split(":")
                lo, hi = int(a), (max(msgs, default=0) if b == "*" else int(b))
                out += [u for u in msgs if lo <= u <= hi]
            elif int(part) in msgs:
                out.append(int(part))
        return sorted(set(out))

    def _match(self, m: dict, criteria: list) -> bool:
        hdr = m["raw"].decode(errors="replace").lower()
        crit = [c.strip('"') if isinstance(c, str) else c for c in criteria if c is not None]
        if not crit or crit == ["ALL"]:
            return True
        if crit[0] == "FLAGGED":
            return "\\Flagged" in m["flags"]
        if crit[0] in ("FROM", "TO"):
            needle = crit[1].lower()
            line = next((l for l in hdr.splitlines() if l.startswith(crit[0].lower() + ":")), "")
            return needle in line
        if crit[0] == "X-GM-RAW":
            def one(term):
                term = term.strip()
                if term == "is:starred":
                    return "\\Flagged" in m["flags"] or "\\Starred" in m.get("labels", [])
                if term == "in:sent":
                    return "\\Sent" in m.get("labels", [])
                if term.startswith("from:"):
                    line = next((l for l in hdr.splitlines() if l.startswith("from:")), "")
                    return term[5:] in line
                if term.startswith("to:"):
                    line = next((l for l in hdr.splitlines() if l.startswith("to:")), "")
                    return term[3:] in line
                if term.startswith("label:"):
                    return any(l.lower().replace(" ", "-") == term[6:] for l in m.get("labels", []))
                return False
            return any(one(t) for t in crit[1].split(" OR "))
        if crit[0] == "HEADER" and crit[1].lower() == "message-id":
            return crit[2].lower() in hdr
        if crit[0] == "X-GM-MSGID":
            return str(m.get("msgid")) == crit[1]
        raise AssertionError(f"unsupported search {criteria}")

    def uid(self, command, *args):
        self.commands[command.upper()] += 1
        cmd = command.upper()
        folder = self.folders[self.selected]
        msgs = folder["msgs"]
        if cmd == "SEARCH":
            hits = [u for u, m in sorted(msgs.items()) if self._match(m, list(args))]
            return "OK", [" ".join(map(str, hits)).encode()]
        if cmd == "FETCH":
            uidset, items = args
            out = []
            for seq, u in enumerate(self._uids(uidset), 1):
                m = msgs[u]
                meta = f"{seq} (UID {u} FLAGS ({' '.join(m['flags'])})"
                if self.gmail:
                    labels = " ".join(f'"{l}"' if " " in l else l for l in m.get("labels", []))
                    meta += f" X-GM-THRID {m.get('thrid', u)} X-GM-MSGID {m.get('msgid', u)} X-GM-LABELS ({labels})"
                if "INTERNALDATE" in items:
                    meta += ' INTERNALDATE "10-Mar-2015 09:00:00 +0000"'
                if "BODY.PEEK" in items:
                    out.append((f"{meta} BODY[HEADER.FIELDS (...)] {{{len(m['raw'])}}}".encode(), m["raw"]))
                    out.append(b")")
                else:
                    out.append((meta + ")").encode())
            return "OK", out
        if cmd in ("MOVE", "COPY"):
            uidset, dest = args
            dest = dest.strip('"')
            target = self.folders[dest]
            for u in self._uids(uidset):
                m = dict(msgs[u])
                if self.gmail and dest.endswith("Trash"):
                    m["labels"] = [l for l in m.get("labels", []) if l != "\\Inbox"]
                if self.gmail and dest == "INBOX":
                    m["labels"] = sorted(set(m.get("labels", [])) | {"\\Inbox"})
                target["msgs"][target["next"]] = m
                target["next"] += 1
                if cmd == "MOVE":
                    del msgs[u]
            return "OK", [b"done"]
        if cmd == "STORE":
            uidset, op, value = args
            names = re.findall(r'"([^"]*)"|([^\s()]+)', value)
            values = [a or b for a, b in names]
            for u in self._uids(uidset):
                key = "labels" if "X-GM-LABELS" in op else "flags"
                current = set(msgs[u].get(key, []))
                msgs[u][key] = sorted(current | set(values)) if op.startswith("+") else sorted(current - set(values))
            return "OK", [b"stored"]
        if cmd == "EXPUNGE":
            for u in self._uids(args[0]):
                if "\\Deleted" in msgs[u]["flags"]:
                    del msgs[u]
            return "OK", [b"expunged"]
        raise AssertionError(f"unsupported command {command}")


def raw_header(sender: str, subject: str, to: str = "me@example.com", msgid: str = "", refs: str = "",
               extra: str = "") -> bytes:
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {subject}", "Date: Tue, 10 Mar 2015 09:00:00 +0000"]
    if msgid:
        lines.append(f"Message-ID: {msgid}")
    if refs:
        lines.append(f"References: {refs}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()

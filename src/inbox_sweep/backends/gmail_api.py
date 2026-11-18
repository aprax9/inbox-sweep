"""Gmail API backend.

Costs follow Google's published quota table (developers.google.com/workspace/gmail/api/reference/quota).
Since May 2026 new projects get 6,000 units per user per minute and reading one
message's headers costs 20 units, so a *first* index runs at about 5 messages per
second. Everything after that is cheap:

- Re-indexing lists ids (5 units per 500) and refreshes labels per label
  (5 units per 500), fetching headers only for new mail.
- Labelling and trashing use messages.batchModify: 1,000 messages per call for
  50 units. The first batch is verified, with a fallback to messages.trash.
  Only the planned message ids are touched, never whole threads, so mail that
  arrives in a planned thread after you planned stays put.

For very large Gmail inboxes the IMAP backend (app password) has no per-message
quota and is much faster to index.

You bring your own OAuth client (docs/gmail-api-setup.md). The only scope
requested is gmail.modify: it can label and trash, but cannot delete permanently.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterator

from inbox_sweep.backends.base import Backend, BackendError, LiveProtection, Pacer, Progress, chunked
from inbox_sweep.headers import WANTED_HEADERS, decode, parse_address_list, parse_date, parse_from
from inbox_sweep.model import CHAT, DRAFT, SPAM, TRASH, Message

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Quota units per method, and the per-user budget, for each quota generation.
QUOTAS = {
    # Projects created on or after 1 May 2026.
    "2026": {"per_minute": 6000, "get": 20, "list": 5, "batch_modify": 50, "message_trash": 20,
             "message_untrash": 5, "profile": 1, "labels_list": 1, "labels_create": 5},
    # Projects created before 1 May 2026 keep the older limits.
    "legacy": {"per_minute": 15000, "get": 5, "list": 5, "batch_modify": 50, "message_trash": 5,
               "message_untrash": 5, "profile": 1, "labels_list": 1, "labels_create": 5},
}
SAFETY = 0.9          # use 90% of the budget
# The pacer lets one request start before its cost has been "earned", so any
# 60-second window sees at most 60 * rate + (largest single request). With 25
# gets per batch (500 units) that is 5,400 + 500 < 6,000 for 2026 projects.
GET_BATCH = 25
TRASH_BATCH = 20      # messages.trash calls per HTTP batch (400 units)
MODIFY_CHUNK = 1000   # batchModify limit
LIST_PAGE = 500
MAX_ATTEMPTS = 6
SKIP_LABELS_FOR_REFRESH = {"UNREAD", "DRAFT", "CHAT", "SPAM", "TRASH"}
NOT_STORED = {"UNREAD"}  # changes constantly, never used to decide anything


def message_from_gmail(data: dict, label_names: dict[str, str]) -> Message:
    """Convert a users.messages.get(format=metadata) response into a Message."""
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    name, email = parse_from(headers.get("from"))
    internal = int(data.get("internalDate", "0") or 0) // 1000
    return Message(
        id=data["id"],
        thread_id=data.get("threadId", data["id"]),
        date=internal or parse_date(headers.get("date")),
        from_email=email,
        from_name=name,
        to=parse_address_list(headers.get("to")),
        cc=parse_address_list(headers.get("cc")),
        subject=decode(headers.get("subject")),
        labels=[label_names.get(lid, lid) for lid in data.get("labelIds", []) if lid not in NOT_STORED],
        list_unsubscribe=headers.get("list-unsubscribe", ""),
        list_unsubscribe_post=headers.get("list-unsubscribe-post", ""),
        list_id=headers.get("list-id", ""),
        precedence=headers.get("precedence", ""),
        auto_submitted=headers.get("auto-submitted", ""),
        snippet="",  # Gmail returns a body snippet; it is deliberately not stored
        message_id_header=headers.get("message-id", ""),
    )


class _FastPathRejected(Exception):
    """batchModify was accepted but the messages did not move."""


def _status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _retryable(exc: Exception) -> bool:
    status = _status(exc)
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        text = str(getattr(exc, "content", b"") or exc)
        return "rateLimitExceeded" in text or "userRateLimitExceeded" in text
    return False


def _gmail_query_term(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ' ()"') else value


def protection_queries(protect: LiveProtection, max_chars: int = 1200) -> list[str]:
    """Gmail searches that together match everything absolutely protected."""
    parts = []
    if protect.starred:
        parts.append("is:starred")
    if protect.sent:
        parts.append("in:sent")
    for label in protect.labels:
        parts.append("label:" + _gmail_query_term(label.lower().replace(" ", "-").replace("/", "-")))
    for person in protect.people:
        parts += [f"from:{person}", f"to:{person}"]
    for me in protect.me:
        parts.append(f"from:{me}")
    queries, current = [], []
    for part in parts:
        if current and len(" OR ".join(current + [part])) > max_chars:
            queries.append(" OR ".join(current))
            current = []
        current.append(part)
    if current:
        queries.append(" OR ".join(current))
    return queries


class GmailBackend(Backend):
    name = "gmail"

    def __init__(self, credentials_path: str = "", token_path: str = "", interactive: bool = True,
                 quota: str = "2026", fast_trash: bool = True, service=None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if quota not in QUOTAS:
            raise BackendError(f"[gmail] quota must be one of {', '.join(QUOTAS)}")
        self.costs = QUOTAS[quota]
        self.pacer = Pacer(self.costs["per_minute"] * SAFETY / 60, clock=clock, sleep=sleep)
        self.sleep = sleep
        self.fast_trash = fast_trash
        self._fast_ok = False  # set once a label-based move has been verified
        self._labels: dict[str, str] | None = None
        self.service = service if service is not None else self._build_service(credentials_path, token_path, interactive)
        self.users = self.service.users()

    @staticmethod
    def _build_service(credentials_path: str, token_path: str, interactive: bool):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials_path = Path(credentials_path).expanduser()
        token_path = Path(token_path).expanduser()
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None  # e.g. a Testing-mode token older than 7 days
        if not creds or not creds.valid:
            if not interactive:
                raise BackendError("Gmail token missing or expired. Run `inbox-sweep auth`.")
            if not credentials_path.exists():
                raise BackendError(
                    f"OAuth client file not found at {credentials_path}. See docs/gmail-api-setup.md.")
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            os.chmod(token_path, 0o600)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # -- request helpers --------------------------------------------------------------
    def _execute(self, request, units: int):
        from googleapiclient.errors import HttpError
        for attempt in range(MAX_ATTEMPTS):
            self.pacer.spend(units)
            try:
                return request.execute()
            except HttpError as exc:
                if not _retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    raise
                self.sleep(min(60, 2 ** attempt))
        raise BackendError("unreachable")

    def _batch(self, make_request, keys: list[str], units_each: int, size: int) -> tuple[dict, list[str]]:
        """Run make_request(key) for every key in batched HTTP calls, retrying throttled ones.

        Returns ({key: response}, [keys that failed]).
        """
        from googleapiclient.errors import HttpError

        results: dict[str, object] = {}
        failed: list[str] = []  # non-retryable, e.g. 404 for a message deleted meanwhile
        pending = list(keys)
        for attempt in range(MAX_ATTEMPTS):
            retry: list[str] = []

            def callback(request_id, response, exception):
                if exception is None:
                    results[request_id] = response
                elif _retryable(exception):
                    retry.append(request_id)
                else:
                    failed.append(request_id)

            for group in chunked(pending, size):
                batch = self.service.new_batch_http_request(callback=callback)
                for key in group:
                    batch.add(make_request(key), request_id=key)
                self.pacer.spend(units_each * len(group))
                try:
                    batch.execute()
                except HttpError as exc:  # the whole batch was rejected
                    if not _retryable(exc):
                        raise
                    retry.extend(k for k in group if k not in results and k not in retry)
            pending = retry
            if not pending:
                break
            self.sleep(min(60, 2 ** attempt))
        return results, failed + pending

    def _label_names(self) -> dict[str, str]:
        if self._labels is None:
            resp = self._execute(self.users.labels().list(userId="me"), self.costs["labels_list"])
            self._labels = {lab["id"]: lab["name"] for lab in resp.get("labels", [])}
        return self._labels

    def _list(self, query: str | None = None, label_id: str | None = None,
              include_trash: bool = False, limit: int | None = None) -> list[dict]:
        """[{"id", "threadId"}] for every message matching, newest first."""
        out: list[dict] = []
        token = None
        while True:
            kwargs = {"userId": "me", "maxResults": LIST_PAGE, "includeSpamTrash": include_trash}
            if query:
                kwargs["q"] = query
            if label_id:
                kwargs["labelIds"] = [label_id]
            if token:
                kwargs["pageToken"] = token
            resp = self._execute(self.users.messages().list(**kwargs), self.costs["list"])
            out.extend(resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token or (limit and len(out) >= limit):
                return out[:limit] if limit else out

    def _never_indexed(self, include_trash: bool) -> set[str]:
        """Ids of drafts and chats (and spam, when Trash is included).

        They are never indexed. Listing them costs 5 units per 500 ids; reading and
        dropping them would cost 20 units each, on every sync.
        """
        names = self._label_names()
        skip: set[str] = set()
        for lid in (DRAFT, CHAT) + ((SPAM,) if include_trash else ()):
            if lid in names:
                skip.update(m["id"] for m in self._list(label_id=lid, include_trash=include_trash))
        return skip

    def _fetch(self, ids: list[str], progress: Progress, include_trash: bool) -> Iterator[Message]:
        labels = self._label_names()
        say = progress or (lambda _m: None)
        done = 0
        started = time.monotonic()
        for group in chunked(ids, 500):
            responses, failed = self._batch(
                lambda mid: self.users.messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=WANTED_HEADERS),
                group, self.costs["get"], GET_BATCH,
            )
            for mid in group:
                data = responses.get(mid)
                if not data:
                    continue
                msg = message_from_gmail(data, labels)
                if DRAFT in msg.labels or CHAT in msg.labels or SPAM in msg.labels:
                    continue
                if TRASH in msg.labels and not include_trash:
                    continue
                yield msg
            done += len(group)
            rate = done / max(time.monotonic() - started, 1e-9)
            say(f"read {done}/{len(ids)} ({rate:.1f}/s)" + (f", {len(failed)} unreadable" if failed else ""))

    # -- Backend API -------------------------------------------------------------------------
    def account(self) -> str:
        profile = self._execute(self.users.getProfile(userId="me"), self.costs["profile"])
        return profile.get("emailAddress", "").lower()

    def iter_messages(self, query=None, limit=None, progress: Progress = None,
                      include_trash: bool = False) -> Iterator[Message]:
        skip = self._never_indexed(include_trash)
        ids = [m["id"] for m in self._list(query, include_trash=include_trash, limit=limit) if m["id"] not in skip]
        if progress:
            progress(f"{len(ids)} messages to read")
        yield from self._fetch(ids, progress, include_trash)

    def sync(self, known, query=None, limit=None, progress: Progress = None, include_trash: bool = False):
        say = progress or (lambda _m: None)
        skip = self._never_indexed(include_trash)
        current_ids = [m["id"] for m in self._list(query, include_trash=include_trash) if m["id"] not in skip]
        new_ids = [i for i in current_ids if i not in known]
        if limit:
            new_ids = new_ids[:limit]
        say(f"{len(current_ids)} messages in the mailbox, {len(new_ids)} new")
        yield from (("new", m) for m in self._fetch(new_ids, progress, include_trash))
        if query is None and limit is None:
            current_set = set(current_ids)
            for mid in known.keys() - current_set:
                yield ("removed", mid)
        still_known = [i for i in current_ids if i in known]
        if not still_known:
            return
        # Refresh labels label by label: listing costs 5 units per 500 ids,
        # far cheaper than re-reading every message.
        membership: dict[str, list[str]] = {i: [] for i in still_known}
        names = self._label_names()
        for lid, lname in names.items():
            if lid in SKIP_LABELS_FOR_REFRESH:
                continue
            for m in self._list(label_id=lid, include_trash=include_trash):
                if m["id"] in membership:
                    membership[m["id"]].append(lname)
        if include_trash:
            for m in self._list(label_id=TRASH, include_trash=True):
                if m["id"] in membership:
                    membership[m["id"]].append(TRASH)
        for mid in still_known:
            labels = membership[mid]
            if sorted(labels) != sorted(known[mid]):
                yield ("labels", mid, labels)

    def _ensure_label(self, name: str) -> str:
        for lid, lname in self._label_names().items():
            if lname.lower() == name.lower():
                return lid
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        created = self._execute(self.users.labels().create(userId="me", body=body), self.costs["labels_create"])
        self._labels[created["id"]] = created["name"]
        return created["id"]

    def _batch_modify(self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None) -> None:
        body: dict = {"ids": ids}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._execute(self.users.messages().batchModify(userId="me", body=body), self.costs["batch_modify"])

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        lid = self._ensure_label(label)
        ids = [m.id for m in messages]
        for n, group in enumerate(chunked(ids, MODIFY_CHUNK), 1):
            self._batch_modify(group, add=[lid])
            if progress:
                progress(f"labelled {min(n * MODIFY_CHUNK, len(ids))}/{len(ids)}")

    def _in_trash(self, message_id: str) -> bool:
        data = self._execute(self.users.messages().get(userId="me", id=message_id, format="minimal"),
                             self.costs["get"])
        return TRASH in data.get("labelIds", [])

    @staticmethod
    def _batches(entries: list[dict]) -> list[list[dict]]:
        """Group whole plan entries into batches of at most MODIFY_CHUNK message ids."""
        batches: list[list[dict]] = []
        group: list[dict] = []
        count = 0
        for entry in entries:
            n = len(entry["messages"])
            if group and count + n > MODIFY_CHUNK:
                batches.append(group)
                group, count = [], 0
            group.append(entry)
            count += n
        if group:
            batches.append(group)
        return batches

    def _per_message(self, entries: list[dict], to_trash: bool, progress: Progress) -> list[str]:
        """Slow path: messages.trash / messages.untrash, one call per planned message."""
        method = "trash" if to_trash else "untrash"
        cost = self.costs["message_trash" if to_trash else "message_untrash"]
        call = getattr(self.users.messages(), method)
        ids = [ref["id"] for e in entries for ref in e["messages"]]
        done, failed = self._batch(lambda mid: call(userId="me", id=mid), ids, cost, TRASH_BATCH)
        if progress and failed:
            progress(f"{len(failed)} messages could not be {'trashed' if to_trash else 'restored'}")
        return [e["thread_id"] for e in entries if e["messages"] and all(r["id"] in done for r in e["messages"])]

    def _modify(self, ids: list[str], to_trash: bool) -> None:
        for part in chunked(ids, MODIFY_CHUNK):  # a single thread can exceed 1,000 messages
            self._batch_modify(part, add=[TRASH] if to_trash else None, remove=None if to_trash else [TRASH])

    def _modify_or_split(self, batch: list[dict], to_trash: bool, progress: Progress) -> list[str]:
        """Fast path already proven on this account, so a 4xx means some id in the
        batch is gone (e.g. deleted forever since planning). Halve until it's found."""
        from googleapiclient.errors import HttpError

        try:
            self._modify([ref["id"] for e in batch for ref in e["messages"]], to_trash)
            return [e["thread_id"] for e in batch]
        except HttpError as exc:
            if _status(exc) not in (400, 403, 404):
                raise
        if len(batch) == 1:
            return self._per_message(batch, to_trash, progress)
        half = len(batch) // 2
        return (self._modify_or_split(batch[:half], to_trash, progress)
                + self._modify_or_split(batch[half:], to_trash, progress))

    def _move(self, entries: list[dict], to_trash: bool, progress: Progress) -> list[str]:
        from googleapiclient.errors import HttpError

        entries = [e for e in entries if e.get("messages")]
        done: list[str] = []
        failures = 0
        for batch in self._batches(entries):
            if not self.fast_trash:
                done.extend(self._per_message(batch, to_trash, progress))
                continue
            if self._fast_ok:
                done.extend(self._modify_or_split(batch, to_trash, progress))
                continue
            ids = [ref["id"] for e in batch for ref in e["messages"]]
            try:
                self._modify(ids, to_trash)
                if self._in_trash(ids[0]) != to_trash:  # accepted, but did it move?
                    raise _FastPathRejected()
                self._fast_ok = True
                done.extend(e["thread_id"] for e in batch)
            except (HttpError, _FastPathRejected) as exc:
                if isinstance(exc, HttpError) and _status(exc) not in (400, 403, 404):
                    raise
                failures += 1
                if isinstance(exc, _FastPathRejected) or failures >= 2:
                    self.fast_trash = False  # this account doesn't take label-based trash
                    if progress:
                        progress("Label-based trash not accepted; using messages.trash (slower)")
                done.extend(self._per_message(batch, to_trash, progress))
        return done

    def trash(self, entries, progress: Progress = None) -> list[str]:
        return self._move(entries, True, progress)

    def restore(self, entries, progress: Progress = None) -> list[str]:
        return self._move(entries, False, progress)

    def protected_thread_ids(self, protect: LiveProtection) -> set[str] | None:
        threads: set[str] = set()
        for query in protection_queries(protect):
            threads.update(m["threadId"] for m in self._list(query))
        return threads

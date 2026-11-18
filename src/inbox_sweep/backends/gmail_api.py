"""Gmail API backend.

Fast path: headers come in batched metadata requests, labels are added 1,000
messages per call, and threads go to Trash in batched requests. Everything is
throttled under Gmail's per-user quota (250 units/second) and retried on 429/5xx.

You bring your own OAuth client (docs/gmail-api-setup.md). The only scope
requested is gmail.modify, which can label and trash but cannot delete forever.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterator

from inbox_sweep.backends.base import Backend, BackendError, Progress, chunked
from inbox_sweep.headers import WANTED_HEADERS, parse_address_list, parse_date, parse_from, decode
from inbox_sweep.model import CHAT, DRAFT, Message

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Gmail quota units per call (https://developers.google.com/gmail/api/reference/quota)
UNITS_GET = 5
UNITS_LIST = 5
UNITS_THREAD_TRASH = 10
UNITS_BATCH_MODIFY = 50
UNITS_PER_SECOND = 200  # stay under the 250/s per-user limit

GET_BATCH = 50
TRASH_BATCH = 20
MAX_ATTEMPTS = 6


class RateLimiter:
    """Simple quota-unit budget per second."""

    def __init__(self, units_per_second: int = UNITS_PER_SECOND):
        self.rate = units_per_second
        self.allowance = float(units_per_second)
        self.last = time.monotonic()

    def spend(self, units: int) -> None:
        while True:
            now = time.monotonic()
            self.allowance = min(self.rate, self.allowance + (now - self.last) * self.rate)
            self.last = now
            if self.allowance >= units:
                self.allowance -= units
                return
            time.sleep((units - self.allowance) / self.rate)


def message_from_gmail(data: dict, label_names: dict[str, str],
                       attachment_ids: set[str] | None = None) -> Message:
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
        labels=[label_names.get(lid, lid) for lid in data.get("labelIds", [])],
        list_unsubscribe=headers.get("list-unsubscribe", ""),
        list_unsubscribe_post=headers.get("list-unsubscribe-post", ""),
        list_id=headers.get("list-id", ""),
        precedence=headers.get("precedence", ""),
        auto_submitted=headers.get("auto-submitted", ""),
        snippet=data.get("snippet", "")[:200],
        has_attachment=bool(attachment_ids and data["id"] in attachment_ids),
        message_id_header=headers.get("message-id", ""),
    )


def _retryable(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        text = str(getattr(exc, "content", b"") or exc)
        return "rateLimitExceeded" in text or "userRateLimitExceeded" in text
    return False


class GmailBackend(Backend):
    name = "gmail"

    def __init__(self, credentials_path: str, token_path: str, interactive: bool = True):
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
                    f"OAuth client file not found at {credentials_path}. "
                    "See docs/gmail-api-setup.md to create one."
                )
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())
            os.chmod(token_path, 0o600)
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.users = self.service.users()
        self.limiter = RateLimiter()
        self._labels: dict[str, str] | None = None

    # -- helpers --------------------------------------------------------------
    def _execute(self, request, units: int):
        from googleapiclient.errors import HttpError
        for attempt in range(MAX_ATTEMPTS):
            self.limiter.spend(units)
            try:
                return request.execute()
            except HttpError as exc:
                if not _retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(60, 2 ** attempt))
        raise BackendError("unreachable")

    def _batch(self, make_request, keys: list[str], units_each: int, size: int) -> tuple[dict, list[str]]:
        """Run make_request(key) for every key in batched HTTP calls, retrying failures.

        Returns ({key: response}, [keys that failed permanently]).
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
                self.limiter.spend(units_each * len(group))
                try:
                    batch.execute()
                except HttpError as exc:  # the whole batch was rejected
                    if not _retryable(exc):
                        raise
                    retry.extend(k for k in group if k not in results and k not in retry)
            pending = retry
            if not pending:
                break
            time.sleep(min(60, 2 ** attempt))
        return results, failed + pending

    def _label_names(self) -> dict[str, str]:
        if self._labels is None:
            resp = self._execute(self.users.labels().list(userId="me"), 1)
            self._labels = {lab["id"]: lab["name"] for lab in resp.get("labels", [])}
        return self._labels

    def _list_ids(self, query: str | None, limit: int | None = None) -> list[str]:
        ids: list[str] = []
        token = None
        while True:
            kwargs = {"userId": "me", "maxResults": 500, "includeSpamTrash": False}
            if query:
                kwargs["q"] = query
            if token:
                kwargs["pageToken"] = token
            resp = self._execute(self.users.messages().list(**kwargs), UNITS_LIST)
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token or (limit and len(ids) >= limit):
                return ids[:limit] if limit else ids

    # -- Backend API ------------------------------------------------------------
    def account(self) -> str:
        profile = self._execute(self.users.getProfile(userId="me"), 1)
        return profile.get("emailAddress", "")

    def iter_messages(self, query=None, limit=None, progress: Progress = None) -> Iterator[Message]:
        labels = self._label_names()
        say = progress or (lambda _msg: None)
        ids = self._list_ids(query, limit)
        say(f"{len(ids)} messages to read")
        attach_query = f"has:attachment {query}" if query else "has:attachment"
        attachment_ids = set(self._list_ids(attach_query))
        done = 0
        for group in chunked(ids, 500):
            responses, failed = self._batch(
                lambda mid: self.users.messages().get(
                    userId="me", id=mid, format="metadata", metadataHeaders=WANTED_HEADERS),
                group, UNITS_GET, GET_BATCH,
            )
            for mid in group:
                data = responses.get(mid)
                if not data:
                    continue
                msg = message_from_gmail(data, labels, attachment_ids)
                if DRAFT in msg.labels or CHAT in msg.labels:
                    continue
                yield msg
            done += len(group)
            say(f"read {done}/{len(ids)}" + (f" ({len(failed)} unreadable)" if failed else ""))

    def _ensure_label(self, name: str) -> str:
        for lid, lname in self._label_names().items():
            if lname.lower() == name.lower():
                return lid
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        created = self._execute(self.users.labels().create(userId="me", body=body), 5)
        self._labels[created["id"]] = created["name"]
        return created["id"]

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        lid = self._ensure_label(label)
        ids = [m.id for m in messages]
        for n, group in enumerate(chunked(ids, 1000), 1):
            body = {"ids": group, "addLabelIds": [lid]}
            self._execute(self.users.messages().batchModify(userId="me", body=body), UNITS_BATCH_MODIFY)
            if progress:
                progress(f"labelled {min(n * 1000, len(ids))}/{len(ids)}")

    def trash(self, entries, progress: Progress = None) -> list[str]:
        thread_ids = [e["thread_id"] for e in entries]
        done, failed = self._batch(
            lambda tid: self.users.threads().trash(userId="me", id=tid),
            thread_ids, UNITS_THREAD_TRASH, TRASH_BATCH,
        )
        if progress and failed:
            progress(f"{len(failed)} threads could not be trashed")
        return [tid for tid in thread_ids if tid in done]

    def restore(self, entries, progress: Progress = None) -> list[str]:
        thread_ids = [e["thread_id"] for e in entries]
        done, failed = self._batch(
            lambda tid: self.users.threads().untrash(userId="me", id=tid),
            thread_ids, UNITS_THREAD_TRASH, TRASH_BATCH,
        )
        if progress and failed:
            progress(f"{len(failed)} threads could not be restored")
        return [tid for tid in thread_ids if tid in done]

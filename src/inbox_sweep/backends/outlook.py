"""Outlook backend (Outlook.com, Hotmail, Live, Microsoft 365) via Microsoft Graph.

- Sign-in: your own Entra app registration and the OAuth device-code flow (MSAL).
  Delegated scopes are Mail.ReadWrite and User.Read only. See docs/outlook-setup.md.
- Index: /me/messages with a $select of header fields (bodies are never requested),
  skipping Deleted Items, Junk, Drafts, Outbox and conversation history.
- Keep label: an Outlook category, added without disturbing existing categories.
- Trash: a move to the well-known "deleteditems" folder, never a permanent delete.
  Undo moves each message back to the folder it came from.

Requests ask for immutable ids (Prefer: IdType="ImmutableId"), so a message keeps
its id when moved. Throttling (429/503) is retried using the server's Retry-After.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote, urlencode

from inbox_sweep.backends.base import Backend, BackendError, Progress, chunked
from inbox_sweep.headers import decode
from inbox_sweep.model import INBOX, SENT, STARRED, Message

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "User.Read"]
SELECT = ",".join([
    "id", "conversationId", "receivedDateTime", "from", "sender", "toRecipients", "ccRecipients",
    "subject", "categories", "flag", "parentFolderId", "internetMessageId", "isDraft",
    "hasAttachments", "internetMessageHeaders",
])  # no body or bodyPreview: message content is never requested
WELL_KNOWN = ["inbox", "sentitems", "deleteditems", "junkemail", "drafts", "outbox", "conversationhistory", "archive"]
SKIP_WELL_KNOWN = {"deleteditems", "junkemail", "drafts", "outbox", "conversationhistory"}
BATCH_SIZE = 20  # Graph's JSON batching limit
MAX_ATTEMPTS = 6
RETRYABLE = {429, 500, 502, 503, 504}

Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, dict]]


class GraphError(BackendError):
    def __init__(self, status: int, body: dict | None = None):
        message = ((body or {}).get("error") or {}).get("message", "") if isinstance(body, dict) else ""
        super().__init__(f"Microsoft Graph error {status}: {message}".strip())
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, dict]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data


def _retry_after(headers: dict, attempt: int) -> float:
    try:
        return min(120.0, float(headers.get("retry-after", "")))
    except ValueError:
        return min(60.0, 2.0 ** attempt)


class GraphClient:
    """Minimal Graph client: JSON requests, paging, $batch, and throttling retries."""

    def __init__(self, token: Callable[[], str], transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = token
        self.transport = transport or urllib_transport
        self.sleep = sleep

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Prefer": 'IdType="ImmutableId"',
        }

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else GRAPH + path
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, safe="$,", quote_via=quote)
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(MAX_ATTEMPTS):
            status, headers, data = self.transport(method, url, self._headers(), payload)
            if status in RETRYABLE and attempt < MAX_ATTEMPTS - 1:
                self.sleep(_retry_after(headers, attempt))
                continue
            if status >= 400:
                raise GraphError(status, data)
            return data
        raise GraphError(429)

    def pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        data = self.request("GET", path, params)
        while True:
            yield from data.get("value", [])
            next_link = data.get("@odata.nextLink")
            if not next_link:
                return
            data = self.request("GET", next_link)

    def batch(self, requests: list[dict]) -> dict[str, dict]:
        """Run sub-requests ({"id", "method", "url", "body"?}) 20 at a time.

        Returns {id: {"status", "body"}}. Throttled sub-requests are retried.
        """
        results: dict[str, dict] = {}
        pending = list(requests)
        for attempt in range(MAX_ATTEMPTS):
            retry, wait = [], 0.0
            for group in chunked(pending, BATCH_SIZE):
                subs = []
                for r in group:
                    sub = {"id": r["id"], "method": r["method"], "url": r["url"]}
                    if "body" in r:
                        sub["body"] = r["body"]
                        sub["headers"] = {"Content-Type": "application/json"}
                    subs.append(sub)
                data = self.request("POST", "/$batch", body={"requests": subs})
                by_id = {r["id"]: r for r in group}
                for resp in data.get("responses", []):
                    status = int(resp.get("status", 0))
                    if status in RETRYABLE and resp["id"] in by_id:
                        retry.append(by_id[resp["id"]])
                        wait = max(wait, _retry_after({k.lower(): v for k, v in (resp.get("headers") or {}).items()}, attempt))
                    else:
                        results[resp["id"]] = {"status": status, "body": resp.get("body") or {}}
            pending = retry
            if not pending:
                break
            self.sleep(wait)
        for r in pending:
            results[r["id"]] = {"status": 429, "body": {}}
        return results


def _parse_time(value: str) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _addr(entry: dict | None) -> tuple[str, str]:
    ea = (entry or {}).get("emailAddress") or {}
    return (ea.get("name") or "").strip(), (ea.get("address") or "").strip().lower()


def message_from_graph(data: dict, folder_names: dict[str, str], well_known: dict[str, str]) -> Message:
    name, email = _addr(data.get("from") or data.get("sender"))
    headers = {h.get("name", "").lower(): h.get("value", "") for h in (data.get("internetMessageHeaders") or [])}
    parent = data.get("parentFolderId", "")
    labels: list[str] = []
    if parent and parent == well_known.get("inbox"):
        labels.append(INBOX)
    elif parent and parent == well_known.get("sentitems"):
        labels.append(SENT)
    elif parent in folder_names:
        labels.append(folder_names[parent])  # folders act as labels, e.g. for [protect] labels
    labels += list(data.get("categories") or [])
    if ((data.get("flag") or {}).get("flagStatus") or "").lower() == "flagged":
        labels.append(STARRED)
    return Message(
        id=data["id"],
        thread_id="c-" + (data.get("conversationId") or data["id"]),
        date=_parse_time(data.get("receivedDateTime", "")),
        from_email=email,
        from_name=name,
        to=[a for _, a in map(_addr, data.get("toRecipients") or []) if a],
        cc=[a for _, a in map(_addr, data.get("ccRecipients") or []) if a],
        subject=decode(data.get("subject") or ""),
        labels=sorted(set(labels)),
        list_unsubscribe=headers.get("list-unsubscribe", ""),
        list_unsubscribe_post=headers.get("list-unsubscribe-post", ""),
        list_id=headers.get("list-id", ""),
        precedence=headers.get("precedence", ""),
        auto_submitted=headers.get("auto-submitted", ""),
        snippet="",  # body text is never requested
        has_attachment=bool(data.get("hasAttachments")),
        message_id_header=data.get("internetMessageId", "") or "",
        folder=parent,
    )


def _msal_token_provider(client_id: str, authority: str, token_path: Path, interactive: bool) -> Callable[[], str]:
    try:
        import msal
    except ImportError as exc:
        raise BackendError("The Outlook backend needs: pip install 'inbox-sweep[outlook]'") from exc
    cache = msal.SerializableTokenCache()
    if token_path.exists():
        cache.deserialize(token_path.read_text())
    tenant = authority if authority.startswith("https://") else f"https://login.microsoftonline.com/{authority}"
    app = msal.PublicClientApplication(client_id, authority=tenant, token_cache=cache)

    def save() -> None:
        if cache.has_state_changed:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(cache.serialize())
            os.chmod(token_path, 0o600)

    def token() -> str:
        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result:
            if not interactive:
                raise BackendError("Outlook sign-in needed. Run `inbox-sweep auth`.")
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise BackendError(f"Could not start Microsoft sign-in: {flow.get('error_description', flow)}")
            print(flow["message"], file=sys.stderr, flush=True)
            result = app.acquire_token_by_device_flow(flow)
        save()
        if "access_token" not in result:
            raise BackendError(f"Microsoft sign-in failed: {result.get('error_description', result.get('error'))}")
        return result["access_token"]

    return token


class OutlookBackend(Backend):
    name = "outlook"

    def __init__(self, client_id: str = "", authority: str = "common",
                 token_path: str = "~/.config/inbox-sweep/outlook-token.json",
                 interactive: bool = True, client: GraphClient | None = None):
        if client is None:
            if not client_id:
                raise BackendError("Set [outlook] client_id in sweep.toml (see docs/outlook-setup.md).")
            client = GraphClient(_msal_token_provider(client_id, authority, Path(token_path).expanduser(), interactive))
        self.client = client
        self._folders: dict[str, str] | None = None
        self._well_known: dict[str, str] = {}

    # -- folders ----------------------------------------------------------------
    def _load_folders(self) -> None:
        if self._folders is not None:
            return
        folders: dict[str, str] = {}

        def walk(path: str, prefix: str, depth: int) -> None:
            for f in self.client.pages(path, {"$top": "250", "$select": "id,displayName,childFolderCount"}):
                folders[f["id"]] = prefix + f.get("displayName", "")
                if f.get("childFolderCount") and depth < 4:
                    walk(f"/me/mailFolders/{quote(f['id'], safe='')}/childFolders", folders[f["id"]] + "/", depth + 1)

        walk("/me/mailFolders", "", 0)
        for name in WELL_KNOWN:
            try:
                self._well_known[name] = self.client.request("GET", f"/me/mailFolders/{name}", {"$select": "id"})["id"]
            except GraphError:
                pass  # not every mailbox has every well-known folder
        self._folders = folders

    # -- Backend API ------------------------------------------------------------------
    def account(self) -> str:
        me = self.client.request("GET", "/me", {"$select": "mail,userPrincipalName"})
        return (me.get("mail") or me.get("userPrincipalName") or "").lower()

    def iter_messages(self, query=None, limit=None, progress: Progress = None) -> Iterator[Message]:
        self._load_folders()
        say = progress or (lambda _m: None)
        skip = {self._well_known[n] for n in SKIP_WELL_KNOWN if n in self._well_known}
        params = {"$select": SELECT, "$top": "250"}
        if query:
            params["$search"] = '"' + query.replace('"', "") + '"'
        count = 0
        for item in self.client.pages("/me/messages", params):
            if item.get("isDraft") or item.get("parentFolderId") in skip:
                continue
            yield message_from_graph(item, self._folders or {}, self._well_known)
            count += 1
            if count % 1000 == 0:
                say(f"read {count}")
            if limit and count >= limit:
                return

    def label_messages(self, messages, label, progress: Progress = None) -> None:
        ids = [m.id for m in messages]
        done = 0
        for group in chunked(ids, 200):
            current = self.client.batch([
                {"id": str(i), "method": "GET", "url": f"/me/messages/{quote(mid, safe='')}?$select=categories"}
                for i, mid in enumerate(group)
            ])
            patches = []
            for i, mid in enumerate(group):
                got = current.get(str(i), {})
                if got.get("status") != 200:
                    continue
                cats = list(got["body"].get("categories") or [])
                if label not in cats:
                    patches.append({"id": str(i), "method": "PATCH", "url": f"/me/messages/{quote(mid, safe='')}",
                                    "body": {"categories": cats + [label]}})
            if patches:
                self.client.batch(patches)
            done += len(group)
            if progress:
                progress(f"labelled {done}/{len(ids)}")

    def _move(self, refs: list[dict], destination: Callable[[dict], str], id_of: Callable[[dict], str]) -> dict[int, dict]:
        requests = [{"id": str(i), "method": "POST",
                     "url": f"/me/messages/{quote(id_of(ref), safe='')}/move",
                     "body": {"destinationId": destination(ref)}} for i, ref in enumerate(refs)]
        return {int(k): v for k, v in self.client.batch(requests).items()}

    def trash(self, entries, progress: Progress = None) -> list[str]:
        refs = [(e["thread_id"], ref) for e in entries for ref in e["messages"]]
        results = self._move([r for _, r in refs], lambda _r: "deleteditems", lambda r: r["id"])
        failed: set[str] = set()
        for i, (tid, ref) in enumerate(refs):
            res = results.get(i, {"status": 0})
            if res["status"] in (200, 201):
                ref["trash_id"] = res["body"].get("id", ref["id"])  # logged for undo
            elif res["status"] != 404:  # 404: already gone, nothing to move
                failed.add(tid)
        if progress and failed:
            progress(f"{len(failed)} conversations could not be moved to Deleted Items")
        return [e["thread_id"] for e in entries if e["thread_id"] not in failed]

    def restore(self, entries, progress: Progress = None) -> list[str]:
        refs = [(e["thread_id"], ref) for e in entries for ref in e["messages"]]
        results = self._move([r for _, r in refs], lambda r: r.get("folder") or "inbox",
                             lambda r: r.get("trash_id") or r["id"])
        retry = [i for i, (_, ref) in enumerate(refs) if results.get(i, {}).get("status") == 404 and ref.get("folder")]
        if retry:  # original folder no longer exists: fall back to the Inbox
            again = self._move([refs[i][1] for i in retry], lambda _r: "inbox", lambda r: r.get("trash_id") or r["id"])
            for n, i in enumerate(retry):
                results[i] = again.get(n, results[i])
        failed = {tid for i, (tid, _) in enumerate(refs) if results.get(i, {}).get("status") not in (200, 201)}
        return [e["thread_id"] for e in entries if e["thread_id"] not in failed]

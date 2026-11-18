"""Outlook backend against a fake Microsoft Graph (no network, no account)."""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from inbox_sweep.backends.base import BackendError
from inbox_sweep.backends.outlook import GraphClient, OutlookBackend, message_from_graph
from inbox_sweep.model import INBOX, SENT, STARRED

FOLDERS = {
    "inbox": "F-INBOX", "sentitems": "F-SENT", "deleteditems": "F-DEL", "junkemail": "F-JUNK",
    "drafts": "F-DRAFT", "archive": "F-ARCH",
}


def graph_message(mid, folder="F-INBOX", **kw):
    data = {
        "id": mid, "conversationId": "conv-" + mid, "receivedDateTime": "2015-03-10T09:00:00Z",
        "from": {"emailAddress": {"name": "Jane Doe", "address": "Jane@Example.org"}},
        "toRecipients": [{"emailAddress": {"name": "Me", "address": "me@example.com"}}],
        "subject": "Hello", "categories": [], "flag": {"flagStatus": "notFlagged"},
        "parentFolderId": folder, "internetMessageId": f"<{mid}@example.org>", "isDraft": False,
        "hasAttachments": False, "bodyPreview": "secret body text",
        "internetMessageHeaders": [{"name": "List-Unsubscribe", "value": "<https://x.example/u>"}],
    }
    data.update(kw)
    return data


class FakeGraph:
    """Enough of Graph for the backend: folders, paged messages, $batch move/get/patch."""

    def __init__(self, messages, throttle_once=False):
        self.messages = {m["id"]: m for m in messages}
        self.calls = []
        self.throttle_once = throttle_once
        self.categories_patched = {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        assert headers["Prefer"] == 'IdType="ImmutableId"'
        assert headers["Authorization"] == "Bearer TOKEN"
        parts = urlsplit(url)
        path = parts.path.replace("/v1.0", "")
        query = parse_qs(parts.query)
        if path == "/me":
            return 200, {}, {"mail": "Me@Example.com"}
        if path == "/me/mailFolders":
            return 200, {}, {"value": [
                {"id": "F-INBOX", "displayName": "Inbox", "childFolderCount": 0},
                {"id": "F-TAX", "displayName": "Taxes", "childFolderCount": 1},
            ]}
        if path == "/me/mailFolders/F-TAX/childFolders":
            return 200, {}, {"value": [{"id": "F-TAX-2015", "displayName": "2015", "childFolderCount": 0}]}
        if path.startswith("/me/mailFolders/"):
            name = path.rsplit("/", 1)[1]
            if name in FOLDERS:
                return 200, {}, {"id": FOLDERS[name]}
            return 404, {}, {"error": {"message": "not found"}}
        if path == "/me/messages":
            if "$select" in query:  # first page; later pages follow nextLink as-is
                assert "bodyPreview" not in query["$select"][0] and "body" not in query["$select"][0].split(",")
            items = sorted(self.messages.values(), key=lambda m: m["id"])
            if "skip" in query:
                return 200, {}, {"value": items[2:]}
            return 200, {}, {"value": items[:2], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?skip=2"}
        if path == "/$batch":
            if self.throttle_once:
                self.throttle_once = False
                return 429, {"retry-after": "0"}, {}
            return 200, {}, {"responses": [self._sub(r) for r in json.loads(body)["requests"]]}
        raise AssertionError(f"unexpected {method} {url}")

    def _sub(self, r):
        path = unquote(urlsplit(r["url"]).path)
        mid = path.split("/")[3]
        msg = self.messages.get(mid)
        if msg is None:
            return {"id": r["id"], "status": 404, "body": {}}
        if r["method"] == "POST" and path.endswith("/move"):
            dest = r["body"]["destinationId"]
            msg["parentFolderId"] = FOLDERS.get(dest, dest)
            return {"id": r["id"], "status": 201, "body": {"id": mid}}
        if r["method"] == "GET":
            return {"id": r["id"], "status": 200, "body": {"categories": msg["categories"]}}
        if r["method"] == "PATCH":
            msg["categories"] = r["body"]["categories"]
            return {"id": r["id"], "status": 200, "body": {}}
        raise AssertionError(r)


def backend_for(fake):
    return OutlookBackend(client=GraphClient(lambda: "TOKEN", transport=fake, sleep=lambda s: None))


def test_message_mapping():
    folders = {"F-TAX": "Taxes"}
    m = message_from_graph(graph_message("A", folder="F-TAX", categories=["Family"],
                                         flag={"flagStatus": "flagged"}), folders, FOLDERS)
    assert m.from_email == "jane@example.org" and m.from_name == "Jane Doe"
    assert set(m.labels) == {"Taxes", "Family", STARRED}
    assert m.thread_id == "c-conv-A" and m.folder == "F-TAX"
    assert m.list_unsubscribe == "<https://x.example/u>"
    assert m.snippet == ""  # body previews are never stored
    assert m.date == 1425978000
    assert INBOX in message_from_graph(graph_message("B"), {}, FOLDERS).labels
    assert SENT in message_from_graph(graph_message("C", folder="F-SENT"), {}, FOLDERS).labels


def test_index_pages_and_skips_trash_junk_drafts():
    fake = FakeGraph([graph_message("A"), graph_message("B", folder="F-DEL"), graph_message("C", folder="F-JUNK"),
                      graph_message("D", isDraft=True), graph_message("E", folder="F-TAX-2015")])
    backend = backend_for(fake)
    assert backend.account() == "me@example.com"
    msgs = list(backend.iter_messages())
    assert [m.id for m in msgs] == ["A", "E"]
    assert "Taxes/2015" in msgs[1].labels


def test_trash_restore_and_keep_label_roundtrip():
    fake = FakeGraph([graph_message("A", categories=["Family"]), graph_message("B", folder="F-TAX"),
                      graph_message("C")], throttle_once=True)
    backend = backend_for(fake)
    msgs = list(backend.iter_messages())
    entries = [{"thread_id": "c-conv-B", "messages": [{"id": "B", "folder": "F-TAX"}]},
               {"thread_id": "c-conv-Z", "messages": [{"id": "Z", "folder": "F-INBOX"}]}]  # Z already gone
    done = backend.trash(entries)
    assert done == ["c-conv-B", "c-conv-Z"]
    assert fake.messages["B"]["parentFolderId"] == "F-DEL"
    assert entries[0]["messages"][0]["trash_id"] == "B"

    restored = backend.restore(entries[:1])
    assert restored == ["c-conv-B"] and fake.messages["B"]["parentFolderId"] == "F-TAX"

    backend.label_messages([m for m in msgs if m.id in ("A", "C")], "Keep")
    assert fake.messages["A"]["categories"] == ["Family", "Keep"]  # existing categories preserved
    assert fake.messages["C"]["categories"] == ["Keep"]


def test_requires_client_id():
    with pytest.raises(BackendError):
        OutlookBackend(client_id="")

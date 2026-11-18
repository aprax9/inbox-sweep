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
        self.searches = []

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
        if path.startswith("/me/mailFolders/") and path.endswith("/messages"):
            fid = unquote(path.split("/")[3])
            return 200, {}, {"value": [m for m in self._matching(query) if m["parentFolderId"] == fid]}
        if path.startswith("/me/mailFolders/"):
            name = path.rsplit("/", 1)[1]
            if name in FOLDERS:
                return 200, {}, {"id": FOLDERS[name]}
            return 404, {}, {"error": {"message": "not found"}}
        if path == "/me/messages" and ("$filter" in query or "$search" in query):
            return 200, {}, {"value": self._matching(query)}
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

    def _matching(self, query):
        items = sorted(self.messages.values(), key=lambda m: m["id"])
        flt = query.get("$filter", [""])[0]
        if flt == "flag/flagStatus eq 'flagged'":
            items = [m for m in items if m["flag"]["flagStatus"] == "flagged"]
        elif flt.startswith("categories/any(c:c eq '"):
            cat = flt[len("categories/any(c:c eq '"):-2].replace("''", "'")
            items = [m for m in items if cat in m["categories"]]
        elif flt:
            raise AssertionError(flt)
        search = query.get("$search", [""])[0].strip('"').lower()
        if search:
            field, who = search.split(":", 1)
            def addrs(m):
                people = [m["from"]] + (m["toRecipients"] if field == "participants" else [])
                return [p["emailAddress"]["address"].lower() for p in people]
            items = [m for m in items if who in addrs(m)]
        self.searches.append((flt, search))
        return items

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


def test_live_protection_queries():
    me = {"emailAddress": {"address": "me@example.com"}}
    fake = FakeGraph([
        graph_message("flag", flag={"flagStatus": "flagged"}),
        graph_message("sent", folder="F-SENT"),
        graph_message("filed-mine", folder="F-ARCH", **{"from": me}),
        graph_message("taxfolder", folder="F-TAX"),
        graph_message("taxcat", categories=["Taxes"]),
        graph_message("mom", **{"from": {"emailAddress": {"address": "mom@family.example"}}}),
        graph_message("to-mom", toRecipients=[{"emailAddress": {"address": "mom@family.example"}}]),
        graph_message("promo"),
    ])
    from inbox_sweep.backends.base import LiveProtection
    threads = backend_for(fake).protected_thread_ids(
        LiveProtection(me=["me@example.com"], people=["mom@family.example"], labels=["Taxes"]))
    assert threads == {"c-conv-" + x for x in ("flag", "sent", "filed-mine", "taxfolder", "taxcat", "mom", "to-mom")}


def test_include_trash_marks_deleted_items():
    from inbox_sweep.model import TRASH
    fake = FakeGraph([graph_message("A"), graph_message("B", folder="F-DEL"), graph_message("C", folder="F-JUNK")])
    msgs = {m.id: m for m in backend_for(fake).iter_messages(include_trash=True)}
    assert set(msgs) == {"A", "B"} and TRASH in msgs["B"].labels and TRASH not in msgs["A"].labels


def test_batches_are_paced_per_sub_request():
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    fake = FakeGraph([graph_message(f"M{i:03d}") for i in range(100)])
    client = GraphClient(lambda: "TOKEN", transport=fake, sleep=sleep, clock=lambda: clock["t"],
                         requests_per_second=15)
    backend = OutlookBackend(client=client)
    entries = [{"thread_id": f"c-conv-M{i:03d}", "messages": [{"id": f"M{i:03d}", "folder": "F-INBOX"}]}
               for i in range(100)]
    assert len(backend.trash(entries)) == 100
    # 100 moves in batches of 4 (Outlook runs at most 4 requests per mailbox at once), each
    # counted as 4 requests: Graph's 10,000 per 10 minutes is per sub-request.
    batches = [c for c in fake.calls if c[1].endswith("/$batch")]
    assert len(batches) == 25
    assert clock["t"] >= (100 - 4) / 15 - 1e-9


def test_network_failures_are_retried():
    fake = FakeGraph([graph_message("A")])
    drops = {"left": 2}

    def flaky(method, url, headers, body):
        if drops["left"]:
            drops["left"] -= 1
            return 0, {}, {"error": {"message": "network error: timed out"}}
        return fake(method, url, headers, body)

    backend = OutlookBackend(client=GraphClient(lambda: "TOKEN", transport=flaky, sleep=lambda s: None))
    assert backend.account() == "me@example.com"


def test_urllib_transport_turns_network_errors_into_a_retryable_status(monkeypatch):
    import urllib.error
    import urllib.request
    from inbox_sweep.backends.outlook import urllib_transport

    def down(*a, **kw):
        raise urllib.error.URLError("Temporary failure in name resolution")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    status, _, body = urllib_transport("GET", "https://graph.microsoft.com/v1.0/me", {}, None)
    assert status == 0 and "network error" in body["error"]["message"]


def test_nested_and_inbox_folders_count_as_protected_labels():
    from inbox_sweep.backends.base import LiveProtection
    fake = FakeGraph([graph_message("t2015", folder="F-TAX-2015"), graph_message("promo")])
    threads = backend_for(fake).protected_thread_ids(LiveProtection(labels=["Taxes"], starred=False, sent=False))
    assert threads == {"c-conv-t2015"}  # "Taxes/2015" sits inside the protected "Taxes"

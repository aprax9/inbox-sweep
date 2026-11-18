"""IMAP backend end to end against an in-memory IMAP server (generic and Gmail flavours)."""

import pytest

from inbox_sweep.backends.base import BackendError, LiveProtection
from inbox_sweep.backends.imap import ImapBackend, connect, uid_sets
from inbox_sweep.model import INBOX, SENT, STARRED, TRASH

from fakes import FakeIMAP, raw_header


def generic_server():
    return FakeIMAP({
        "INBOX": {"uidvalidity": 7, "msgs": {
            1: {"raw": raw_header("Jane <jane@gmail.com>", "Hello", msgid="<a@x>"), "flags": ["\\Seen"]},
            2: {"raw": raw_header("deals@shop.example", "Sale", msgid="<b@x>",
                                  extra="List-Unsubscribe: <https://shop.example/u>"), "flags": []},
            3: {"raw": raw_header("Jane <jane@gmail.com>", "Re: Hello", msgid="<c@x>", refs="<a@x>"),
                "flags": ["\\Flagged"]},
        }},
        "Sent": {"flags": ["\\Sent"], "uidvalidity": 8, "msgs": {
            1: {"raw": raw_header("me@example.com", "Re: Hello", to="jane@gmail.com", msgid="<d@x>", refs="<a@x>"),
                "flags": ["\\Seen"]},
        }},
        "Taxes": {"uidvalidity": 9, "msgs": {
            1: {"raw": raw_header("cra@revenue.gov.example", "Notice of assessment", msgid="<e@x>"), "flags": []},
        }},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10},
        "Drafts": {"flags": ["\\Drafts"], "uidvalidity": 11, "msgs": {
            1: {"raw": raw_header("me@example.com", "unsent"), "flags": ["\\Draft"]}}},
    })


def gmail_server():
    return FakeIMAP({
        "INBOX": {"uidvalidity": 1},
        "[Gmail]/All Mail": {"flags": ["\\All"], "uidvalidity": 5, "msgs": {
            10: {"raw": raw_header("Mom <mom@family.example>", "Dinner"), "flags": [], "msgid": 1001, "thrid": 501,
                 "labels": ["\\Inbox", "Family"]},
            11: {"raw": raw_header("deals@shop.example", "Sale"), "flags": [], "msgid": 1002, "thrid": 502,
                 "labels": ["\\Inbox"]},
            12: {"raw": raw_header("me@gmail.com", "Re: Dinner", to="mom@family.example"), "flags": ["\\Seen"],
                 "msgid": 1003, "thrid": 501, "labels": ["\\Sent"]},
        }},
        "[Gmail]/Trash": {"flags": ["\\Trash"], "uidvalidity": 6},
    }, gmail=True)


def make(server):
    return ImapBackend("imap.example.com", 993, "me@example.com", "secret", connect_fn=lambda: server)


def test_generic_index_labels_threads_and_skips_drafts():
    imap = make(generic_server())
    msgs = {m.id: m for m in imap.iter_messages()}
    assert len(msgs) == 5  # inbox 3 + sent 1 + Taxes 1; drafts and trash skipped
    inbox_hello = msgs["INBOX|7|1"]
    assert INBOX in inbox_hello.labels and inbox_hello.from_email == "jane@gmail.com"
    assert STARRED in msgs["INBOX|7|3"].labels
    assert SENT in msgs["Sent|8|1"].labels and "Taxes" in msgs["Taxes|9|1"].labels
    # Original, reply and your sent reply share one thread (rooted at <a@x>).
    assert msgs["INBOX|7|1"].thread_id == msgs["INBOX|7|3"].thread_id == msgs["Sent|8|1"].thread_id


def test_generic_incremental_sync_reads_only_new_headers():
    server = generic_server()
    imap = make(server)
    known = {m.id: m.labels for m in imap.iter_messages()}
    server.folders["INBOX"]["msgs"][2]["flags"].append("\\Flagged")
    del server.folders["INBOX"]["msgs"][1]
    server.folders["INBOX"]["msgs"][4] = {"raw": raw_header("bob@gmail.com", "New"), "flags": []}
    server.commands.clear()
    events = list(make(server).sync(known))
    assert [e[1].id for e in events if e[0] == "new"] == ["INBOX|7|4"]
    assert ("removed", "INBOX|7|1") in events
    assert any(e[0] == "labels" and e[1] == "INBOX|7|2" and STARRED in e[2] for e in events)


def test_generic_trash_restore_roundtrip():
    server = generic_server()
    imap = make(server)
    msgs = {m.id: m for m in imap.iter_messages()}
    sale = msgs["INBOX|7|2"]
    entry = {"thread_id": sale.thread_id, "messages": [{"id": sale.id, "folder": "INBOX", "uid": 2,
                                                         "uidvalidity": "7", "message_id_header": "<b@x>"}]}
    assert imap.trash([entry]) == [sale.thread_id]
    assert 2 not in server.folders["INBOX"]["msgs"] and len(server.folders["Trash"]["msgs"]) == 1
    assert make(server).restore([entry]) == [sale.thread_id]
    assert len(server.folders["Trash"]["msgs"]) == 0 and len(server.folders["INBOX"]["msgs"]) == 3


def test_uidvalidity_change_stops_trash():
    server = generic_server()
    imap = make(server)
    server.folders["INBOX"]["uidvalidity"] = 99
    entry = {"thread_id": "t", "messages": [{"id": "INBOX|7|2", "folder": "INBOX", "uid": 2, "uidvalidity": "7"}]}
    with pytest.raises(BackendError):
        imap.trash([entry])
    assert 2 in server.folders["INBOX"]["msgs"]


def test_refuses_servers_without_move_or_uidplus():
    server = generic_server()
    server.caps = ["IMAP4REV1"]
    imap = make(server)
    entry = {"thread_id": "t", "messages": [{"id": "INBOX|7|2", "folder": "INBOX", "uid": 2, "uidvalidity": "7"}]}
    assert imap.trash([entry]) == []  # nothing moved, nothing expunged
    assert 2 in server.folders["INBOX"]["msgs"]


def test_copy_and_uidplus_expunge_path():
    server = generic_server()
    server.caps = ["IMAP4REV1", "UIDPLUS"]
    imap = make(server)
    entry = {"thread_id": "t", "messages": [{"id": "INBOX|7|2", "folder": "INBOX", "uid": 2, "uidvalidity": "7",
                                             "message_id_header": "<b@x>"}]}
    assert imap.trash([entry]) == ["t"]
    assert 2 not in server.folders["INBOX"]["msgs"] and 1 in server.folders["INBOX"]["msgs"]


def test_gmail_flavour_index_trash_restore_label():
    server = gmail_server()
    imap = make(server)
    assert imap.gmail and imap.all_mail == "[Gmail]/All Mail"
    msgs = {m.id: m for m in imap.iter_messages()}
    assert set(msgs) == {"gm-1001", "gm-1002", "gm-1003"}
    assert msgs["gm-1001"].thread_id == msgs["gm-1003"].thread_id == "gt-501"
    assert "Family" in msgs["gm-1001"].labels and SENT in msgs["gm-1003"].labels
    imap.label_messages([msgs["gm-1001"]], "Keep")
    assert "Keep" in server.folders["[Gmail]/All Mail"]["msgs"][10]["labels"]
    entry = {"thread_id": "gt-502", "messages": [{"id": "gm-1002", "folder": "[Gmail]/All Mail", "uid": 11,
                                                   "uidvalidity": "5"}]}
    assert imap.trash([entry]) == ["gt-502"]
    assert len(server.folders["[Gmail]/Trash"]["msgs"]) == 1
    assert make(server).restore([entry]) == ["gt-502"]
    assert len(server.folders["INBOX"]["msgs"]) == 1  # Gmail restores to the Inbox


def test_include_trash_for_eval():
    server = gmail_server()
    server.folders["[Gmail]/Trash"]["msgs"][1] = {"raw": raw_header("old@shop.example", "Old sale"), "flags": [],
                                                  "msgid": 2000, "thrid": 900, "labels": []}
    msgs = list(make(server).iter_messages(include_trash=True))
    trashed = [m for m in msgs if TRASH in m.labels]
    assert [m.id for m in trashed] == ["gm-2000"]


def test_live_protection_generic_and_gmail():
    generic = make(generic_server())
    protect = LiveProtection(me=["me@example.com"], people=["jane@gmail.com"], labels=["Taxes"])
    threads = generic.protected_thread_ids(protect)
    by_id = {m.id: m for m in make(generic_server()).iter_messages()}
    assert by_id["INBOX|7|1"].thread_id in threads   # replied + protected person
    assert by_id["Taxes|9|1"].thread_id in threads   # protected folder
    assert by_id["INBOX|7|2"].thread_id not in threads
    gm = make(gmail_server())
    assert gm.protected_thread_ids(LiveProtection(me=["me@gmail.com"])) == {"gt-501"}


def test_uid_sets_compress_and_split():
    assert uid_sets([5, 1, 2, 3, 9, 10, 12]) == ["1:3,5,9:10,12"]
    sets = uid_sets(list(range(1, 4000, 2)), max_chars=100)
    assert all(len(s) <= 100 for s in sets) and sum(s.count(",") + 1 for s in sets) == 2000


def test_plaintext_or_unverified_only_to_localhost():
    for kwargs in ({"tls": "none"}, {"verify": False}):
        with pytest.raises(BackendError):
            connect("imap.example.com", 143, **kwargs)
    with pytest.raises(BackendError):
        connect("imap.example.com", 993, tls="bogus")


def test_bad_password_is_a_clear_error():
    with pytest.raises(BackendError):
        ImapBackend("imap.example.com", 993, "me@example.com", "wrong", connect_fn=generic_server)


def test_live_check_matches_index_threads_without_message_id():
    server = generic_server()
    server.folders["INBOX"]["msgs"][5] = {"raw": raw_header("x@shop.example", "No id"), "flags": []}
    m = next(m for m in make(server).iter_messages() if m.id == "INBOX|7|5")
    server.folders["INBOX"]["msgs"][5]["flags"].append("\\Flagged")  # starred after indexing
    assert m.thread_id in make(server).protected_thread_ids(LiveProtection())

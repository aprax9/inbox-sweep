"""Regressions from the independent review: each test is a scenario that once went wrong."""

import socket
import threading
import time

import pytest

from inbox_sweep.backends.base import LiveProtection
from inbox_sweep.backends.imap import ImapBackend, decode_mutf7, encode_mutf7, folder_label
from inbox_sweep.classify import classify
from inbox_sweep.config import Config
from inbox_sweep.plan import apply_plan, build_plan, log_path_for, undo, verify_plan
from inbox_sweep.store import Store

from fakes import FakeIMAP, raw_header

LU = "List-Unsubscribe: <https://shop.example/u>"


def gmail_server():
    return FakeIMAP({
        "INBOX": {"uidvalidity": 1},
        "[Gmail]/All Mail": {"flags": ["\\All"], "uidvalidity": 5, "msgs": {
            10: {"raw": raw_header("Mom <mom@family.example>", "Dinner"), "flags": [], "msgid": 1001, "thrid": 501,
                 "labels": ["\\Inbox", "Family"]},
            11: {"raw": raw_header("deals@shop.example", "Sale", extra=LU), "flags": [], "msgid": 1002, "thrid": 502,
                 "labels": ["\\Inbox"]},
            12: {"raw": raw_header("me@gmail.com", "Re: Dinner", to="mom@family.example"), "flags": ["\\Seen"],
                 "msgid": 1003, "thrid": 501, "labels": ["\\Sent"]},
        }},
        "[Gmail]/Trash": {"flags": ["\\Trash"], "uidvalidity": 6},
    }, gmail=True)


def index_into(store, backend):
    new, rel, rem = [], [], []
    for ev in backend.sync(store.labels_by_id()):
        (new.append(ev[1]) if ev[0] == "new" else rel.append((ev[1], ev[2])) if ev[0] == "labels" else rem.append(ev[1]))
    store.upsert_messages(new)
    store.update_labels(rel)
    store.delete_messages(rem)
    store.set_meta("uidvalidity", backend.uidvalidities())
    store.set_meta("indexed_at", int(time.time()))


def test_gmail_all_mail_renumbered_between_index_and_apply(tmp_path):
    srv = gmail_server()
    make = lambda: ImapBackend("imap.gmail.com", 993, "me@gmail.com", "secret", connect_fn=lambda: srv)
    cfg = Config()
    cfg.me, cfg.data_dir, cfg.recent_days = ["me@gmail.com"], str(tmp_path), 0
    store = Store(cfg.db_path)
    index_into(store, make())
    am = srv.folders["[Gmail]/All Mail"]
    old = am["msgs"]
    am["uidvalidity"], am["msgs"] = 77, {11: old[12], 12: old[10], 10: old[11]}  # uid 11 is now your reply
    index_into(store, make())
    msgs = store.messages()
    decisions = classify(msgs, cfg)
    plan = build_plan(msgs, decisions, cfg, account="me@gmail.com", backend="imap",
                      uidvalidity=store.get_meta("uidvalidity"), indexed_at=store.get_meta("indexed_at"))
    assert verify_plan(plan, msgs, decisions, cfg) == []
    assert apply_plan(plan, make(), cfg, msgs, label_keep=False)["trashed"] == 1
    trashed = [m["msgid"] for m in srv.folders["[Gmail]/Trash"]["msgs"].values()]
    assert trashed == [1002]  # the shop mail, found by its Gmail id, not the reply now at its old UID
    assert undo(log_path_for(plan, cfg), make(), cfg)["restored"] == 1
    assert not srv.folders["[Gmail]/Trash"]["msgs"]


def test_generic_uid_reused_by_another_message_is_never_moved():
    srv = FakeIMAP({"INBOX": {"uidvalidity": 7, "msgs": {
        2: {"raw": raw_header("Mom <mom@family.example>", "Hi", msgid="<mom@x>"), "flags": []}}},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10}})
    imap = ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    entry = {"thread_id": "t", "messages": [{"id": "INBOX|7|2", "folder": "INBOX", "uid": 2, "uidvalidity": "7",
                                             "message_id_header": "<sale@shop>"}]}
    assert imap.trash([entry]) == []
    assert 2 in srv.folders["INBOX"]["msgs"] and not srv.folders["Trash"]["msgs"]


def test_modified_utf7_names_match_protected_labels():
    assert decode_mutf7("Fam&AO0-lia") == "Família" and encode_mutf7("Família") == "Fam&AO0-lia"
    assert decode_mutf7("R&AOk-sum&AOk- &- notes") == "Résumé & notes"
    for text in ("家族", "Ünïcödé/子", "a&b", "😀 photos"):
        assert decode_mutf7(encode_mutf7(text)) == text
    assert folder_label("INBOX.Fam&AO0-lia", ".") == "INBOX/Família"
    srv = FakeIMAP({
        "INBOX": {"uidvalidity": 7},
        "Fam&AO0-lia": {"uidvalidity": 9, "msgs": {1: {"raw": raw_header(
            "Family Photos <noreply@photos.example>", "Mom shared an album", msgid="<p@x>", extra=LU), "flags": []}}},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10},
    })
    b = ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    msgs = list(b.iter_messages())
    assert msgs[0].labels == ["Família"]
    cfg = Config()
    cfg.me, cfg.protect_labels, cfg.recent_days = ["me@example.com"], ["família"], 0
    assert [d.action for d in classify(msgs, cfg)] == ["keep"]
    assert b.protected_thread_ids(LiveProtection(me=cfg.me, labels=cfg.protect_labels)) == {msgs[0].thread_id}


def test_gmail_live_check_with_non_ascii_label_and_address():
    srv = gmail_server()
    srv.folders["Fam&AO0-lia"] = {"uidvalidity": 3, "next": 2, "flags": [], "msgs": {
        1: {"raw": raw_header("deals@shop.example", "Sale"), "flags": [], "msgid": 1002, "thrid": 502,
            "labels": ["Fam&AO0-lia"]}}}
    b = ImapBackend("imap.gmail.com", 993, "me@gmail.com", "secret", connect_fn=lambda: srv)
    threads = b.protected_thread_ids(LiveProtection(me=["me@gmail.com"], labels=["Família"],
                                                    people=["zoë@familie.example"]))
    assert threads == {"gt-501", "gt-502"}  # the reply's thread, and the thread filed under Família


def test_generic_undo_is_exact_even_without_message_ids_and_with_duplicates(tmp_path):
    srv = FakeIMAP({
        "INBOX": {"uidvalidity": 7, "msgs": {
            1: {"raw": raw_header("deals@shop.example", "No message-id", extra=LU), "flags": []},
            2: {"raw": raw_header("alerts@device.example", "Alert A", msgid="<same@device>", extra=LU), "flags": []},
            3: {"raw": raw_header("alerts@device.example", "Alert B", msgid="<same@device>", extra=LU), "flags": []},
            4: {"raw": raw_header("news@list.example", "Digest", msgid="<dup@list>", extra=LU), "flags": []},
        }},
        "Lists": {"uidvalidity": 8, "msgs": {
            1: {"raw": raw_header("news@list.example", "Digest", msgid="<dup@list>", extra=LU), "flags": []}}},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10},
    })
    mk = lambda: ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    cfg = Config()
    cfg.me, cfg.data_dir, cfg.recent_days = ["me@example.com"], str(tmp_path), 0
    msgs = list(mk().iter_messages())
    plan = build_plan(msgs, classify(msgs, cfg), cfg, account="me@example.com", backend="imap",
                      indexed_at=int(time.time()))
    assert plan["summary"]["trash_messages"] == 5
    apply_plan(plan, mk(), cfg, msgs, label_keep=False)
    assert len(srv.folders["Trash"]["msgs"]) == 5
    result = undo(log_path_for(plan, cfg), mk(), cfg)
    assert result["restored"] == result["total"]
    assert not srv.folders["Trash"]["msgs"]
    assert len(srv.folders["INBOX"]["msgs"]) == 4 and len(srv.folders["Lists"]["msgs"]) == 1


def test_undo_without_copyuid_falls_back_to_message_id_search(tmp_path):
    srv = FakeIMAP({"INBOX": {"uidvalidity": 7, "msgs": {
        i: {"raw": raw_header("deals@shop.example", f"Sale {i}", msgid=f"<s{i}@shop>", extra=LU), "flags": []}
        for i in range(1, 6)}}, "Trash": {"flags": ["\\Trash"], "uidvalidity": 10}})
    srv.copyuid = False
    mk = lambda: ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    cfg = Config()
    cfg.me, cfg.data_dir, cfg.recent_days = ["me@example.com"], str(tmp_path), 0
    msgs = list(mk().iter_messages())
    plan = build_plan(msgs, classify(msgs, cfg), cfg, indexed_at=int(time.time()))
    apply_plan(plan, mk(), cfg, msgs, label_keep=False)
    srv.commands.clear()
    assert undo(log_path_for(plan, cfg), mk(), cfg)["restored"] == 5
    assert srv.commands["SEARCH"] <= 6  # one search per message id, never a full Trash rescan per batch


@pytest.mark.parametrize("raw", [
    b"From: Bob <bob@x.example>\r\nSubject: hi\r\nList-Id: Caf\xc3\xa9 Club <club.x.example>\r\nMessage-ID: <a@x>\r\n\r\n",
    b"From: Bob <bob@x.example>\r\nSubject: Re: hi\r\nReferences: <r\xc3\xa9f@x>\r\nMessage-ID: <b@x>\r\n\r\n",
    b"From: Ren\xc3\xa9 <rene@x.example>\r\nSubject: caf\xc3\xa9\r\nMessage-ID: <c@x>\r\n\r\n",
    b"From: Bob <bob@x.example>\r\nSubject: hi\r\nMessage-ID: <d\xc3\xa9@x>\r\n\r\n",
    b"From: =?iso-8859-1?q?Ren=E9?= <rene@x.example>\r\nSubject: caf\xe9 latin-1\r\n\r\n",
])
def test_raw_8bit_headers_index_and_store(tmp_path, raw):
    srv = FakeIMAP({"INBOX": {"uidvalidity": 7, "msgs": {1: {"raw": raw, "flags": []}}},
                    "Trash": {"flags": ["\\Trash"], "uidvalidity": 10}})
    msgs = list(ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv).iter_messages())
    assert len(msgs) == 1 and all(isinstance(getattr(msgs[0], f), str)
                                  for f in ("subject", "list_id", "message_id_header", "from_name"))
    assert "caf" in msgs[0].subject or msgs[0].subject in ("hi", "Re: hi")
    Store(tmp_path / "i.db").upsert_messages(msgs)


def test_more_than_a_megabyte_of_uids_in_one_search_line():
    n, first = 140_000, 1_000_000

    def serve(sock):
        conn, _ = sock.accept()
        f = conn.makefile("rwb")
        f.write(b"* OK ready\r\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                return
            tag, cmd = line.split(b" ", 1)
            up = cmd.upper()
            if up.startswith(b"CAPABILITY"):
                f.write(b"* CAPABILITY IMAP4rev1 MOVE UIDPLUS\r\n")
            elif up.startswith(b"LIST"):
                f.write(b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n* LIST (\\HasNoChildren \\Trash) "/" "Trash"\r\n')
            elif up.startswith(b"SELECT") or up.startswith(b"EXAMINE"):
                f.write(b"* %d EXISTS\r\n* OK [UIDVALIDITY 42] ok\r\n" % n)
            elif up.startswith(b"UID SEARCH"):
                f.write(b"* SEARCH " + b" ".join(str(first + i).encode() for i in range(n)) + b"\r\n")
            elif up.startswith(b"LOGOUT"):
                f.write(b"* BYE\r\n" + tag + b" OK bye\r\n")
                f.flush()
                return
            f.write(tag + b" OK done\r\n")
            f.flush()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    threading.Thread(target=serve, args=(s,), daemon=True).start()
    b = ImapBackend("127.0.0.1", s.getsockname()[1], "me@example.com", "pw", tls="none")
    b._select("INBOX", readonly=True)
    assert len(b._search()) == n
    b.close()


class Flaky(FakeIMAP):
    """Drops the connection once, in the middle of indexing."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.drops = 1

    def uid(self, command, *args):
        if command.upper() == "FETCH" and self.drops:
            self.drops -= 1
            import imaplib
            raise imaplib.IMAP4.abort("socket error: EOF")
        return super().uid(command, *args)


def test_a_dropped_connection_is_reopened_once():
    srv = Flaky({"INBOX": {"uidvalidity": 7, "msgs": {
        i: {"raw": raw_header("a@shop.example", f"S{i}", msgid=f"<{i}@x>"), "flags": []} for i in range(1, 4)}},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10}})
    b = ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    assert len(list(b.iter_messages())) == 3
    assert b.imap.reconnects == 1


def test_courier_style_folders():
    srv = FakeIMAP({
        "INBOX": {"uidvalidity": 7},
        "INBOX.Family": {"uidvalidity": 8, "msgs": {1: {"raw": raw_header(
            "noreply@photos.example", "Album", msgid="<a@x>", extra=LU), "flags": []}}},
        "INBOX.Trash": {"uidvalidity": 9},
        "INBOX.Junk": {"uidvalidity": 11, "msgs": {1: {"raw": raw_header("x@spam.example", "spam"), "flags": []}}},
    })
    srv.delimiter = "."
    b = ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    assert b.trash_folder == "INBOX.Trash"
    msgs = list(b.iter_messages())
    assert [m.labels for m in msgs] == [["INBOX/Family"]]  # Junk skipped, label uses "/"
    cfg = Config()
    cfg.me, cfg.protect_labels, cfg.recent_days = ["me@example.com"], ["Family"], 0
    assert [d.action for d in classify(msgs, cfg)] == ["keep"]


def test_folder_renumbered_between_check_and_move_moves_nothing():
    srv = FakeIMAP({"INBOX": {"uidvalidity": 7, "msgs": {
        2: {"raw": raw_header("deals@shop.example", "Sale", msgid="<b@x>", extra=LU), "flags": []}}},
        "Trash": {"flags": ["\\Trash"], "uidvalidity": 10}})
    imap = ImapBackend("h", 993, "me@example.com", "secret", connect_fn=lambda: srv)
    real_select = srv.select

    def select(mailbox, readonly=False):
        if not readonly:  # the read-write select that precedes the move sees a renumbered folder
            srv.folders["INBOX"]["uidvalidity"] = 8
        return real_select(mailbox, readonly)

    srv.select = select
    entry = {"thread_id": "t", "messages": [{"id": "INBOX|7|2", "folder": "INBOX", "uid": 2, "uidvalidity": "7",
                                             "message_id_header": "<b@x>"}]}
    assert imap.trash([entry]) == []
    assert 2 in srv.folders["INBOX"]["msgs"]

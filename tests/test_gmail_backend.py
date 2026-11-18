"""Gmail backend against a fake Gmail API: quota pacing, incremental sync, fast trash."""

import pytest

pytest.importorskip("googleapiclient")

from inbox_sweep.backends.base import LiveProtection, Pacer  # noqa: E402
from inbox_sweep.backends.gmail_api import QUOTAS, GmailBackend, protection_queries  # noqa: E402

from fakes import FakeGmail, gmail_message  # noqa: E402

T0 = 1_700_000_000


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert s >= 0
        self.sleeps.append(s)
        self.t += s


def backend(svc, **kw):
    clock = FakeClock()
    return GmailBackend(service=svc, clock=clock, sleep=clock.sleep, **kw), clock


def mailbox(n=120):
    msgs = [gmail_message(f"m{i}", f"t{i // 2}", f"Sender {i} <s{i}@shop.example>", f"Offer {i}", T0 + i)
            for i in range(n)]
    msgs.append(gmail_message("draft1", "td", "me@gmail.com", "draft", T0, labels=["DRAFT"]))
    msgs.append(gmail_message("chat1", "tc", "friend@gmail.com", "chat", T0, labels=["CHAT"]))
    msgs.append(gmail_message("trash1", "tt", "x@shop.example", "old", T0, labels=["TRASH"]))
    return msgs


def test_pacer_handles_costs_bigger_than_one_second_of_budget():
    clock = FakeClock()
    pacer = Pacer(per_second=90, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        pacer.spend(1000)  # a batch of 50 gets at 20 units each
    assert clock.t == pytest.approx(3 * 1000 / 90)  # first is immediate; never hangs


def test_full_sync_reads_everything_once():
    svc = FakeGmail(mailbox())
    gm, clock = backend(svc)
    events = list(gm.sync({}))
    new = [e[1] for e in events if e[0] == "new"]
    assert len(new) == 120 and {m.id for m in new}.isdisjoint({"draft1", "chat1", "trash1"})
    assert all(m.snippet == "" for m in new)  # body snippets are never stored
    assert svc.calls["messages.get"] == 120  # drafts and chats skipped before reading


def test_incremental_sync_fetches_only_new_mail_and_tracks_labels_and_removals():
    svc = FakeGmail(mailbox(), labels=[{"id": "Label_1", "name": "Taxes", "type": "user"}])
    gm, _ = backend(svc)
    known = {m.id: m.labels for e in gm.sync({}) for m in [e[1]]}
    svc.calls.clear()

    svc.msgs["m5"]["labelIds"].append("STARRED")          # starred in the Gmail app
    svc.msgs["m6"]["labelIds"].append("Label_1")          # filed under a user label
    del svc.msgs["m7"]                                     # deleted forever by hand
    svc.msgs["new1"] = gmail_message("new1", "tn", "Friend <f@gmail.com>", "hi", T0 + 999)
    gm2, _ = backend(svc)
    events = list(gm2.sync(known))
    kinds = {e[0] for e in events}
    assert kinds == {"new", "labels", "removed"}
    assert [e[1].id for e in events if e[0] == "new"] == ["new1"]
    assert ("removed", "m7") in events
    relabelled = {e[1]: e[2] for e in events if e[0] == "labels"}
    assert "STARRED" in relabelled["m5"] and "Taxes" in relabelled["m6"]
    assert svc.calls["messages.get"] == 1  # only the new message's headers were read


def test_fast_trash_uses_batch_modify_and_verifies_once():
    svc = FakeGmail(mailbox(30))
    gm, _ = backend(svc)
    entries = [{"thread_id": f"t{i}", "messages": [{"id": f"m{2 * i}"}, {"id": f"m{2 * i + 1}"}]} for i in range(15)]
    assert gm.trash(entries) == [f"t{i}" for i in range(15)]
    assert svc.calls["messages.batchModify"] == 1 and svc.calls["messages.trash"] == 0
    assert svc.calls["messages.get"] == 1  # one verification read
    assert all("TRASH" in svc.msgs[f"m{i}"]["labelIds"] for i in range(30))
    assert gm.restore(entries) == [f"t{i}" for i in range(15)]
    assert not any("TRASH" in svc.msgs[f"m{i}"]["labelIds"] for i in range(30))


def test_fast_trash_chunks_at_1000_messages():
    msgs = [gmail_message(f"m{i}", f"t{i}", "a@shop.example", "x", T0 + i) for i in range(2500)]
    svc = FakeGmail(msgs)
    gm, _ = backend(svc)
    entries = [{"thread_id": f"t{i}", "messages": [{"id": f"m{i}"}]} for i in range(2500)]
    assert len(gm.trash(entries)) == 2500
    assert svc.calls["messages.batchModify"] == 3


def single_threads(n):
    msgs = [gmail_message(f"m{i}", f"t{i}", "a@shop.example", "x", T0 + i) for i in range(n)]
    return msgs, [{"thread_id": f"t{i}", "messages": [{"id": f"m{i}"}]} for i in range(n)]


def test_falls_back_to_per_message_trash_when_label_trash_is_rejected():
    msgs, entries = single_threads(1500)
    svc = FakeGmail(msgs, reject_trash_label=True)
    gm, _ = backend(svc)
    assert gm.trash(entries) == [e["thread_id"] for e in entries]
    assert svc.calls["messages.batchModify"] == 2 and not gm.fast_trash  # gave up after two tries
    assert svc.calls["messages.trash"] == 1500 and svc.calls["threads.trash"] == 0
    assert gm.restore(entries) == [e["thread_id"] for e in entries]
    assert svc.calls["messages.untrash"] == 1500 and svc.calls["messages.batchModify"] == 2


def test_mail_arriving_in_a_planned_thread_is_never_trashed():
    for reject in (False, True):
        svc = FakeGmail(mailbox(4), reject_trash_label=reject)
        gm, _ = backend(svc)
        entries = [{"thread_id": "t0", "messages": [{"id": "m0"}, {"id": "m1"}]}]
        svc.msgs["late"] = gmail_message("late", "t0", "Friend <f@gmail.com>", "Re: Offer", T0 + 50)
        assert gm.trash(entries) == ["t0"]
        assert "TRASH" in svc.msgs["m0"]["labelIds"] and "TRASH" not in svc.msgs["late"]["labelIds"]


def test_a_message_deleted_since_planning_does_not_stop_the_rest():
    msgs, entries = single_threads(3000)
    svc = FakeGmail(msgs, strict_ids=True)
    gm, _ = backend(svc)
    del svc.msgs["m1500"]  # deleted forever by hand after the plan was made
    done = gm.trash(entries)
    assert len(done) == 2999 and "t1500" not in done
    assert all("TRASH" in m["labelIds"] for m in svc.msgs.values())
    assert gm.fast_trash and svc.calls["messages.trash"] == 1  # isolated by halving, not slow path


def test_huge_thread_is_split_across_calls():
    msgs = [gmail_message(f"m{i}", "big", "a@shop.example", "x", T0 + i) for i in range(2300)]
    svc = FakeGmail(msgs)
    gm, _ = backend(svc)
    assert gm.trash([{"thread_id": "big", "messages": [{"id": f"m{i}"} for i in range(2300)]}]) == ["big"]
    assert all("TRASH" in m["labelIds"] for m in svc.msgs.values())


def test_quota_is_never_exceeded_in_any_minute_and_pacing_is_tight():
    msgs, _ = single_threads(1500)
    svc = FakeGmail(msgs)
    gm, clock = backend(svc)
    log = []
    real = gm.pacer.spend

    def spend(units):
        real(units)
        log.append((clock.t, units))

    gm.pacer.spend = spend
    assert sum(1 for e in gm.sync({}) if e[0] == "new") == 1500
    limit = QUOTAS["2026"]["per_minute"]
    worst = max(sum(u for t2, u in log[i:] if t2 < t + 60) for i, (t, _) in enumerate(log))
    assert worst <= limit
    total = sum(u for _, u in log)
    assert clock.t <= total / (limit / 60) * 1.25  # within 25% of the fastest legal pace


def test_drafts_and_chats_are_never_read():
    svc = FakeGmail(mailbox(10))
    gm, _ = backend(svc)
    known = {e[1].id: e[1].labels for e in gm.sync({})}
    assert svc.calls["messages.get"] == 10
    svc.calls.clear()
    assert [e for e in backend(svc)[0].sync(known) if e[0] != "labels"] == []
    assert svc.calls["messages.get"] == 0  # nothing re-read on the second run


def test_throttled_requests_are_retried():
    svc = FakeGmail(mailbox(4), throttle_once={"m1", "m2"})
    gm, clock = backend(svc)
    new = [e[1].id for e in gm.sync({}) if e[0] == "new"]
    assert sorted(new) == ["m0", "m1", "m2", "m3"]
    assert any(s >= 1 for s in clock.sleeps)  # backed off before retrying


def test_live_protection_queries_and_threads():
    msgs = [gmail_message("a", "ta", "Me <me@gmail.com>", "hello", T0, labels=["SENT"]),
            gmail_message("b", "tb", "x@shop.example", "deal", T0, labels=["INBOX", "STARRED"]),
            gmail_message("c", "tc", "Mom <mom@family.example>", "dinner", T0),
            gmail_message("d", "td", "x@shop.example", "deal", T0)]
    svc = FakeGmail(msgs)
    gm, _ = backend(svc)
    protect = LiveProtection(me=["me@gmail.com"], people=["mom@family.example"], labels=["Keep"])
    assert gm.protected_thread_ids(protect) == {"ta", "tb", "tc"}
    long = LiveProtection(people=[f"person{i}@example.org" for i in range(200)])
    assert len(protection_queries(long)) > 1 and all(len(q) <= 1200 for q in protection_queries(long))


def test_label_messages_creates_label_and_batches():
    svc = FakeGmail(mailbox(10))
    gm, _ = backend(svc)
    from inbox_sweep.model import Message
    gm.label_messages([Message(id=f"m{i}", thread_id="t", date=0, from_email="x") for i in range(10)], "Keep")
    keep_id = next(l["id"] for l in svc.labels_.values() if l["name"] == "Keep")
    assert all(keep_id in svc.msgs[f"m{i}"]["labelIds"] for i in range(10))
    assert svc.calls["messages.batchModify"] == 1

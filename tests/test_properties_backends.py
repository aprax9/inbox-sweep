"""Property tests for the backend helpers where an off-by-one would silently move the wrong mail."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from inbox_sweep.backends.base import LiveProtection
from inbox_sweep.backends.imap import parse_fetch_response, uid_sets

FAST = settings(max_examples=300, deadline=None)


def expand(sets: list[str]) -> list[int]:
    out = []
    for s in sets:
        for part in s.split(","):
            if ":" in part:
                a, b = map(int, part.split(":"))
                out.extend(range(a, b + 1))
            else:
                out.append(int(part))
    return out


@FAST
@given(st.lists(st.integers(1, 10**9), max_size=400), st.integers(25, 2000))
def test_uid_sets_cover_exactly_the_requested_uids(uids, max_chars):
    sets = uid_sets(uids, max_chars=max_chars)
    assert expand(sets) == sorted(set(uids))  # nothing extra, nothing missing, no overlap
    assert all(len(s) <= max_chars for s in sets)
    assert all(s for s in sets)


@FAST
@given(st.lists(st.integers(0, 1500), max_size=60))
def test_gmail_batches_keep_threads_whole_and_lose_nothing(sizes):
    pytest.importorskip("googleapiclient")
    from inbox_sweep.backends.gmail_api import MODIFY_CHUNK, GmailBackend

    entries = [{"thread_id": f"t{i}", "messages": [{"id": f"m{i}-{k}"} for k in range(n)]}
               for i, n in enumerate(sizes)]
    batches = GmailBackend._batches(entries)
    assert [e for b in batches for e in b] == entries
    for b in batches:
        total = sum(len(e["messages"]) for e in b)
        assert total <= MODIFY_CHUNK or len(b) == 1  # only a single huge thread may exceed it


@FAST
@given(st.lists(st.from_regex(r"[a-z]{1,20}@[a-z]{1,15}\.example", fullmatch=True), max_size=80),
       st.lists(st.from_regex(r"[A-Za-z ]{1,25}", fullmatch=True), max_size=5))
def test_protection_queries_cover_every_term_once(people, labels):
    pytest.importorskip("googleapiclient")
    from inbox_sweep.backends.gmail_api import protection_queries

    protect = LiveProtection(me=["me@example.com"], people=people, labels=[l for l in labels if l.strip()])
    queries = protection_queries(protect, max_chars=300)
    terms = [t for q in queries for t in q.split(" OR ")]
    for person in protect.people:
        assert terms.count(f"from:{person}") >= 1 and terms.count(f"to:{person}") >= 1
    assert "is:starred" in terms and "in:sent" in terms and "from:me@example.com" in terms
    assert all(len(q) <= 300 for q in queries)


@pytest.mark.parametrize("labels", [b'("UID 99" \\\\Inbox)', b"(UID 99)", b'("FLAGS (\\\\Flagged)")'])
def test_fetch_parser_is_not_fooled_by_label_names(labels):
    meta = (b"1 (X-GM-THRID 5 X-GM-MSGID 6 X-GM-LABELS " + labels +
            b" UID 7 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM)] {12}")
    rec = parse_fetch_response([(meta, b"From: a@b.c\r\n"), b")"])[0]
    assert rec["uid"] == 7
    assert rec["flags"] == ["\\seen"]

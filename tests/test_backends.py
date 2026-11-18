"""Backend parsing, without touching a real mailbox."""

from inbox_sweep.backends.gmail_api import message_from_gmail
from inbox_sweep.backends.imap import (
    parse_fetch_response,
    parse_label_list,
    parse_list_line,
    record_to_message,
    thread_root,
)
from inbox_sweep.model import INBOX, SENT, STARRED

HEADER = (
    b"From: Jane Doe <Jane@Example.org>\r\n"
    b"To: me@example.com\r\n"
    b"Subject: =?utf-8?q?Caf=C3=A9_plans?=\r\n"
    b"Date: Tue, 10 Mar 2015 09:00:00 +0000\r\n"
    b"Message-ID: <abc@example.org>\r\n"
    b"References: <root@example.org> <abc0@example.org>\r\n"
    b"List-Unsubscribe: <https://example.org/u>\r\n\r\n"
)


def test_gmail_metadata_to_message():
    data = {
        "id": "18a", "threadId": "18a", "internalDate": "1426000000000", "snippet": "hi",
        "labelIds": ["INBOX", "STARRED", "Label_7"],
        "payload": {"headers": [
            {"name": "From", "value": "Jane Doe <jane@example.org>"},
            {"name": "To", "value": "me@example.com, b@x.org"},
            {"name": "Subject", "value": "Hello"},
            {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        ]},
    }
    m = message_from_gmail(data, {"Label_7": "Family"}, {"18a"})
    assert m.from_email == "jane@example.org" and m.from_name == "Jane Doe"
    assert m.labels == ["INBOX", "STARRED", "Family"]
    assert m.date == 1426000000 and m.has_attachment
    assert m.to == ["me@example.com", "b@x.org"]


def test_imap_list_line_parsing():
    assert parse_list_line(b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"') == ({"\\hasnochildren", "\\all"}, "[Gmail]/All Mail")
    assert parse_list_line(b'(\\HasNoChildren) "." INBOX') == ({"\\hasnochildren"}, "INBOX")


def test_imap_label_tokens():
    assert parse_label_list(b'\\Inbox "Job Hunt" Keep "Say \\"hi\\""') == ["\\Inbox", "Job Hunt", "Keep", 'Say "hi"']


def test_imap_fetch_parsing_gmail_with_trailing_items():
    data = [
        (b'1 (X-GM-THRID 1626 X-GM-MSGID 1627 X-GM-LABELS (\\Inbox "\\\\Starred" "Job Hunt") UID 42 '
         b'INTERNALDATE "10-Mar-2015 09:00:00 +0000" BODY[HEADER.FIELDS (FROM)] {300}', HEADER),
        b" FLAGS (\\Seen \\Flagged))",
    ]
    recs = parse_fetch_response(data)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["uid"] == 42 and rec["thrid"] == "1626" and rec["msgid"] == "1627"
    assert "\\flagged" in rec["flags"]
    m = record_to_message(rec, "[Gmail]/All Mail", "9", gmail=True, folder_flags={"\\all"})
    assert m.id == "gm-1627" and m.thread_id == "gt-1626"
    assert m.subject == "Café plans" and m.from_email == "jane@example.org"
    assert INBOX in m.labels and STARRED in m.labels and "Job Hunt" in m.labels
    assert m.uid == 42 and m.folder == "[Gmail]/All Mail"


def test_imap_generic_threads_and_folders():
    data = [(b'3 (UID 7 FLAGS () BODY[HEADER.FIELDS (FROM)] {300}', HEADER), b")"]
    rec = parse_fetch_response(data)[0]
    m = record_to_message(rec, "Sent Items", "55", gmail=False, folder_flags={"\\sent"})
    assert m.id == "Sent Items|55|7"
    assert SENT in m.labels
    # Thread id comes from the first References entry, so replies share it.
    from email import message_from_bytes
    assert m.thread_id == thread_root(message_from_bytes(HEADER), "x")
    m2 = record_to_message(rec, "Archive/2015", "55", gmail=False, folder_flags=set())
    assert "Archive/2015" in m2.labels

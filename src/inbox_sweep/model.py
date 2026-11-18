"""Provider-neutral message record.

Every backend (Gmail API, IMAP, demo) produces these. Only headers are stored,
never bodies, so the local index stays small and nothing sensitive is copied.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# Provider-neutral system labels. Backends map their own flags onto these.
INBOX = "INBOX"
SENT = "SENT"
STARRED = "STARRED"
IMPORTANT = "IMPORTANT"
DRAFT = "DRAFT"
CHAT = "CHAT"
TRASH = "TRASH"
SPAM = "SPAM"
CATEGORY_PROMOTIONS = "CATEGORY_PROMOTIONS"
CATEGORY_SOCIAL = "CATEGORY_SOCIAL"
CATEGORY_UPDATES = "CATEGORY_UPDATES"
CATEGORY_FORUMS = "CATEGORY_FORUMS"

SYSTEM_LABELS = {
    INBOX, SENT, STARRED, IMPORTANT, DRAFT, CHAT, TRASH, SPAM,
    CATEGORY_PROMOTIONS, CATEGORY_SOCIAL, CATEGORY_UPDATES, CATEGORY_FORUMS,
    "UNREAD", "CATEGORY_PERSONAL",
}


@dataclass
class Message:
    id: str
    thread_id: str
    date: int  # unix seconds, UTC
    from_email: str
    from_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    labels: list[str] = field(default_factory=list)
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""
    list_id: str = ""
    precedence: str = ""
    auto_submitted: str = ""
    snippet: str = ""
    has_attachment: bool = False
    message_id_header: str = ""
    # IMAP-only location data, needed to move the message later.
    folder: str = ""
    uid: int = 0

    @property
    def from_domain(self) -> str:
        return self.from_email.rsplit("@", 1)[-1] if "@" in self.from_email else ""

    def has_label(self, name: str) -> bool:
        target = name.lower()
        return any(label.lower() == target for label in self.labels)

    def to_row(self) -> dict:
        row = asdict(self)
        row["to"] = json.dumps(self.to)
        row["cc"] = json.dumps(self.cc)
        row["labels"] = json.dumps(self.labels)
        row["has_attachment"] = int(self.has_attachment)
        return row

    @classmethod
    def from_row(cls, row) -> "Message":
        data = dict(row)
        data["to"] = json.loads(data.get("to") or "[]")
        data["cc"] = json.loads(data.get("cc") or "[]")
        data["labels"] = json.loads(data.get("labels") or "[]")
        data["has_attachment"] = bool(data.get("has_attachment"))
        return cls(**data)


@dataclass
class Decision:
    message_id: str
    thread_id: str
    action: str  # "keep" | "trash"
    category: str
    reason: str
    protected: str = ""  # "" | "absolute" | "soft"

"""The interface every mail backend implements.

A backend only does four things: list headers, add a label, move threads to
Trash, and move them back. All judgment happens locally in classify.py, so a
new provider (Outlook Graph, Fastmail JMAP, ...) is one small file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterator, Optional

from inbox_sweep.model import Message

Progress = Optional[Callable[[str], None]]


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    name = "base"
    #: True if label_messages() is supported (Gmail API, Gmail over IMAP, demo).
    supports_labels = True

    @abstractmethod
    def account(self) -> str:
        """The mailbox address, shown in plans so you can't apply one to the wrong account."""

    @abstractmethod
    def iter_messages(self, query: str | None = None, limit: int | None = None,
                      progress: Progress = None) -> Iterator[Message]:
        """Yield header-only records for every message outside Trash and Spam.

        Drafts and chats must be skipped: they are never candidates for Trash.
        """

    @abstractmethod
    def label_messages(self, messages: list[Message], label: str, progress: Progress = None) -> None:
        """Add `label` to these messages, creating the label if needed."""

    @abstractmethod
    def trash(self, entries: list[dict], progress: Progress = None) -> list[str]:
        """Move whole threads to Trash. Return the thread ids that succeeded.

        Each entry is a plan entry: {"thread_id", "messages": [{"id", "folder", "uid",
        "message_id_header"}, ...], ...}. Never delete permanently.
        """

    @abstractmethod
    def restore(self, entries: list[dict], progress: Progress = None) -> list[str]:
        """Undo trash() for these entries. Return the thread ids restored."""

    def close(self) -> None:  # pragma: no cover - optional
        pass


def chunked(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]

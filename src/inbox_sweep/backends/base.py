"""The interface every mail backend implements.

A backend lists headers, adds a label, moves threads to Trash and back, and can
say which threads are protected *right now* (so `apply` can re-check the live
mailbox). All judgment happens locally in classify.py, so a new provider is one
small file.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Iterable, Iterator, Optional, Union

from inbox_sweep.model import Message

Progress = Optional[Callable[[str], None]]

# sync() events: ("new", Message) | ("labels", message_id, [labels]) | ("removed", message_id)
SyncEvent = Union[tuple[str, Message], tuple[str, str, list], tuple[str, str]]


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    name = "base"
    #: True if label_messages() is supported (Gmail API, Gmail over IMAP, Outlook, demo).
    supports_labels = True

    @abstractmethod
    def account(self) -> str:
        """The mailbox address, shown in plans so you can't apply one to the wrong account."""

    @abstractmethod
    def iter_messages(self, query: str | None = None, limit: int | None = None,
                      progress: Progress = None, include_trash: bool = False) -> Iterator[Message]:
        """Yield header-only records for every message outside Trash and Spam.

        Drafts and chats must be skipped: they are never candidates for Trash.
        With include_trash, messages in Trash are included and carry the TRASH label
        (used only by `eval`, never by plans).
        """

    def sync(self, known: dict[str, list[str]], query: str | None = None, limit: int | None = None,
             progress: Progress = None, include_trash: bool = False) -> Iterator[SyncEvent]:
        """Bring an existing index up to date.

        `known` maps indexed message ids to their stored labels. Yields "new" for
        messages not yet indexed, "labels" when a known message's labels changed,
        and "removed" for indexed messages no longer in the mailbox (only when the
        whole mailbox was scanned). Backends override this to avoid re-reading
        headers they already have; this default re-reads everything.
        """
        seen: set[str] = set()
        for msg in self.iter_messages(query=query, limit=limit, progress=progress, include_trash=include_trash):
            seen.add(msg.id)
            if msg.id in known:
                if sorted(known[msg.id]) != sorted(msg.labels):
                    yield ("labels", msg.id, msg.labels)
            else:
                yield ("new", msg)
        if query is None and limit is None:
            for mid in known.keys() - seen:
                yield ("removed", mid)

    @abstractmethod
    def label_messages(self, messages: list[Message], label: str, progress: Progress = None) -> None:
        """Add `label` to these messages, creating the label if needed."""

    @abstractmethod
    def trash(self, entries: list[dict], progress: Progress = None) -> list[str]:
        """Move whole threads to Trash. Return the thread ids that succeeded.

        Each entry is a plan entry: {"thread_id", "messages": [{"id", "folder", "uid",
        "message_id_header"}, ...], ...}. A backend may add keys to the message refs
        (e.g. "trash_id"); they are written to the undo log. Never delete permanently.
        """

    @abstractmethod
    def restore(self, entries: list[dict], progress: Progress = None) -> list[str]:
        """Undo trash() for these entries. Return the thread ids restored."""

    def protected_thread_ids(self, protect: "LiveProtection") -> set[str] | None:
        """Thread ids that are protected in the live mailbox right now.

        `apply` calls this just before trashing, so a message starred or replied to
        after indexing is still safe. Return None if the backend can't check.
        """
        return None

    def close(self) -> None:  # pragma: no cover - optional
        pass


class LiveProtection:
    """What `apply` asks the live mailbox about: the absolute protections."""

    def __init__(self, me: Iterable[str] = (), people: Iterable[str] = (), labels: Iterable[str] = (),
                 starred: bool = True, sent: bool = True):
        self.me = [m.lower() for m in me]
        self.people = [p.lower() for p in people if "@" in p]
        self.labels = [l for l in labels if l]
        self.starred = starred
        self.sent = sent


def chunked(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class Pacer:
    """Spreads work evenly so a quota of `per_second` units is never exceeded.

    Each spend(n) reserves n / per_second seconds; callers wait for their slot.
    Works for any n, including more than one second's worth.
    """

    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self.per_second = per_second
        self.clock = clock
        self.sleep = sleep
        self.free_at = clock()

    def spend(self, units: float) -> None:
        now = self.clock()
        start = max(now, self.free_at)
        self.free_at = start + units / self.per_second
        if start > now:
            self.sleep(start - now)

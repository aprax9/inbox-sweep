import itertools

import pytest

from inbox_sweep.config import Config
from inbox_sweep.model import Message

NOW = 1_800_000_000
DAY = 86400
_ids = itertools.count(1)


def make(sender="someone@example.org", subject="Hello", days_ago=400, *, name="", thread=None,
         labels=None, to=None, bulk=False, list_id="", lup="", lu=None, **kw) -> Message:
    n = next(_ids)
    return Message(
        id=f"m{n}",
        thread_id=thread or f"t{n}",
        date=NOW - int(days_ago * DAY),
        from_email=sender,
        from_name=name,
        to=to if to is not None else ["me@example.com"],
        subject=subject,
        labels=list(labels or []),
        list_unsubscribe=lu if lu is not None else ("<https://lists.example.org/u>" if bulk else ""),
        list_unsubscribe_post=lup,
        list_id=list_id,
        **kw,
    )


@pytest.fixture
def cfg():
    c = Config()
    c.me = ["me@example.com"]
    return c

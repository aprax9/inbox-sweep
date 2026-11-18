"""One-click unsubscribe (RFC 8058) for senders whose mail is all being trashed.

Since 2024 Gmail and Yahoo require bulk senders to support one-click unsubscribe:
a single HTTPS POST to the URL in the List-Unsubscribe header. That is what this
does. It never opens links in email bodies, never sends email for mailto: unsubscribes,
and never contacts private or local addresses. Senders without one-click support
are listed for you to handle by hand.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from inbox_sweep.headers import is_bulk, one_click_url, parse_list_unsubscribe
from inbox_sweep.model import Decision, Message

UNSUBSCRIBE_CATEGORIES = {"newsletter", "promotion", "social", "other_automated", "job_automated", "account_notice"}
USER_AGENT = "inbox-sweep (RFC 8058 one-click unsubscribe)"


def is_safe_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP literals: refuse, they're how SSRF tricks start
    except ValueError:
        return True


def _blocks_unsubscribe(d: Decision) -> bool:
    """A sender is off-limits if any of its mail is protected for a reason other than being recent."""
    if d.protected == "absolute":
        return True
    return d.protected == "soft" and not d.reason.startswith("from the last")


def candidates(messages: list[Message], decisions: list[Decision]) -> list[dict]:
    """Bulk senders whose mail is being trashed (apart from the recent working set)
    and who offer an unsubscribe header."""
    by_id = {d.message_id: d for d in decisions}
    groups: dict[str, list[tuple[Message, Decision]]] = defaultdict(list)
    for m in messages:
        d = by_id.get(m.id)
        if d:
            groups[m.from_email].append((m, d))
    out = []
    for sender, items in groups.items():
        if any(_blocks_unsubscribe(d) for _, d in items) or not any(d.action == "trash" for _, d in items):
            continue
        items.sort(key=lambda md: md[0].date, reverse=True)
        latest, _ = items[0]
        category = Counter(d.category for _, d in items).most_common(1)[0][0]
        if not is_bulk(latest) or category not in UNSUBSCRIBE_CATEGORIES:
            continue
        uris = parse_list_unsubscribe(latest.list_unsubscribe)
        if not uris:
            continue
        url = one_click_url(latest)
        if url and is_safe_url(url):
            method, target = "one-click", url
        elif any(u.lower().startswith("mailto:") for u in uris):
            method, target = "mailto", next(u for u in uris if u.lower().startswith("mailto:"))
        else:
            method, target = "link", next((u for u in uris if u.lower().startswith("https://")), uris[0])
        out.append({"sender": sender, "name": latest.from_name, "messages": len(items),
                    "method": method, "target": target})
    out.sort(key=lambda c: -c["messages"])
    return out


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "refusing unsafe redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirects)


def one_click(url: str, timeout: float = 15.0, opener=None) -> tuple[bool, str]:
    if not is_safe_url(url):
        return False, "unsafe URL"
    req = urllib.request.Request(
        url, data=b"List-Unsubscribe=One-Click", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    try:
        with (opener or _opener).open(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))[:120]


def run(cands: list[dict], log_path: str | Path, opener=None, progress=None) -> dict:
    say = progress or (lambda _m: None)
    results = {"ok": 0, "failed": 0, "manual": 0}
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        for c in cands:
            if c["method"] != "one-click":
                results["manual"] += 1
                continue
            ok, detail = one_click(c["target"], opener=opener)
            results["ok" if ok else "failed"] += 1
            fh.write(json.dumps({"sender": c["sender"], "ok": ok, "detail": detail, "at": int(time.time())}) + "\n")
            say(f"{'unsubscribed' if ok else 'failed'}: {c['sender']} ({detail})")
    return results

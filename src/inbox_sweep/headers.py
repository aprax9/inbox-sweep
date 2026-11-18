"""Header parsing and the "is this a person or a machine?" signals."""

from __future__ import annotations

import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from inbox_sweep.model import (
    CATEGORY_FORUMS,
    CATEGORY_PROMOTIONS,
    CATEGORY_SOCIAL,
    CATEGORY_UPDATES,
    Message,
)

# Headers every backend asks for. Keep this list short: it is fetched for
# every message in the mailbox.
WANTED_HEADERS = [
    "From", "To", "Cc", "Subject", "Date", "Message-ID",
    "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id",
    "Precedence", "Auto-Submitted",
]

# Free webmail and ISP domains. A non-bulk sender on one of these is almost
# always a person. Extend it in sweep.toml with [protect] personal_domains.
PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "hotmail.ca", "live.com", "live.ca", "live.co.uk", "msn.com", "yahoo.com",
    "yahoo.ca", "yahoo.co.uk", "yahoo.co.in", "yahoo.com.hk", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "pm.me", "gmx.com", "gmx.de", "web.de", "mail.com", "zoho.com", "fastmail.com",
    "hey.com", "qq.com", "163.com", "126.com", "naver.com", "rediffmail.com",
    "rogers.com", "sympatico.ca", "bell.net", "comcast.net", "verizon.net",
    "att.net", "btinternet.com", "orange.fr", "free.fr", "t-online.de",
})

NOREPLY_RE = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|notify|alerts?|"
    r"mailer[-_]daemon|postmaster|bounces?|newsletters?|news|info|marketing|"
    r"promo(tions)?|offers|deals|updates?|digest|hello|team|support|service|"
    r"billing|receipts?|orders?|bookings?|reservations?|accounts?|security|"
    r"membership|members|rewards|feedback|surveys?|events?|careers|jobs|"
    r"recruiting|talent|automated|system|admin)([-_.+].*)?$",
    re.IGNORECASE,
)

BULK_PRECEDENCE = {"bulk", "list", "junk"}
MACHINE_CATEGORY_LABELS = {CATEGORY_PROMOTIONS, CATEGORY_SOCIAL, CATEGORY_FORUMS, CATEGORY_UPDATES}


def decode(value: str | None) -> str:
    """Decode RFC 2047 encoded-words ("=?utf-8?q?...?=") into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # malformed encodings are common in old mail
        return value.strip()


def parse_from(value: str | None) -> tuple[str, str]:
    name, addr = parseaddr(decode(value))
    return name.strip().strip('"'), addr.strip().lower()


def parse_address_list(*values: str | None) -> list[str]:
    decoded = [decode(v) for v in values if v]
    return [addr.strip().lower() for _, addr in getaddresses(decoded) if "@" in addr]


def parse_date(value: str | None, fallback: int = 0) -> int:
    if not value:
        return fallback
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except Exception:
        return fallback


def parse_list_unsubscribe(value: str | None) -> list[str]:
    """Return the URIs inside a List-Unsubscribe header, in order."""
    if not value:
        return []
    return [uri.strip() for uri in re.findall(r"<([^>]+)>", value)]


def one_click_url(msg: Message) -> str:
    """The RFC 8058 one-click unsubscribe URL, or "" if the sender doesn't offer one."""
    if "list-unsubscribe=one-click" not in msg.list_unsubscribe_post.lower().replace(" ", ""):
        return ""
    for uri in parse_list_unsubscribe(msg.list_unsubscribe):
        if uri.lower().startswith("https://"):
            return uri
    return ""


def is_bulk(msg: Message) -> bool:
    """True when the message carries mailing-list or auto-generated markers."""
    if msg.list_unsubscribe or msg.list_id:
        return True
    if msg.precedence.strip().lower() in BULK_PRECEDENCE:
        return True
    auto = msg.auto_submitted.strip().lower()
    return bool(auto) and auto != "no"


def is_noreply_address(email: str) -> bool:
    local = email.split("@", 1)[0]
    return bool(NOREPLY_RE.match(local))


def is_machine(msg: Message) -> bool:
    """Best guess that no human typed this message."""
    if is_bulk(msg) or is_noreply_address(msg.from_email):
        return True
    return any(label in MACHINE_CATEGORY_LABELS for label in msg.labels)


def is_personal_domain(domain: str, extra: frozenset[str] | set[str] = frozenset()) -> bool:
    domain = domain.lower()
    return domain in PERSONAL_DOMAINS or domain in extra


def domain_matches(email_or_domain: str, patterns: list[str]) -> bool:
    """True if the address's domain equals a pattern or is a subdomain of it."""
    domain = email_or_domain.rsplit("@", 1)[-1].lower()
    for pattern in patterns:
        p = pattern.lower().lstrip("@").lstrip(".")
        if domain == p or domain.endswith("." + p):
            return True
    return False

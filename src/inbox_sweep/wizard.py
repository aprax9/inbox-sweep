"""`inbox-sweep init`: a short interview that writes a working sweep.toml."""

from __future__ import annotations

import json
import re
from typing import Callable

from inbox_sweep.config import CONFIG_TEMPLATE

PROVIDER_FOR_DOMAIN = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "hotmail.co.uk": "outlook", "hotmail.ca": "outlook",
    "live.com": "outlook", "live.ca": "outlook", "live.co.uk": "outlook", "msn.com": "outlook",
}
IMAP_HOSTS = {
    "gmail.com": ("imap.gmail.com", 993, "ssl"), "googlemail.com": ("imap.gmail.com", 993, "ssl"),
    "icloud.com": ("imap.mail.me.com", 993, "ssl"), "me.com": ("imap.mail.me.com", 993, "ssl"),
    "mac.com": ("imap.mail.me.com", 993, "ssl"), "yahoo.com": ("imap.mail.yahoo.com", 993, "ssl"),
    "ymail.com": ("imap.mail.yahoo.com", 993, "ssl"), "aol.com": ("imap.aol.com", 993, "ssl"),
    "fastmail.com": ("imap.fastmail.com", 993, "ssl"), "fastmail.fm": ("imap.fastmail.com", 993, "ssl"),
    "zoho.com": ("imap.zoho.com", 993, "ssl"), "gmx.com": ("imap.gmx.com", 993, "ssl"),
    "gmx.de": ("imap.gmx.net", 993, "ssl"), "web.de": ("imap.web.de", 993, "ssl"),
    # Proton Mail only speaks IMAP through the local Bridge app.
    "proton.me": ("127.0.0.1", 1143, "starttls"), "protonmail.com": ("127.0.0.1", 1143, "starttls"),
    "pm.me": ("127.0.0.1", 1143, "starttls"),
}


def _split(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", text or "") if p.strip()]


def suggest(email: str) -> dict:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    provider = PROVIDER_FOR_DOMAIN.get(domain, "imap")
    host, port, tls = IMAP_HOSTS.get(domain, (f"imap.{domain}" if domain else "imap.example.com", 993, "ssl"))
    return {"provider": provider, "imap_host": host, "imap_port": port, "imap_tls": tls}


def _set(text: str, section: str, key: str, value) -> str:
    """Replace `key = ...` inside [section] of the template, keeping its trailing comment."""
    rendered = ("true" if value else "false") if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
    lines = text.split("\n")
    in_section = False
    for i, line in enumerate(lines):
        if re.match(r"^\[[^\]]+\]\s*$", line):
            in_section = line.strip() == f"[{section}]"
            continue
        m = re.match(rf"^{re.escape(key)}\s*=\s*(.*?)(\s+#.*)?$", line) if in_section else None
        if m:
            lines[i] = f"{key} = {rendered}{m.group(2) or ''}"
            return "\n".join(lines)
    raise ValueError(f"template has no {section}.{key}")


def render_config(*, provider: str, me: list[str], imap_host: str = "", imap_port: int = 993,
                  imap_tls: str = "ssl", imap_user: str = "", people: list[str] = (), domains: list[str] = (),
                  labels: list[str] = (), keywords: list[str] = (), private: bool = False) -> str:
    text = CONFIG_TEMPLATE
    text = _set(text, "account", "me", list(me))
    text = _set(text, "backend", "type", provider)
    if provider == "imap":
        text = _set(text, "imap", "host", imap_host)
        text = _set(text, "imap", "port", int(imap_port))
        text = _set(text, "imap", "tls", imap_tls)
        text = _set(text, "imap", "user", imap_user or (me[0] if me else ""))
        if imap_host in ("127.0.0.1", "localhost"):
            text = _set(text, "imap", "verify", False)  # local bridges use self-signed certificates
    text = _set(text, "protect", "people", list(people))
    text = _set(text, "protect", "domains", list(domains))
    text = _set(text, "protect", "labels", list(labels))
    text = _set(text, "protect", "keywords", list(keywords))
    text = _set(text, "privacy", "private", bool(private))
    return text


def interview(ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> dict:
    """Ask the handful of questions that make the defaults safe for this person."""
    say("A few questions so nothing important gets trashed. Press Enter to accept [defaults].\n")
    me = _split(ask("Your email address(es), comma-separated: "))
    while not me:
        me = _split(ask("At least one address, please (it's how replies are recognised): "))
    guess = suggest(me[0])
    provider = (ask(f"Mail provider: gmail, outlook or imap [{guess['provider']}]: ").strip().lower()
                or guess["provider"])
    while provider not in ("gmail", "outlook", "imap"):
        provider = ask("Please type gmail, outlook or imap: ").strip().lower()
    answers = {"provider": provider, "me": me, "imap_user": me[0]}
    if provider == "imap":
        host = ask(f"IMAP server [{guess['imap_host']}]: ").strip() or guess["imap_host"]
        answers.update(imap_host=host, imap_port=guess["imap_port"] if host == guess["imap_host"] else 993,
                       imap_tls=guess["imap_tls"] if host == guess["imap_host"] else "ssl")
    say("\nWho must never be trashed? Family surnames or addresses, close friends' addresses.")
    answers["people"] = _split(ask("Protected people (comma-separated, optional): "))
    say("Employers and schools: protecting a domain protects the people there, not its newsletters.")
    answers["domains"] = _split(ask("Protected domains, e.g. employer.com, school.edu (optional): "))
    answers["labels"] = _split(ask("Labels or folders you keep important mail in (optional): "))
    agent = ask("Will an AI agent (e.g. Claude Code) run these commands for you? [y/N]: ").strip().lower()
    answers["private"] = agent.startswith("y")
    return answers

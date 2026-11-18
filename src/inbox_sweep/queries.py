"""Gmail search queries for browser mode, generated from the same sweep.toml.

Browser mode can't read headers in bulk, so it protects mail with Gmail searches
(tag every match with the keep label), then trashes everything without the label.
These queries encode the same rules as classify.py as closely as Gmail search allows.
"""

from __future__ import annotations

import re

from inbox_sweep.config import FOREVER, Config
from inbox_sweep.headers import PERSONAL_DOMAINS

MAX_QUERY_CHARS = 1400

DOCUMENT_TERMS = [
    "T4", "T4A", "T5", "T1", "W-2", "1099", "P60", '"tax return"', '"tax slip"', '"tax form"',
    '"tax document"', '"notice of assessment"', "e-visa", '"visa application"', '"work permit"',
    '"study permit"', '"permanent resident"', "passport", "immigration", "citizenship",
    '"offer letter"', '"offer of employment"', '"job offer"', '"employment agreement"',
    '"employment contract"', '"exit letter"', '"reference letter"', '"recommendation letter"',
    "transcript", "diploma", '"offer of admission"', '"admission decision"', '"insurance policy"',
    '"lease agreement"', '"tenancy agreement"', "mortgage", "pension", "RRSP", "401k",
    '"class action"', '"settlement claim"', '"consumer claim"', "payslip", '"pay slip"', '"pay stub"',
    '"vaccine certificate"', '"vaccination record"',
]
SIGN_SENDERS = ["docusign.net", "docusign.com", "hellosign.com", "dropboxsign.com", "adobesign.com",
                "echosign.com", "eversign.com", "pandadoc.net", "signnow.com"]
NOREPLY_TERMS = ["noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification",
                 "mailer-daemon", "newsletter", "marketing"]


def label_query_name(label: str) -> str:
    """Gmail's search form of a label name: lower case, spaces and slashes as dashes."""
    return re.sub(r"[\s/]+", "-", label.strip().lower())


def term(value: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        return value
    return f'"{value}"' if re.search(r'[\s()"]', value) else value


def or_groups(operator: str, values: list[str], suffix: str = "", max_chars: int = MAX_QUERY_CHARS) -> list[str]:
    """`op:(a OR b ...) suffix`, split into several queries if one would be too long."""
    queries, chunk = [], []
    for v in [term(v) for v in values if v.strip()]:
        candidate = chunk + [v]
        text = f"{operator}:({' OR '.join(candidate)}){suffix}" if operator else f"({' OR '.join(candidate)}){suffix}"
        if len(text) > max_chars and chunk:
            queries.append(f"{operator}:({' OR '.join(chunk)}){suffix}" if operator else f"({' OR '.join(chunk)}){suffix}")
            chunk = [v]
        else:
            chunk = candidate
    if chunk:
        queries.append(f"{operator}:({' OR '.join(chunk)}){suffix}" if operator else f"({' OR '.join(chunk)}){suffix}")
    return queries


def build_queries(cfg: Config) -> dict:
    label = cfg.keep_label
    lq = label_query_name(label)
    not_kept = f" -label:{lq}"
    never = f" -from:({' OR '.join(term(s) for s in cfg.never_protect)})" if cfg.never_protect else ""
    protect: list[dict] = []
    notes: list[str] = []

    def add(name: str, queries: list[str], why: str) -> None:
        for i, q in enumerate(queries, 1):
            protect.append({"name": name if len(queries) == 1 else f"{name} ({i})", "query": q, "why": why})

    if cfg.me:
        # Never use from:me: Gmail also matches any ".me" domain.
        add("threads you sent or replied in", or_groups("from", cfg.me, " -subject:unsubscribe" + not_kept),
            "Labelling is per conversation, so whole threads you took part in are kept.")
    else:
        notes.append("Set [account] me in sweep.toml: without it, threads you replied in can't be protected.")

    scoped = []
    if cfg.protect_starred:
        scoped.append("is:starred")
    scoped += [f"label:{label_query_name(l)}" for l in cfg.protect_labels]
    scoped.append("in:chats")
    add("starred, your labels and chats", [f"{{{' '.join(scoped)}}}{not_kept}"], "Things you deliberately marked.")

    emails = [p for p in cfg.protect_people if "@" in p]
    names = [p for p in cfg.protect_people if "@" not in p]
    if emails:
        add("protected people (from)", or_groups("from", emails, not_kept), "Never trashed.")
        add("protected people (to)", or_groups("to", emails, not_kept), "Mail you sent them.")
    if names:
        add("protected names", or_groups("from", names, never + not_kept), "Display names, e.g. family surnames.")
    if cfg.protect_domains:
        add("people at protected domains", or_groups("from", cfg.protect_domains, " -unsubscribe" + never + not_kept),
            "Employers and schools: people there, not their mailing lists.")
    personal = sorted(set(PERSONAL_DOMAINS) | set(cfg.personal_domains))
    add("people on personal addresses",
        or_groups("from", personal, f" -unsubscribe -from:({' OR '.join(NOREPLY_TERMS)})" + never + not_kept),
        "Friends and family on webmail. Scan these senders before labelling: spam also uses webmail.")
    if cfg.protect_keywords:
        add("protected keywords", or_groups("subject", cfg.protect_keywords, " -unsubscribe -from:linkedin.com" + not_kept),
            "Subjects you said are sentimental. LinkedIn is excluded because its alerts quote profile text.")

    if cfg.retention.get("documents", FOREVER) == FOREVER:
        add("documents", or_groups("subject", DOCUMENT_TERMS, " -category:promotions" + not_kept),
            "Tax, government, visas, contracts, offers, transcripts.")
        add("signed documents", or_groups("from", SIGN_SENDERS, not_kept), "Completed e-signatures.")

    def windowed(category: str, terms: list[str], why: str) -> None:
        days = cfg.retention.get(category, 0)
        if days == FOREVER:
            prefix = ""
        elif days > 0:
            prefix = f"newer_than:{days}d "
        else:
            return
        add(category.replace("_", " "), [prefix + q for q in or_groups("subject", terms, " -category:promotions" + not_kept)], why)

    windowed("person_payment", ['"e-transfer"', '"sent you money"', '"paid you"', '"has sent you"', '"money transfer"'],
             "Money from people, kept for the retention window.")
    windowed("job_process", ["interview", '"next steps"', '"take home"', '"take-home"', "assessment"],
             "Live job processes.")
    windowed("receipt", ["receipt", "invoice", '"order confirmation"', "refund"], "Receipts within the window.")
    windowed("booking", ["itinerary", '"e-ticket"', "booking", "reservation", '"boarding pass"'], "Recent travel.")

    if cfg.recent_days > 0:
        add("recent working set",
            [f"newer_than:{cfg.recent_days}d -category:promotions -category:social -unsubscribe{not_kept}"],
            "Leave the last few weeks alone.")

    trash = f"-label:{lq} -in:chats -in:drafts"
    verify = [f"in:trash label:{lq}"]
    if cfg.me:
        verify += ["in:trash " + q for q in or_groups("from", cfg.me, " -subject:unsubscribe")]
    verify.append("in:trash is:starred")
    if emails:
        verify += ["in:trash " + q for q in or_groups("from", emails)]
    if cfg.protect_domains:
        verify += ["in:trash " + q for q in or_groups("from", cfg.protect_domains, " -unsubscribe")]
    return {"label": label, "label_query": lq, "protect": protect, "trash": trash, "verify": verify, "notes": notes}

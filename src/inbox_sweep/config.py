"""sweep.toml loading. Every setting has a safe default."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FOREVER = -1

# Days to keep each category before it becomes eligible for Trash.
# -1 keeps forever, 0 trashes regardless of age.
DEFAULT_RETENTION = {
    "documents": FOREVER,       # tax, government, visas, contracts, offers, transcripts
    "person_payment": 1095,     # e-transfers, "X paid you"
    "job_process": 365,         # interviews, assessments, next steps
    "receipt": 1095,            # receipts, invoices, order confirmations
    "booking": 365,             # flights, hotels, tickets
    "security": 30,             # codes, sign-in alerts, password resets
    "reminder": 30,             # bills due, renewals, appointments
    "account_notice": 30,       # "statement is ready", terms updates
    "job_automated": 0,         # application received, rejections, job alerts
    "newsletter": 0,
    "promotion": 0,
    "social": 0,
    "spam_like": 0,             # "You won!" scams that slipped past the spam filter
    "cold_outreach": 365,       # sales and recruiting pitches from strangers
    "other_automated": 90,
    "human_unknown": FOREVER,   # people you never wrote to: shown for review, kept by default
}

CONFIG_TEMPLATE = """\
# inbox-sweep configuration. Keep this file private: it names the people you protect.
# The test every rule serves: "Would I ever need this in 20 years, and would it
# upset me if I needed it and couldn't find it?"

[account]
# Every address you send from. Used to find threads you replied in and people you've written to.
me = ["you@example.com"]

[backend]
type = "gmail"          # gmail | outlook | imap | demo
credentials = "~/.config/inbox-sweep/credentials.json"   # Gmail OAuth client (see docs/gmail-api-setup.md)
token = "~/.config/inbox-sweep/token.json"

[gmail]
# Google cut API quotas for projects created on or after 1 May 2026 ("2026").
# Older projects keep higher limits ("legacy"). Indexing a big inbox is much faster
# over IMAP; trashing is fast either way.
quota = "2026"
fast_trash = true       # 1,000 messages per call, verified on the first batch

[outlook]
# Outlook.com / Hotmail / Microsoft 365 via Microsoft Graph (see docs/outlook-setup.md)
client_id = ""          # your Entra app registration's Application (client) ID
authority = "common"    # "common", "consumers" (personal accounts only) or your tenant id
token = "~/.config/inbox-sweep/outlook-token.json"

[imap]
host = "imap.gmail.com"
port = 993
user = "you@example.com"
tls = "ssl"             # "ssl", "starttls", or "none" (only to this machine, e.g. a local bridge)
verify = true           # false only for a local bridge with a self-signed certificate
ca_file = ""            # extra CA certificate, e.g. Proton Mail Bridge's exported cert
# Password is read from INBOX_SWEEP_IMAP_PASSWORD, or prompted. Use an app password.

[privacy]
# true: the CLI prints only counts, never senders or subjects. Use this when an AI
# agent runs the commands, so no mail data reaches its provider. Same as --private.
private = false

[protect]
# Never trashed, whatever their content.
people = []          # exact addresses ("mom@example.com") or display-name words ("Smith")
domains = []         # employers' and schools' domains (people there, not their marketing)
keywords = []        # subject words that make mail sentimental to you, e.g. a school's name
labels = []          # your own labels to protect, e.g. ["Taxes", "Family"]
personal_domains = []  # extra webmail domains (your ISP, a regional provider)
starred = true
replied_threads = true   # any thread you ever replied in
correspondents = true    # anyone you have ever emailed (bulk mail from them still goes)
recent_days = 30         # leave the last N days alone as your working set

[never_protect]
# Senders that look protected but aren't, e.g. an employer's newsletter address.
senders = []

[retention]
# Days to keep each category. -1 = forever, 0 = trash now.
documents = -1
person_payment = 1095
job_process = 365
receipt = 1095
booking = 365
security = 30
reminder = 30
account_notice = 30
job_automated = 0
newsletter = 0
promotion = 0
social = 0
spam_like = 0
cold_outreach = 365
other_automated = 90
human_unknown = -1

[ai]
# Optional: ask a language model about senders the rules can't place.
# Only sender names, addresses, counts and subject lines are used. Never bodies.
#   provider = "local"             any OpenAI-compatible server on this machine (Ollama,
#                                  LM Studio, llama.cpp, vLLM). Refuses non-local URLs.
#   provider = "openai-compatible" the same protocol at any URL: data goes to that provider
#   provider = "anthropic"         Claude (pip install 'inbox-sweep[ai]'): data goes to Anthropic
enabled = false
provider = "local"
model = ""              # blank = provider default (local: llama3.1:8b, anthropic: claude-opus-5-5)
base_url = "http://localhost:11434/v1"   # Ollama's default; LM Studio uses http://localhost:1234/v1
api_key_env = ""        # name of an environment variable holding the key, if the server needs one
batch_size = 40
min_messages = 1

[apply]
keep_label = "Keep"     # added to everything kept, to audit in your mail app. It does not
                        # protect mail on later runs: star it or use [protect] labels
batch_size = 50
max_index_age_days = 7  # refuse plans built from an older index (0 = no limit)

[paths]
data_dir = ".inbox-sweep"
"""


@dataclass
class Config:
    me: list[str] = field(default_factory=list)
    backend: str = "gmail"
    credentials: str = "~/.config/inbox-sweep/credentials.json"
    token: str = "~/.config/inbox-sweep/token.json"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_tls: str = "ssl"
    imap_verify: bool = True
    imap_ca_file: str = ""
    gmail_quota: str = "2026"
    gmail_fast_trash: bool = True
    outlook_client_id: str = ""
    outlook_authority: str = "common"
    outlook_token: str = "~/.config/inbox-sweep/outlook-token.json"
    private: bool = False
    protect_people: list[str] = field(default_factory=list)
    protect_domains: list[str] = field(default_factory=list)
    protect_keywords: list[str] = field(default_factory=list)
    protect_labels: list[str] = field(default_factory=list)
    personal_domains: list[str] = field(default_factory=list)
    protect_starred: bool = True
    protect_replied_threads: bool = True
    protect_correspondents: bool = True
    recent_days: int = 30
    never_protect: list[str] = field(default_factory=list)
    retention: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RETENTION))
    ai_enabled: bool = False
    ai_provider: str = "local"
    ai_model: str = ""
    ai_base_url: str = "http://localhost:11434/v1"
    ai_api_key_env: str = ""
    ai_batch_size: int = 40
    ai_min_messages: int = 1
    keep_label: str = "Keep"
    apply_batch_size: int = 50
    max_index_age_days: int = 7
    data_dir: str = ".inbox-sweep"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_path / "index.db"


class ConfigError(ValueError):
    pass


def _list(section: dict, key: str) -> list[str]:
    value = section.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def _bool(section: dict, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be true or false (no quotes)")
    return value


BACKENDS = ("gmail", "outlook", "imap", "demo")


def load_config(path: str | Path | None) -> Config:
    """Load sweep.toml. A missing file gives the defaults (useful for --backend demo)."""
    cfg = Config()
    if path is None:
        return cfg
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"{path} not found. Run `inbox-sweep init` to create one.")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc
    try:
        _read(cfg, data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"{path}: a setting has the wrong type ({exc})") from exc
    return cfg


def _read(cfg: Config, data: dict) -> None:
    account = data.get("account", {})
    cfg.me = [m.lower() for m in _list(account, "me")]

    backend = data.get("backend", {})
    cfg.backend = str(backend.get("type", cfg.backend)).lower()
    if cfg.backend not in BACKENDS:
        raise ConfigError(f"backend.type must be one of {', '.join(BACKENDS)}")
    cfg.credentials = backend.get("credentials", cfg.credentials)
    cfg.token = backend.get("token", cfg.token)

    imap = data.get("imap", {})
    cfg.imap_host = imap.get("host", cfg.imap_host)
    cfg.imap_port = int(imap.get("port", cfg.imap_port))
    cfg.imap_user = imap.get("user", cfg.imap_user)
    cfg.imap_tls = imap.get("tls", cfg.imap_tls)
    if cfg.imap_tls not in ("ssl", "starttls", "none"):
        raise ConfigError("imap.tls must be 'ssl', 'starttls' or 'none'")
    cfg.imap_verify = _bool(imap, "verify", True)
    cfg.imap_ca_file = imap.get("ca_file", cfg.imap_ca_file)

    gmail = data.get("gmail", {})
    cfg.gmail_quota = gmail.get("quota", cfg.gmail_quota)
    if cfg.gmail_quota not in ("2026", "legacy"):
        raise ConfigError("gmail.quota must be '2026' or 'legacy'")
    cfg.gmail_fast_trash = _bool(gmail, "fast_trash", True)

    outlook = data.get("outlook", {})
    cfg.outlook_client_id = outlook.get("client_id", cfg.outlook_client_id).strip()
    cfg.outlook_authority = outlook.get("authority", cfg.outlook_authority).strip() or "common"
    cfg.outlook_token = outlook.get("token", cfg.outlook_token)

    cfg.private = _bool(data.get("privacy", {}), "private", False)

    protect = data.get("protect", {})
    cfg.protect_people = _list(protect, "people")
    cfg.protect_domains = _list(protect, "domains")
    cfg.protect_keywords = _list(protect, "keywords")
    cfg.protect_labels = _list(protect, "labels")
    cfg.personal_domains = [d.lower() for d in _list(protect, "personal_domains")]
    cfg.protect_starred = _bool(protect, "starred", True)
    cfg.protect_replied_threads = _bool(protect, "replied_threads", True)
    cfg.protect_correspondents = _bool(protect, "correspondents", True)
    cfg.recent_days = int(protect.get("recent_days", 30))

    cfg.never_protect = [s.lower() for s in _list(data.get("never_protect", {}), "senders")]

    retention = data.get("retention", {})
    unknown = set(retention) - set(DEFAULT_RETENTION)
    if unknown:
        raise ConfigError(f"Unknown retention categories: {', '.join(sorted(unknown))}")
    for key, value in retention.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < FOREVER:
            raise ConfigError(f"retention.{key} must be an integer >= -1")
        cfg.retention[key] = value

    ai = data.get("ai", {})
    cfg.ai_enabled = _bool(ai, "enabled", False)
    cfg.ai_provider = ai.get("provider", cfg.ai_provider)
    if cfg.ai_provider not in ("local", "openai-compatible", "anthropic"):
        raise ConfigError("ai.provider must be 'local', 'openai-compatible' or 'anthropic'")
    cfg.ai_model = ai.get("model", cfg.ai_model)
    cfg.ai_base_url = ai.get("base_url", cfg.ai_base_url)
    cfg.ai_api_key_env = ai.get("api_key_env", cfg.ai_api_key_env)
    cfg.ai_batch_size = int(ai.get("batch_size", cfg.ai_batch_size))
    cfg.ai_min_messages = int(ai.get("min_messages", cfg.ai_min_messages))

    apply = data.get("apply", {})
    cfg.keep_label = apply.get("keep_label", cfg.keep_label)
    cfg.apply_batch_size = int(apply.get("batch_size", cfg.apply_batch_size))
    if cfg.apply_batch_size < 1:
        raise ConfigError("apply.batch_size must be at least 1")
    cfg.max_index_age_days = int(apply.get("max_index_age_days", cfg.max_index_age_days))

    cfg.data_dir = data.get("paths", {}).get("data_dir", cfg.data_dir)

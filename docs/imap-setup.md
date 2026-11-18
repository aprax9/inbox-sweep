# IMAP setup

IMAP works with most providers. You need the server name and an **app password**,
a separate password for one app, which you can revoke without changing your real one.

`inbox-sweep init` fills this in for common providers:

```toml
[backend]
type = "imap"

[imap]
host = "imap.gmail.com"
port = 993
tls = "ssl"          # "ssl" (port 993), "starttls" (port 143), or "none"
user = "you@gmail.com"
verify = true        # check the server's certificate
ca_file = ""         # a CA bundle, for a self-signed server you trust
```

Plaintext (`tls = "none"`) and unverified TLS (`verify = false`) are refused
unless the server is on this machine, which is what local bridges need.

The password comes from the `INBOX_SWEEP_IMAP_PASSWORD` environment variable, or
inbox-sweep prompts for it. It is never written to disk.

| Provider | Host | App password |
|---|---|---|
| Gmail | `imap.gmail.com` | Turn on 2-Step Verification, then <https://myaccount.google.com/apppasswords> |
| iCloud | `imap.mail.me.com` | <https://account.apple.com>, Sign-In and Security, App-Specific Passwords |
| Fastmail | `imap.fastmail.com` | Settings, Privacy & Security, App passwords |
| Yahoo | `imap.mail.yahoo.com` | Account security, Generate app password |
| Proton Mail | `127.0.0.1`, port `1143`, `tls = "starttls"`, `verify = false` | Run [Proton Mail Bridge](https://proton.me/mail/bridge); it shows the IMAP password to use |
| Outlook.com / Microsoft 365 | Use the `outlook` backend instead: Microsoft requires OAuth ([setup](outlook-setup.md)) | |

## How IMAP mode behaves

- Only headers are read (`BODY.PEEK`), so nothing is marked as read.
- **Gmail over IMAP:** scans "All Mail" and uses Gmail's own thread ids and labels.
  `index --query` accepts Gmail search syntax. Applying adds the Keep label and
  moves threads to `[Gmail]/Trash`. Undo moves them back to the Inbox.
- **Other servers:** scans every folder except Trash, Junk and Drafts. Folder
  names act as labels for `[protect] labels`, and threads are rebuilt from
  `References` headers. Trash is the folder flagged `\Trash`. The server must
  support `MOVE` or `UIDPLUS`, so inbox-sweep never expunges anything except the
  messages it moved. Undo finds each message in Trash by its `Message-ID` and
  returns it to its original folder. Servers without labels skip the Keep label.
- If a folder's `UIDVALIDITY` changes between `index` and `apply` (rare: the
  server renumbered it), `apply` stops and asks you to re-index.
- **Speed.** Re-indexing fetches only flags for known mail and headers for new
  mail. There is no per-message quota, so Gmail over IMAP is the fastest way to
  index a very large Gmail inbox.
- On servers other than Gmail, a message moved to Trash and back gets a new id,
  so the `index` after an `undo` re-reads those messages.

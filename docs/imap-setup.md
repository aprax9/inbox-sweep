# IMAP setup

IMAP works with most providers. You need the server name and an **app password**,
a separate password for one app, which you can revoke without changing your real one.

```toml
[backend]
type = "imap"

[imap]
host = "imap.gmail.com"
port = 993
user = "you@gmail.com"
```

The password comes from the `INBOX_SWEEP_IMAP_PASSWORD` environment variable, or
inbox-sweep prompts for it. It is never written to disk.

| Provider | Host | App password |
|---|---|---|
| Gmail | `imap.gmail.com` | Turn on 2-Step Verification, then <https://myaccount.google.com/apppasswords> |
| iCloud | `imap.mail.me.com` | <https://account.apple.com>, Sign-In and Security, App-Specific Passwords |
| Fastmail | `imap.fastmail.com` | Settings, Privacy & Security, App passwords |
| Yahoo | `imap.mail.yahoo.com` | Account security, Generate app password |
| Outlook.com / Microsoft 365 | Not supported yet: Microsoft requires OAuth for IMAP | |

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

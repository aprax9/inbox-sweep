# Security and privacy

## What stays where

- **Your machine:** the header index (`.inbox-sweep/index.db`), plans, logs, your
  `sweep.toml`, and the Gmail OAuth token (`~/.config/inbox-sweep/token.json`,
  mode `0600`). None of these should be committed. `.gitignore` excludes them.
- **Never fetched:** message bodies and attachments.
- **Sent to Anthropic, only with `classify --ai`:** sender names and addresses,
  message counts, date ranges and up to six subject lines per sender, for senders
  the rules couldn't place.
- **Sent to senders, only with `unsubscribe --yes`:** one HTTPS POST per sender
  to the one-click URL from their own `List-Unsubscribe` header.

## Permissions

- Gmail API: `gmail.modify` only. It can label and move to Trash. It cannot delete
  permanently or change settings.
- IMAP: an app password you can revoke. inbox-sweep moves messages to the Trash
  folder and refuses servers where that would need a blanket `EXPUNGE`.

## Untrusted input

Email headers are written by strangers:
- The review page embeds them as JSON and renders them with `textContent`.
- The AI prompt marks them as untrusted data, and the output is schema-constrained.
- Unsubscribe URLs must be HTTPS public hostnames (no IP literals, `localhost`
  or credentials), including on redirect.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security, Report a
vulnerability) rather than a public issue.

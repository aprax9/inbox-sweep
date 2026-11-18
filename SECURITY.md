# Security and privacy

## What stays where

- **Your machine:** the header index (`.inbox-sweep/index.db`), plans, logs, your
  `sweep.toml`, and OAuth tokens (`~/.config/inbox-sweep/token.json` for Gmail,
  `outlook-token.json` for Outlook, mode `0600`). None of these should be
  committed. `.gitignore` excludes them.
- **Never requested or stored:** message bodies, body previews and attachments.
- **AI providers get nothing by default.** Every decision is made by local rules.
  The optional AI step (`classify --ai`) defaults to `provider = "local"`, which
  refuses any URL that isn't on this machine. Only with `openai-compatible` or
  `anthropic`, which you must set explicitly, do sender names and addresses,
  counts, date ranges and up to six subject lines per sender leave your machine.
  The CLI prints where data goes before it calls a model.
- **AI agents running the CLI** see everything it prints. `--private` (or
  `[privacy] private = true`) limits output to counts and categories.
- **Sent to senders, only with `unsubscribe --yes`:** one HTTPS POST per sender
  to the one-click URL from their own `List-Unsubscribe` header.

## Permissions

- Gmail API: `gmail.modify` only. It can label and move to Trash. It cannot delete
  permanently or change settings.
- Outlook (Microsoft Graph): delegated `Mail.ReadWrite` and `User.Read`, on your
  own app registration. Trash means moving to Deleted Items. The permanent-delete
  endpoints are never called.
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

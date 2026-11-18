# inbox-sweep

Clean a decades-old inbox down to the mail you'd actually want in 20 years.

> **The test:** Would I ever need to come back to this email in 20 years, and if I
> needed it but couldn't find it, could that plausibly upset me?

inbox-sweep keeps:
- your family, friends and colleagues, and every thread you replied in;
- tax, legal, government, visa, education and employment records;
- money from people (for 3 years by default);
- anything you starred or filed.

It moves the rest to **Trash** (never deleted outright): newsletters,
promotions, notifications, security codes, old receipts and bookings, job-board
mail, and the 2014 alumni fundraising emails. Then it offers one-click
unsubscribes.

It runs two ways with the same rules:

| | **API mode** | **Browser mode** |
|---|---|---|
| How | CLI reads headers via the Gmail API or IMAP, decides locally, applies a frozen plan | An AI agent (or you) drives Gmail's web UI with searches and bulk actions |
| Setup | ~5 min: your own Google OAuth client, or an IMAP app password | None, just be signed in to Gmail |
| Speed | 20,000 emails indexed in minutes; decisions in seconds | A 5,000-thread trash takes ~15–20 min; labelling is one bulk job at a time |
| Providers | Gmail, Fastmail, iCloud, Yahoo, most IMAP hosts | Gmail |

## Try it in 30 seconds (fake mailbox, no account)

```bash
uvx --from "git+https://github.com/aprax9/inbox-sweep" inbox-sweep demo --open
```

This builds 15 years of synthetic mail, classifies it, opens the per-sender
review page and writes a plan. Then try `apply`, and `undo`.

## Why it's built this way

It came out of cleaning a real 20-year Gmail inbox by hand-driving the web
interface. That took three rounds and hours. These choices fix what went wrong:

- **Decide locally, act once.** Headers go into a local SQLite file, so changing
  a rule costs seconds, not another hour of Gmail searches.
- **Protect people, not institutions.** Hand-written domain lists missed a school
  that emailed from an unexpected domain, and whitelisting whole banks or
  universities kept thousands of junk emails. Instead, anyone you've ever
  written to is protected automatically. A protected domain protects *people*
  there, not its mailing lists.
- **Review per sender.** A few hundred senders account for nearly all the mail.
  One page, one toggle per sender, one pass.
- **Freeze, then apply.** The plan is a fixed list of thread ids, re-checked
  against your protections before anything moves. Applying logs every batch, so
  an interrupted run resumes and `undo` restores exactly what moved.

## Install

Python 3.10+.

```bash
uv tool install "inbox-sweep[gmail] @ git+https://github.com/aprax9/inbox-sweep"
# or: pipx install "inbox-sweep[gmail] @ git+https://github.com/aprax9/inbox-sweep"
```

Extras: `gmail` (Gmail API), `ai` (optional Claude step), `all`.

## API mode

```bash
inbox-sweep init                  # writes sweep.toml. Fill in who you are and who's protected
inbox-sweep auth                  # Gmail only: one-time browser sign-in
inbox-sweep index                 # headers only; nothing is marked read
inbox-sweep classify              # decisions, in seconds
inbox-sweep review --open         # one row per sender: Follow rules / Keep all / Trash all
inbox-sweep classify --overrides ~/Downloads/decisions.json
inbox-sweep plan                  # refuses any plan that touches protected mail
inbox-sweep apply .inbox-sweep/plan.json         # dry run
inbox-sweep apply .inbox-sweep/plan.json --yes   # label kept mail "Keep", move the rest to Trash
inbox-sweep unsubscribe           # dry run of one-click unsubscribes; add --yes to send them
```

Setup guides: [Gmail API](docs/gmail-api-setup.md) · [IMAP](docs/imap-setup.md).

Changed your mind? `inbox-sweep undo .inbox-sweep/trash-log-<id>.jsonl --yes`.
Interrupted? Run the same `apply` again: it picks up where it stopped.

## Browser mode

**With Claude Code** (recommended): install the plugin and ask Claude to clean
your inbox. It interviews you, then either runs API mode or drives Gmail in a
browser it controls, following the [playbook](skills/inbox-sweep/browser/PLAYBOOK.md).

```
/plugin marketplace add aprax9/inbox-sweep
/plugin install inbox-sweep@inbox-sweep
```

**By hand:** `inbox-sweep queries` prints, from your `sweep.toml`:
- the Gmail searches whose results you label `Keep`, one by one;
- the single search to move everything else to Trash;
- the searches that verify nothing protected went with it.

[`gmail-helpers.js`](skills/inbox-sweep/browser/gmail-helpers.js) adds safety
checks and sender scans you can run in the Gmail tab.

## What's kept by default

Protection comes first and beats everything else.

| Always kept | |
|---|---|
| Threads you replied in, mail you sent | the whole conversation |
| Starred mail, and labels you list | |
| Protected people | addresses or names in `[protect] people` |
| People you've written to | their personal mail; their newsletters can still go |
| People on personal addresses | webmail senders that aren't bulk mail |
| People at protected domains | employers and schools: people, not mailing lists |
| The last 30 days | your working set |

Then each category is kept for its retention period (edit in `sweep.toml`):

| Category | Default | Examples |
|---|---|---|
| documents | forever | tax slips, notices of assessment, visas, offer letters, transcripts, leases, pensions, signed documents |
| person_payment | 3 years | e-transfers, "X paid you" |
| receipt | 3 years | receipts, invoices, orders |
| job_process | 1 year | interviews, assessments, next steps |
| booking | 1 year | flights, hotels, tickets |
| other_automated | 90 days | everything else machine-sent |
| security, reminder, account_notice | 30 days | codes, sign-in alerts, bills due, "statement ready" |
| newsletter, promotion, social, job_automated | trash | mailing lists, sales, notifications, application receipts and rejections |
| human_unknown | forever | a person you never wrote to: kept, and flagged on the review page |

[How it decides](docs/how-it-decides.md) has the full precedence rules.

## Safety

- **Trash only.** Nothing is deleted permanently. Gmail keeps Trash for 30 days.
- **Dry run by default.** `apply` and `unsubscribe` do nothing without `--yes`.
- **Plans are checked twice:** when written, and again before applying, against
  protections recomputed from scratch. A plan made for one account won't apply to another.
- **Local.** Headers live in `.inbox-sweep/index.db` on your machine. Bodies are
  never fetched. OAuth tokens are stored `0600`. The Gmail scope is
  `gmail.modify`: it can label and trash, but can't delete permanently.
- **Unsubscribing** is a single HTTPS POST to the sender's RFC 8058 one-click URL.
  No links in emails are opened, no email is sent, and private or local addresses
  are refused.
- **The optional AI step** (`classify --ai`, Claude) sees only sender names,
  addresses, counts, date ranges and subject lines for senders the rules can't
  place. Its verdicts can't override any protection, and appear on the review page.

## Limitations

- Gmail's web DOM isn't an API. Browser-mode helpers may need selector updates when Gmail changes.
- Gmail OAuth clients in "Testing" mode need re-auth every 7 days (`inbox-sweep auth`).
- Undo over IMAP on Gmail returns threads to the Inbox (like Gmail's "Move to
  Inbox"). Other IMAP servers get messages back in their original folder. API
  mode restores exactly.
- Outlook.com and Microsoft 365 require OAuth for IMAP, which isn't supported yet.
  A Microsoft Graph backend would be a welcome contribution
  (see [CONTRIBUTING](CONTRIBUTING.md)).
- Categories are English-centric. Patterns live in `src/inbox_sweep/categories.py`.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

MIT licensed.

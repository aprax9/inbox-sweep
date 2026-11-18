# Changelog

## 0.3.0

Speed, accuracy and safety, checked against real protocols.

**Faster**
- `index` is incremental: later runs read headers only for new mail and refresh
  labels and flags cheaply (Gmail lists label membership, IMAP fetches flags only).
- Gmail: paced to Google's 2026 per-user quota (6,000 units/min, 20 per message
  read) so it never trips rate limits, and trashing/labelling uses
  `messages.batchModify` (1,000 messages per call). Drafts and chats are skipped
  before reading. `[gmail] quota = "legacy"` for older Google Cloud projects.
- Outlook: 1,000 messages per page; batches paced per sub-request, as Graph counts them.
- Local pipeline: 100,000 messages in ~6 s (`scripts/benchmark.py`, gated in CI).

**More accurate**
- Government senders in many countries count as documents.
- Tax, payslip and visa terms in French, German, Spanish, Dutch, Nordic
  languages, Chinese and Japanese.
- New categories: `spam_like` (trash) and `cold_outreach` (1 year).
- No-reply detection works on whole address tokens, in several languages.
- `inbox-sweep eval` measures the rules against mail you already sorted (a
  label, the review page's decisions.json, or a hand-labelled sample), and
  against a built-in labeled corpus: 0 false trash, 97% agreement (the misses are
  kept junk, by design).

**Safer**
- `apply` re-checks the live mailbox and skips threads starred, replied to or
  filed under a protected label after indexing.
- Only planned message ids move, never whole threads, so a reply that arrives in
  a planned thread later stays put.
- Crash-safe undo log (each batch logged before and after it's sent); `undo`
  reverses a run that died halfway.
- One run per data folder (lock file; a lock held by a running process is never taken over).
- Plans from an index over 7 days old, or older than the last `index`, are refused.
- The index records its schema version; a newer index is refused, not misread.
- Plaintext or unverified IMAP only to this machine (local bridges). `[imap] tls`,
  `verify` and `ca_file` settings.
- The Keep label is an audit trail, not a protection, so later runs can apply
  retention to mail an earlier run kept.

**Easier to set up**
- `inbox-sweep init` asks a few questions (or takes flags) and writes a working
  `sweep.toml`, with IMAP settings for common providers, Proton Bridge included.
- `inbox-sweep doctor` checks everything a run needs and says how to fix it.
- Clear errors for malformed config files instead of tracebacks.
- Works on Windows: UTF-8 file handling throughout, console output that can't
  crash on unusual characters, Windows-aware lock checks.

**Fixed after an independent review** (each has a regression test)
- Gmail over IMAP: after Gmail renumbered All Mail, apply could move the wrong
  message. Messages are now resolved by Gmail's permanent id when applying, and
  every IMAP move is preceded by an identity check.
- IMAP undo is exact: the Trash UID of every moved message is logged (COPYUID),
  with a Message-ID search only as a fallback. Messages without a Message-ID, or
  sharing one, now come back, and undo no longer rescans Trash for every batch.
- Non-ASCII folder and label names (modified UTF-7) match `[protect] labels`,
  in classify and in the live check; Gmail searches with non-ASCII terms work.
- Protected labels cover nested folders ("Family/Photos", "Inbox/Family",
  "INBOX.Family"); Outlook folders are read up to 16 levels deep.
- A subject that looks like spam ("You won't believe…") no longer removes the
  protection for a person on a personal address, and Gmail's Updates/Promotions
  tabs no longer count as machine-sent for it.
- An `index` that stopped early (or used `--query`/`--limit`) no longer lets
  classify, plan or apply run on a partial index; what it read is kept.
- `verify_plan` checks the folder, UID, UIDVALIDITY and Message-ID a backend will
  act on, not just the message id.
- IMAP: response lines over 1 MB (searches over ~125k messages), raw 8-bit
  headers, a FETCH parser fooled by a label named like "UID 99", Courier-style
  folders (`INBOX.Trash`), and dropped connections are all handled.
- Outlook: batches of 4 to respect the 4-concurrent-requests-per-mailbox limit;
  network errors are retried. Gmail: network errors are retried.
- The undo log is fsync'd; undo records what it restored (re-applying works, a
  second undo is a no-op); `undo --all` covers every run; the keep label isn't
  redone when resuming; the log path is printed before anything moves.
- `[protect] people` accepts `Name <address>` and rejects entries that would
  protect nothing (like `@domain.com`).
- Network, database and encoding errors print a clear message instead of a traceback.

**Tested**
- In-memory fakes of the Gmail API, IMAP and Microsoft Graph exercise paging,
  batching, throttling, quota pacing and MOVE semantics.
- Property-based tests (Hypothesis) check that, for random mailboxes, protected
  mail is never planned, threads are never split, and apply + undo restores the
  original state exactly.
- CI runs the whole pipeline against a real Dovecot IMAP server, and the test
  suite on Linux, macOS and Windows.

## 0.2.0

- Outlook / Microsoft 365 backend (Microsoft Graph).
- Private mode: counts only, for AI agents running the CLI.
- Optional AI step, local model by default.

## 0.1.0

- First release: Gmail API, IMAP and browser modes; per-sender review page;
  frozen, verified plans; undo.

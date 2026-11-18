# Release checklist

CI covers the code: unit and property tests on Linux, macOS and Windows; the
Gmail, IMAP and Graph backends against protocol fakes; the whole pipeline
against a real Dovecot IMAP server; the labeled-corpus accuracy gate; and the
100,000-message benchmark. What CI can't do is sign in to a real Gmail or Outlook
account. Before a release, run these by hand with **throwaway accounts**, never
your real mailbox.

## 1. Seed a test mailbox (10 minutes)

Make a new Gmail account and a new Outlook.com account. From another address,
send each one:

- a newsletter you subscribe it to (any real list with an unsubscribe header);
- a message from "family" (an address you put in `[protect] people`);
- a message you **reply to** from the test account;
- a message you **star** (Gmail) or **flag** (Outlook);
- a message filed under a label or folder you list in `[protect] labels`;
- a one-time code from any site that sends one (sign up with the test address).

Wait a day, or set `[protect] recent_days = 0` for the test.

## 2. Gmail API

```bash
inbox-sweep init --me test.address@gmail.com --protect family@example.org --force
inbox-sweep doctor                       # no ✗ lines (warnings are fine before auth)
inbox-sweep auth
inbox-sweep index && inbox-sweep index   # the second run: "0 new"
inbox-sweep classify && inbox-sweep review --open
inbox-sweep plan
```

Then star one of the planned messages in Gmail, and:

```bash
inbox-sweep apply .inbox-sweep/plan.json --yes   # expect "1 skipped because they're protected now"
```

Check in Gmail: only the newsletter and the code are in Trash; the reply,
starred, labelled and family threads are untouched; kept mail has the `Keep`
label. Then `inbox-sweep undo .inbox-sweep/trash-log-*.jsonl --yes` and check the
two messages are back with their original labels.

Repeat with `[gmail] fast_trash = false` to exercise the per-message fallback.

## 3. Outlook

Same as Gmail with `--provider outlook` and the setup in
[outlook-setup.md](outlook-setup.md). Check Deleted Items, and that undo returns
each message to its original folder.

## 4. Gmail over IMAP

Same mailbox, `[backend] type = "imap"`, `imap.gmail.com`, an app password.
Check that undo returns threads to the Inbox.

## 5. Browser mode

In the Gmail test account, follow [PLAYBOOK.md](../skills/inbox-sweep/browser/PLAYBOOK.md)
with `gmail-helpers.js`. If any selector fails, update it and note the date in
the file header.

## 6. Accuracy on real mail you already sorted

If you have cleaned a mailbox by hand, score the rules against your own
decisions before that mail's Trash empties (30 days on Gmail):

```bash
inbox-sweep index --include-trash --fresh
inbox-sweep --private eval --truth-label Keep     # your label for "worth keeping"
```

Look at false trash first. Every false trash is a bug: add the case to
`src/inbox_sweep/corpus.py` (with invented names and `.example` domains) and fix
the rule.

## 7. Publish

1. Bump the version in `pyproject.toml`, `src/inbox_sweep/__init__.py` and
   `.claude-plugin/plugin.json` (a test checks they agree), and add a
   `CHANGELOG.md` entry.
2. First release only: on PyPI, add a pending trusted publisher for owner
   `aprax9`, repository `inbox-sweep`, workflow `release.yml`, environment `pypi`,
   and create the `pypi` environment in the GitHub repository settings.
3. `git tag v0.3.0 && git push origin v0.3.0`. The release workflow tests,
   builds and publishes. No API token is stored anywhere.

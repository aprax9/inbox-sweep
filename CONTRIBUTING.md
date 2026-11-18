# Contributing

```bash
uv venv && uv pip install -e ".[dev]"
pytest
inbox-sweep demo --open   # see your change on the synthetic mailbox
```

## Ground rules

- **No real mail in fixtures, ever.** Use `.example` domains (RFC 2606) and
  invented names. The demo mailbox in `backends/demo.py` is the place to add cases.
- Anything that changes what gets trashed needs a test in `tests/test_classify.py`
  or `tests/test_headers_and_categories.py`, and a scenario in `corpus.py` if it
  fixes a real-world miss. `inbox-sweep eval --corpus --max-false-trash 0` must
  pass: CI enforces zero false trash.
- `tests/test_properties.py` checks the safety invariants on random mailboxes.
  If it finds a counterexample, fix the code, not the test.
- Keep trash-only semantics. No feature may delete permanently.

## Good first contributions

- **A new mail provider.** Implement `account`, `iter_messages`, `label_messages`,
  `trash` and `restore` from `backends/base.py`, plus `sync` (incremental index)
  and `protected_thread_ids` (the live check at apply time) if the provider can
  do them cheaply. Model it on `backends/outlook.py` (HTTP API) or
  `backends/imap.py`, and test it against a fake server as `tests/test_outlook.py`
  and `tests/fakes.py` do. JMAP (Fastmail's native API) is a good candidate.
- **Browser mode for Outlook on the web**, mirroring the Gmail playbook.
- **Category patterns for other languages** in `categories.py`, with tests.
- **Browser-mode selectors.** If Gmail changes its DOM, update `gmail-helpers.js`
  and note the date in the header comment.

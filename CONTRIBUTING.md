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
  or `tests/test_headers_and_categories.py`.
- Keep trash-only semantics. No feature may delete permanently.

## Good first contributions

- **A Microsoft Graph backend** (Outlook.com / Microsoft 365). Implement the five
  methods in `backends/base.py`. Model it on `gmail_api.py`.
- **A JMAP backend** (Fastmail's native API).
- **Category patterns for other languages** in `categories.py`, with tests.
- **Browser-mode selectors.** If Gmail changes its DOM, update `gmail-helpers.js`
  and note the date in the header comment.

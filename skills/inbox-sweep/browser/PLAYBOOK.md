# Browser mode playbook (Gmail web)

For an agent driving Gmail in a real browser tab: no OAuth client, no app
password, nothing to install. It is slower than API mode (Gmail runs one bulk
job at a time, and a 5,000-thread trash takes 15+ minutes), so prefer API mode
for large inboxes when the person is willing to do the 5-minute setup.

The strategy is **protect first, then trash the rest**:

1. Tag everything worth keeping with one label (`Keep` by default), one search at a time.
2. Move everything *without* that label to Trash: `-label:keep -in:chats -in:drafts`.
3. Search Trash for anything protected and restore it.

Deciding what's junk is never a search. You decide what to keep, and the rest goes.

## 0. Before touching anything

- Open `https://mail.google.com/mail/u/0/#inbox` in a tab you control. Confirm the
  signed-in address in the account menu matches the mailbox the person means.
  People often have several Google accounts, and connectors may point at a different one.
- Agree the rules with the person (SKILL.md, step 1) and get them into `sweep.toml`.
- If the CLI is installed, `inbox-sweep queries` (or `--json`) prints every search
  below, built from `sweep.toml`. Otherwise build them from the templates in section 3.
- Inject `gmail-helpers.js` (this folder) with your JavaScript tool. It defines
  `window.inboxSweep`. Re-inject after any full page load.

## 1. Settings that make bulk actions work

- Search results must be sorted **Most recent**, not Most relevant. Otherwise
  Gmail doesn't offer "Select all conversations that match this search".
  `inboxSweep.assertMostRecentSort()` throws if the sort is wrong.
- Conversation view on (Gmail's default). Labels then apply to whole threads.

## 2. Survey before deciding

```js
inboxSweep.startJob('all', () => inboxSweep.scan('-in:chats -in:drafts', 400))
// poll: inboxSweep.jobs.all.done, then inboxSweep.jobs.all.result.senders
```

Show the person the top senders with sample subjects and propose keep or trash.
Scan the personal-address query separately (`from:(gmail.com OR outlook.com ...)`):
spammers use webmail too, so add any you find to `[never_protect]` before labelling.
If your tool filters JS output, use `inboxSweep.dump(result)` and read the page text.

## 3. Protect pass: label the keepers

Typical searches, each ending in `-label:keep` so it shows only what's left to do:

| What | Search |
|---|---|
| Threads you took part in | `from:(you@gmail.com OR you@work.com) -subject:unsubscribe -label:keep` |
| Starred, your labels, chats | `{is:starred label:taxes label:family in:chats} -label:keep` |
| Protected people | `from:(mom@x.com OR dad@y.com) -label:keep` and the same with `to:` |
| Family names | `from:(Smith OR Garcia) -label:keep` |
| People at employers and schools | `from:(employer.com OR school.edu) -unsubscribe -label:keep` |
| People on personal addresses | `from:(gmail.com OR outlook.com ...) -unsubscribe -from:(noreply OR no-reply OR notifications) -label:keep` |
| Documents | `subject:("tax return" OR T4 OR W-2 OR "offer letter" OR visa OR transcript ...) -category:promotions -label:keep` |
| E-signatures | `from:(docusign.net OR hellosign.com OR adobesign.com ...) -label:keep` |
| Money from people (last 3 years) | `newer_than:1095d subject:("e-transfer" OR "sent you money" OR "paid you") -label:keep` |
| Live job processes (last year) | `newer_than:365d subject:(interview OR "next steps" OR assessment) -label:keep` |
| Recent working set | `newer_than:30d -category:promotions -category:social -unsubscribe -label:keep` |

For each search, one at a time:

1. `await inboxSweep.search(q)`. If the count is empty, move on.
2. Click the select-all checkbox.
3. More than 50 results: `inboxSweep.assertSelectAllBanner()`, click "Select all
   conversations that match this search", then `inboxSweep.assertAllSelected()`.
   50 or fewer: `inboxSweep.assertNoSelectAllBanner()` and skip the banner. **Never
   click where the banner would be when it isn't there**: you'll open an email.
4. Click the Labels button, then `inboxSweep.assertLabelMenuOpen()` **before
   typing**. If focus is anywhere else, typed letters fire Gmail shortcuts ("e"
   archives, "#" deletes).
5. Type the label name. The first time, choose "Create new" and confirm the dialog.
   Otherwise tick it, run `inboxSweep.assertLabelChecked('Keep')`, then click Apply.
6. Bulk selection: `inboxSweep.assertConfirmDialog()`, then OK.
7. Wait. Re-run the search until it's empty. Gmail runs **one bulk job at a time**,
   and starting another stalls the first. Big jobs often leave 1 to 10
   stragglers: select them on the page and label them directly.

`inboxSweep.locate('labelButton' | 'labelItem' | 'labelApply' | 'confirmOk' | ...)`
returns CSS-pixel centres. Multiply by `screenshotWidth / innerWidth` for your
click coordinates. Batch each step with its assertion, so a thrown error stops
the batch before the next click.

## 4. Check with the person

Report `label:keep` (total kept) and scan `-label:keep -in:chats -in:drafts` by
sender. Show what's about to go and get an explicit yes. Call out anything
from an employer or school domain, even marketing.

## 5. Trash pass

Search `-label:keep -in:chats -in:drafts`, select all, use the banner, then
`assertAllSelected()`, click Delete (`locate('deleteButton')`), then
`assertConfirmDialog()` and OK. Gmail works through it newest to oldest. Poll the
first row's date on the search page to watch progress. Expect about 5,000
threads in 15 to 20 minutes. Don't start another bulk action meanwhile.

## 6. Verify and repair

Run each check. Expected result: nothing, or only what you knowingly trashed.

```
in:trash label:keep
in:trash from:(you@gmail.com) -subject:unsubscribe
in:trash is:starred
in:trash from:(<protected people>)
in:trash from:(<employer and school domains>) -unsubscribe
in:trash {<school name> <family surname>} -from:linkedin.com
```

To restore: search the match in Trash, select, add the `Keep` label, then click
"Move to Inbox". Domains people forget are the common miss. Example: a school
emailing from a different domain than its name suggests. Searching Trash for
the institution's *name*, not just its domain, catches these.

## 7. Report

Tell the person:
- the before and after counts;
- what went, by kind, and what was kept;
- any judgment calls they might reverse;
- that everything is in Trash and recoverable for about 30 days (give the date).

## Pitfalls collected from real runs

- `from:me` also matches any `.me` domain. List your addresses explicitly.
- `{...}` groups that mix free text with `from:` are unreliable. Use `from:(a OR b)`.
- Free-text names of schools and employers match LinkedIn job alerts (they quote
  profile text). Add `-from:linkedin.com`.
- "1–50 of many" means Gmail won't count. Use `scan()` or Settings, Labels for counts.
- Labels are added per conversation, so a protected reply protects the whole thread.
- Deleting a label never deletes mail. It's safe for tidying up helper labels.
- Trash empties itself after 30 days, so do the verification while it's still recoverable.

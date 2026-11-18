"""The review page: one row per sender, so a 20-year inbox is a 15-minute skim.

The page is a single local HTML file with the data embedded. Nothing is uploaded.
Your choices download as decisions.json, which `inbox-sweep classify --overrides`
reads back.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from inbox_sweep.model import Decision, Message


def build_review_data(messages: list[Message], decisions: list[Decision],
                      overrides: dict[str, str], account: str = "") -> dict:
    by_id = {d.message_id: d for d in decisions}
    groups: dict[str, list[tuple[Message, Decision]]] = defaultdict(list)
    for m in messages:
        d = by_id.get(m.id)
        if d:
            groups[m.from_email].append((m, d))
    rows = []
    for sender, items in groups.items():
        items.sort(key=lambda md: md[0].date, reverse=True)
        keep = sum(d.action == "keep" for _, d in items)
        trash = len(items) - keep
        absolute = sum(d.protected == "absolute" for _, d in items)
        cats = Counter(d.category for _, d in items).most_common(2)
        reasons = Counter(d.reason for _, d in items).most_common(2)
        rows.append({
            "sender": sender,
            "name": items[0][0].from_name,
            "count": len(items),
            "keep": keep,
            "trash": trash,
            "absolute": absolute,
            "last": items[0][0].date,
            "first": items[-1][0].date,
            "proposed": "keep" if trash == 0 else "trash" if keep == 0 else "mixed",
            "categories": [c for c, _ in cats],
            "reasons": [r for r, _ in reasons],
            "subjects": list(dict.fromkeys(m.subject for m, _ in items))[:4],
            "override": overrides.get(sender, ""),
        })
    rows.sort(key=lambda r: (-r["count"], r["sender"]))
    return {
        "account": account,
        "generated": int(time.time()),
        "senders": rows,
    }


def _embed_json(data: dict) -> str:
    # Safe inside <script type="application/json">: no "</script>", no HTML parsing surprises.
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def write_review(path: str | Path, data: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.replace("__DATA__", _embed_json(data)), encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Inbox review</title>
<style>
:root {
  --bg-light: #fbfaf7; --bg-dark: #16171a;
  --fg-light: #1d1f23; --fg-dark: #e8e6e1;
  --muted-light: #5d626b; --muted-dark: #a3a7ae;
  --line-light: #e3e0d8; --line-dark: #2c2e33;
  --row-light: #ffffff; --row-dark: #1d1f23;
  --keep-light: #1f6f43; --keep-dark: #6fd39b;
  --trash-light: #a3361c; --trash-dark: #ff9a7a;
  --accent-light: #2f4fbf; --accent-dark: #9fb4ff;
  --bg: var(--bg-light); --fg: var(--fg-light); --muted: var(--muted-light); --line: var(--line-light);
  --row: var(--row-light); --keep: var(--keep-light); --trash: var(--trash-light); --accent: var(--accent-light);
  @media (prefers-color-scheme: dark) {
    --bg: var(--bg-dark); --fg: var(--fg-dark); --muted: var(--muted-dark); --line: var(--line-dark);
    --row: var(--row-dark); --keep: var(--keep-dark); --trash: var(--trash-dark); --accent: var(--accent-dark);
  }
  @supports (color: light-dark(white, black)) {
    --bg: light-dark(var(--bg-light), var(--bg-dark)); --fg: light-dark(var(--fg-light), var(--fg-dark));
    --muted: light-dark(var(--muted-light), var(--muted-dark)); --line: light-dark(var(--line-light), var(--line-dark));
    --row: light-dark(var(--row-light), var(--row-dark)); --keep: light-dark(var(--keep-light), var(--keep-dark));
    --trash: light-dark(var(--trash-light), var(--trash-dark)); --accent: light-dark(var(--accent-light), var(--accent-dark));
  }
  color-scheme: light dark;
  accent-color: var(--accent);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header, main, footer { padding: 16px clamp(16px, 4vw, 40px); }
h1 { margin: 0 0 4px; font-size: 1.5rem; }
.lede { margin: 0 0 12px; color: var(--muted); }
.totals { display: flex; gap: 24px; flex-wrap: wrap; font-size: 1.05rem; margin-bottom: 12px; }
.totals b { font-variant-numeric: tabular-nums; }
.keep { color: var(--keep); } .trash { color: var(--trash); }
.controls { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
.controls label { display: grid; gap: 4px; font-size: .85rem; color: var(--muted); }
input, select, button { font: inherit; color: inherit; }
input[type=search], select { padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--row); min-height: 36px; }
.primary { padding: 8px 14px; border-radius: 6px; border: 0; background: var(--accent); color: var(--bg); font-weight: 600; min-height: 36px; cursor: pointer; }
.scroller { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; min-inline-size: 960px; background: var(--row); }
caption { text-align: start; padding: 10px 12px; color: var(--muted); }
th, td { padding: 8px 12px; border-top: 1px solid var(--line); text-align: start; vertical-align: top; }
thead th { position: sticky; top: 0; background: var(--row); font-weight: 600; white-space: nowrap; }
th:has(> button) { padding: 0; }
th > button { all: unset; display: flex; gap: 6px; align-items: center; padding: 8px 12px; min-height: 24px; cursor: pointer; width: 100%; box-sizing: border-box; font-weight: 600; }
th > button:focus-visible, select:focus-visible, input:focus-visible, .primary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
th > button svg { width: 12px; height: 12px; opacity: .35; }
th[aria-sort] > button svg { opacity: 1; }
th[aria-sort="descending"] > button svg { transform: rotate(180deg); }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.who b { display: block; overflow-wrap: anywhere; }
.who span, .why, .subjects { color: var(--muted); font-size: .88rem; overflow-wrap: anywhere; }
.subjects li { margin: 0 0 2px; }
.subjects { margin: 0; padding-inline-start: 16px; max-width: 42ch; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 99px; border: 1px solid currentColor; font-size: .8rem; font-weight: 600; white-space: nowrap; }
.locked { display: block; margin-top: 4px; color: var(--muted); font-size: .8rem; }
tr.changed td:last-child { box-shadow: inset 3px 0 0 var(--accent); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
footer { color: var(--muted); }
code { font-size: .9em; }
</style>
</head>
<body>
<header>
  <h1>Inbox review</h1>
  <p class="lede" id="lede"></p>
  <div class="totals" aria-live="polite">
    <span>Keep <b class="keep" id="t-keep">0</b> messages</span>
    <span>Trash <b class="trash" id="t-trash">0</b> messages</span>
    <span>Senders changed <b id="t-changed">0</b></span>
  </div>
  <div class="controls">
    <label>Search senders and subjects<input type="search" id="q" placeholder="name, address or subject"></label>
    <label>Show
      <select id="show">
        <option value="all">All senders</option>
        <option value="trash">Proposed trash</option>
        <option value="keep">Proposed keep</option>
        <option value="mixed">Mixed</option>
        <option value="changed">Changed by you</option>
      </select>
    </label>
    <button type="button" class="primary" id="download">Download decisions.json</button>
  </div>
</header>
<main>
  <div class="scroller" role="region" aria-labelledby="cap" tabindex="0">
    <table id="senders">
      <caption id="cap">One row per sender. "Follow rules" keeps the proposal; "Keep all" or "Trash all" overrides it for that sender. Threads you replied in, starred mail and protected people are always kept.</caption>
      <thead>
        <tr>
          <th scope="col" data-key="sender" data-type="text"><button type="button">Sender<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2l4 6H2z" fill="currentColor"/></svg></button></th>
          <th scope="col" data-key="count" data-type="number" aria-sort="descending"><button type="button">Emails<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2l4 6H2z" fill="currentColor"/></svg></button></th>
          <th scope="col" data-key="last" data-type="number"><button type="button">Latest<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2l4 6H2z" fill="currentColor"/></svg></button></th>
          <th scope="col" data-key="proposed" data-type="text"><button type="button">Proposed<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2l4 6H2z" fill="currentColor"/></svg></button></th>
          <th scope="col">Why</th>
          <th scope="col">Sample subjects</th>
          <th scope="col">Your decision</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
  <p role="status" class="visually-hidden" id="status"></p>
</main>
<footer>
  <p>When you're done, download <code>decisions.json</code> and run:<br>
  <code>inbox-sweep classify --overrides decisions.json</code> then <code>inbox-sweep plan</code>.</p>
</footer>
<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('data').textContent);
  const table = document.getElementById('senders');
  const tbody = table.tBodies[0];
  const status = document.getElementById('status');
  const storeKey = 'inbox-sweep:' + data.account + ':' + data.generated;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(storeKey) || '{}'); } catch (e) { saved = {}; }

  const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short' });
  document.getElementById('lede').textContent =
    (data.account ? data.account + ' · ' : '') + data.senders.length.toLocaleString() +
    ' senders · generated ' + new Date(data.generated * 1000).toLocaleString();

  const el = (tag, props = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'text') node.textContent = v; else if (k === 'class') node.className = v; else node.setAttribute(k, v);
    }
    for (const c of children) node.append(c);
    return node;
  };

  const rows = data.senders.map((s) => {
    const choice = saved[s.sender] ?? s.override ?? '';
    const select = el('select', { 'aria-label': 'Decision for ' + (s.name || s.sender) }, [
      el('option', { value: '', text: 'Follow rules' }),
      el('option', { value: 'keep', text: 'Keep all' }),
      el('option', { value: 'trash', text: 'Trash all' }),
    ]);
    select.value = choice;
    const proposedClass = s.proposed === 'keep' ? 'keep' : s.proposed === 'trash' ? 'trash' : '';
    const proposedText = s.proposed === 'mixed' ? `keep ${s.keep} · trash ${s.trash}` : s.proposed;
    const decisionCell = el('td', {}, [select]);
    if (s.absolute) decisionCell.append(el('span', { class: 'locked', text: `${s.absolute} always kept (replied, starred or protected)` }));
    const tr = el('tr', {}, [
      el('td', { class: 'who' }, [el('b', { text: s.name || s.sender }), el('span', { text: s.name ? s.sender : '' })]),
      el('td', { class: 'num', 'data-value': String(s.count), text: s.count.toLocaleString() }),
      el('td', { class: 'num', 'data-value': String(s.last), text: fmtDate(s.last) }),
      el('td', { 'data-value': s.proposed }, [el('span', { class: 'pill ' + proposedClass, text: proposedText })]),
      el('td', { class: 'why', text: s.reasons.join(' · ') + (s.categories.length ? ' (' + s.categories.join(', ') + ')' : '') }),
      el('td', {}, [el('ul', { class: 'subjects' }, s.subjects.map((t) => el('li', { text: t || '(no subject)' })))]),
      decisionCell,
    ]);
    tr.dataset.sender = s.sender;
    tr.dataset.search = (s.name + ' ' + s.sender + ' ' + s.subjects.join(' ')).toLowerCase();
    select.addEventListener('change', () => { update(); persist(); });
    return { tr, s, select };
  });
  for (const r of rows) tbody.append(r.tr);

  function counts(r) {
    const v = r.select.value;
    if (v === 'keep') return [r.s.count, 0];
    if (v === 'trash') return [r.s.absolute, r.s.count - r.s.absolute];
    return [r.s.keep, r.s.trash];
  }
  function update() {
    let keep = 0, trash = 0, changed = 0;
    for (const r of rows) {
      const [k, t] = counts(r); keep += k; trash += t;
      const isChanged = r.select.value !== '';
      r.tr.classList.toggle('changed', isChanged);
      if (isChanged) changed++;
    }
    document.getElementById('t-keep').textContent = keep.toLocaleString();
    document.getElementById('t-trash').textContent = trash.toLocaleString();
    document.getElementById('t-changed').textContent = changed.toLocaleString();
  }
  function persist() {
    const choices = {};
    for (const r of rows) if (r.select.value) choices[r.s.sender] = r.select.value;
    try { localStorage.setItem(storeKey, JSON.stringify(choices)); } catch (e) { /* storage unavailable: choices still download */ }
  }

  const q = document.getElementById('q');
  const show = document.getElementById('show');
  function filter() {
    const term = q.value.trim().toLowerCase();
    const mode = show.value;
    let visible = 0;
    for (const r of rows) {
      const okTerm = !term || r.tr.dataset.search.includes(term);
      const okMode = mode === 'all' || (mode === 'changed' ? r.select.value !== '' : r.s.proposed === mode);
      r.tr.hidden = !(okTerm && okMode);
      if (!r.tr.hidden) visible++;
    }
    status.textContent = `${visible} senders shown`;
  }
  q.addEventListener('input', filter);
  show.addEventListener('change', filter);

  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  const headers = [...table.tHead.rows[0].cells];
  for (const th of headers) {
    const button = th.querySelector('button');
    if (!button) continue;
    button.addEventListener('click', () => {
      const direction = th.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
      for (const h of headers) h.removeAttribute('aria-sort');
      th.setAttribute('aria-sort', direction);
      const idx = th.cellIndex, numeric = th.dataset.type === 'number', factor = direction === 'ascending' ? 1 : -1;
      const keyed = [...tbody.rows].map((tr) => {
        const cell = tr.cells[idx];
        const raw = cell.dataset.value ?? cell.textContent.trim();
        return { tr, v: numeric ? Number(raw) : raw };
      });
      keyed.sort((a, b) => factor * (numeric ? a.v - b.v : collator.compare(a.v, b.v)));
      const move = typeof tbody.moveBefore === 'function'
        ? (node) => tbody.moveBefore(node, null)
        : (node) => tbody.appendChild(node);
      for (const { tr } of keyed) move(tr);
      status.textContent = `Sorted by ${button.textContent.trim()}, ${direction}`;
    });
  }

  document.getElementById('download').addEventListener('click', () => {
    const overrides = {};
    for (const r of rows) if (r.select.value) overrides[r.s.sender] = r.select.value;
    const blob = new Blob([JSON.stringify({ version: 1, account: data.account, overrides }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: 'decisions.json' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = `Downloaded decisions for ${Object.keys(overrides).length} senders`;
  });

  update();
})();
</script>
</body>
</html>
"""

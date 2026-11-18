/*
 * inbox-sweep browser helpers for Gmail's web interface.
 *
 * Inject into a Gmail tab (an agent's JavaScript tool, or paste into DevTools).
 * Exposes window.inboxSweep. These helpers only read the page and change the
 * URL hash to run searches. They never click: clicking stays with the agent, so
 * every destructive step is a visible, deliberate action.
 *
 * Gmail's DOM is not a public API. Selectors here were correct in late 2026;
 * if rows() returns nothing on a page that clearly has mail, re-check them.
 */
(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => el && el.offsetParent !== null;
  const all = (sel, root = document) => [...root.querySelectorAll(sel)].filter(visible);
  const main = () => all('div[role=main]')[0];

  /** Conversation rows on the current list page. */
  function rows() {
    const m = main();
    if (!m) return [];
    return [...m.querySelectorAll('tr.zA')].filter(visible).map((r) => ({
      thread: r.querySelector('[data-legacy-thread-id]')?.getAttribute('data-legacy-thread-id') || r.innerText.slice(0, 80),
      senders: [...r.querySelectorAll('.yX span[email]')].map((s) => ({
        name: s.getAttribute('name') || '',
        email: (s.getAttribute('email') || '').toLowerCase(),
      })),
      subject: (r.querySelector('.bog')?.innerText || '').slice(0, 120),
      date: r.querySelector('.xW span')?.getAttribute('title') || '',
      selected: r.classList.contains('x7'),
    }));
  }

  /** "1–50 of 2,341" (or "of many") for the current list, or "" when empty. */
  const countText = () => all('.Dj')[0]?.innerText || '';

  function signature() {
    const r = rows();
    if (r.length) return r.slice(0, 3).map((x) => x.thread).join('|') + ':' + r.length;
    const empty = all('div[role=main] td.TC')[0]?.innerText || '';
    return 'EMPTY:' + empty;
  }

  /** Navigate by hash and wait until the list actually changes. */
  async function go(hash, timeoutMs = 15000) {
    const before = signature();
    location.hash = hash;
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await sleep(250);
      const s = signature();
      if (s !== before && (!s.startsWith('EMPTY:') || s.length > 6)) return s;
    }
    return signature();
  }

  /** Run a Gmail search. Returns {query, count, rows}. Resets via the inbox so a repeat search refreshes. */
  async function search(query) {
    await go('#inbox');
    await go('#search/' + encodeURIComponent(query));
    await sleep(600);
    return { query, count: countText(), rows: rows().length };
  }

  /** Page through a search and aggregate senders. maxPages * 50 threads at most. */
  async function scan(query, maxPages = 100) {
    const seen = new Set();
    const senders = new Map();
    let pages = 0;
    await go('#inbox');
    for (let p = 1; p <= maxPages; p++) {
      await go('#search/' + encodeURIComponent(query) + (p > 1 ? '/p' + p : ''));
      await sleep(300);
      const page = rows();
      if (!page.length) break;
      let fresh = 0;
      for (const r of page) {
        if (seen.has(r.thread)) continue;
        seen.add(r.thread);
        fresh++;
        for (const s of r.senders) {
          const entry = senders.get(s.email) || { email: s.email, name: s.name, threads: 0, samples: [], dates: [] };
          entry.threads++;
          if (entry.samples.length < 3) entry.samples.push(r.subject.slice(0, 70));
          if (r.date) entry.dates.push(r.date);
          senders.set(s.email, entry);
        }
      }
      pages = p;
      if (!fresh || page.length < 50) break;
    }
    const list = [...senders.values()].sort((a, b) => b.threads - a.threads).map((e) => ({
      email: e.email, name: e.name, threads: e.threads, samples: e.samples,
      newest: e.dates[0] || '', oldest: e.dates[e.dates.length - 1] || '',
    }));
    return { query, pages, threads: seen.size, senders: list };
  }

  /** Background jobs: long scans outlive a single tool call. Poll jobs[name]. */
  const jobs = {};
  function startJob(name, fn) {
    jobs[name] = { done: false, result: null, error: null };
    fn().then((r) => { jobs[name].result = r; }).catch((e) => { jobs[name].error = String(e); })
      .finally(() => { jobs[name].done = true; });
    return 'started ' + name;
  }

  /** Some agent tools filter JS output; write text into the page and read it with a page-text tool. */
  function dump(text) {
    let a = document.getElementById('inbox-sweep-out');
    if (!a) {
      a = document.createElement('article');
      a.id = 'inbox-sweep-out';
      a.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0.01;z-index:-1;white-space:pre';
      document.body.appendChild(a);
    }
    a.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 1);
    return a.textContent.length;
  }

  // ---- Safety gates: each throws when Gmail is not in the expected state. ----
  // In a batch of browser actions, a thrown error stops the batch before the next click or keystroke.

  function assertSearch(query) {
    const want = '#search/' + encodeURIComponent(query);
    const got = location.hash.replace(/\+/g, '%20');
    if (got !== want && decodeURIComponent(got) !== decodeURIComponent(want)) {
      throw new Error('Not on the expected search. Now: ' + decodeURIComponent(location.hash));
    }
    return 'on search';
  }

  function assertMostRecentSort() {
    const t = all('[aria-label*="sort" i], [data-tooltip*="sort" i]').map((e) => e.innerText).join(' ');
    if (/relevan/i.test(t)) throw new Error('Search results are sorted by relevance. Switch to "Most recent" first.');
    return 'sort ok';
  }

  function selectAllLink() {
    return all('span').find((e) => /^Select all conversations that match this search$/.test(e.innerText.trim())) || null;
  }

  function assertSelectAllBanner() {
    if (!selectAllLink()) throw new Error('No "select all conversations" banner (50 or fewer results?). Do not click where it would be.');
    return 'banner present';
  }

  function assertNoSelectAllBanner() {
    if (selectAllLink()) throw new Error('More than one page of results: use the select-all banner.');
    return 'single page';
  }

  function assertAllSelected() {
    const ok = all('span').some((e) => /All conversations in this search are selected/.test(e.innerText));
    if (!ok) throw new Error('Not every matching conversation is selected.');
    return 'all selected';
  }

  function assertSomeSelected() {
    const n = rows().filter((r) => r.selected).length;
    if (!n) throw new Error('Nothing is selected.');
    return n + ' selected';
  }

  /** Call right after opening the "Label as" menu and BEFORE typing: typing anywhere else fires Gmail shortcuts. */
  function assertLabelMenuOpen() {
    const a = document.activeElement;
    if (!(a && a.tagName === 'INPUT' && a.closest('[role=menu]'))) throw new Error('Label menu is not open/focused. Do not type.');
    return 'label menu focused';
  }

  function labelItem(name) {
    const menu = all('[role=menu]')[0];
    if (!menu) return null;
    return [...menu.querySelectorAll('[role=menuitemcheckbox]')].filter(visible)
      .find((e) => (e.innerText || '').trim().toLowerCase() === name.toLowerCase()) || null;
  }

  function assertLabelChecked(name) {
    const item = labelItem(name);
    if (!item || item.getAttribute('aria-checked') !== 'true') throw new Error('Label "' + name + '" is not ticked.');
    return 'checked';
  }

  function assertConfirmDialog() {
    const d = all('[role=alertdialog],[role=dialog]').find((e) => /Confirm bulk action/.test(e.innerText));
    if (!d) throw new Error('No "Confirm bulk action" dialog.');
    return 'confirm dialog shown';
  }

  // ---- Locating things to click. Returns CSS-pixel centres; scale them to your screenshot. ----
  // scaled = cssValue * (screenshotWidth / window.innerWidth)

  const centre = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), innerWidth: window.innerWidth };
  };

  function locate(what, arg) {
    switch (what) {
      case 'selectAllCheckbox': return centre(all('[aria-label="Select"], [data-tooltip="Select"]')[0]);
      case 'selectAllLink': return centre(selectAllLink());
      case 'labelButton': return centre(all('[aria-label="Labels"], [data-tooltip="Labels"]')[0]);
      case 'deleteButton': return centre(all('[aria-label="Delete"], [data-tooltip="Delete"]')[0]);
      case 'moveToInboxButton': return centre(all('[aria-label="Move to Inbox"], [data-tooltip="Move to Inbox"]')[0]);
      case 'labelItem': return centre(labelItem(arg));
      case 'labelApply': {
        const menu = all('[role=menu]')[0];
        return centre(menu && [...menu.querySelectorAll('[role=menuitem]')].filter(visible).find((e) => /Apply/.test(e.innerText)));
      }
      case 'confirmOk': {
        const d = all('[role=alertdialog],[role=dialog]').find((e) => /Confirm bulk action|Remove label/.test(e.innerText));
        return centre(d && [...d.querySelectorAll('button')].filter(visible).find((b) => /^(OK|Delete)$/.test(b.innerText.trim())));
      }
      default: throw new Error('Unknown target ' + what);
    }
  }

  window.inboxSweep = {
    version: '0.1.0', rows, countText, search, scan, go, jobs, startJob, dump, locate,
    assertSearch, assertMostRecentSort, assertSelectAllBanner, assertNoSelectAllBanner, assertAllSelected,
    assertSomeSelected, assertLabelMenuOpen, assertLabelChecked, assertConfirmDialog,
  };
  return 'inboxSweep ready';
})();

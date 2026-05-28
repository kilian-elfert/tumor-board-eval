/* Annotator UI — drag-select spans, click to edit, autosave per item. */
(function() {
  'use strict';

  const CASE_ID = window.ANN_CASE_ID;
  // Working copy of the annotation; mirrors what the server stores.
  const ANN = window.ANN_ANNOTATION || { texts: {} };
  if (!ANN.texts) ANN.texts = {};

  function ensureBlock(textKey) {
    if (!ANN.texts[textKey]) ANN.texts[textKey] = { items: {} };
    if (!ANN.texts[textKey].items) ANN.texts[textKey].items = {};
    return ANN.texts[textKey].items;
  }

  function getItem(textKey, slug) {
    return ensureBlock(textKey)[slug] || null;
  }

  function setItem(textKey, slug, status, spans) {
    const items = ensureBlock(textKey);
    if (!status && (!spans || spans.length === 0)) {
      delete items[slug];
    } else {
      items[slug] = { status: status || null, spans: spans || [] };
    }
  }

  /* ── Persistence ─────────────────────────────────────────────────────── */
  function save(textKey, slug, status, spans) {
    return fetch('/annotate/' + encodeURIComponent(CASE_ID) + '/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text_key: textKey, slug: slug, status: status, spans: spans })
    }).then(r => r.json());
  }

  function persistItem(textKey, slug) {
    const it = getItem(textKey, slug) || { status: null, spans: [] };
    save(textKey, slug, it.status, it.spans).then(res => {
      if (res && res.ok) updateProgress(textKey, res.progress);
    });
  }

  /* ── Sidebar status pills ───────────────────────────────────────────── */
  function refreshSidebar(textKey, slug) {
    const it = getItem(textKey, slug);
    const status = (it && it.status) || 'unrated';
    document.querySelectorAll(
      '.ann-status-pill[data-text-key="' + cssEsc(textKey) + '"][data-slug="' + cssEsc(slug) + '"]'
    ).forEach(btn => {
      btn.classList.remove('ann-st-enthalten', 'ann-st-falsch', 'ann-st-nicht_enthalten', 'ann-st-unrated');
      btn.classList.add('ann-st-' + status);
      btn.title = btn.dataset.textKey + ': ' + (status === 'unrated' ? 'noch nicht bewertet' : status);
    });
  }

  function refreshAllSidebar() {
    document.querySelectorAll('.ann-status-pill').forEach(btn => {
      refreshSidebar(btn.dataset.textKey, btn.dataset.slug);
    });
  }

  function updateProgress(textKey, prog) {
    const el = document.querySelector('.ann-text-progress-label[data-text-key="' + cssEsc(textKey) + '"]');
    if (el && prog) el.textContent = prog[0] + ' / ' + prog[1] + ' bewertet';
  }

  function computeAllProgress() {
    document.querySelectorAll('.ann-text-pane').forEach(pane => {
      const tk = pane.dataset.textKey;
      const items = ensureBlock(tk);
      let n = 0;
      Object.values(items).forEach(it => { if (it.status) n++; });
      const total = document.querySelectorAll('.ann-item').length;
      updateProgress(tk, [n, total]);
    });
  }

  /* ── Highlight rendering ─────────────────────────────────────────────── */
  /* For each text pane, rebuild the inner HTML from raw text + spans.
     Overlapping spans are merged into a single highlight whose body uses
     the primary (first) slug's status color. A numbered footnote badge is
     appended for every overlapping slug, mirroring the evaluator UI. */
  function renderPane(pane) {
    const raw = pane.dataset.raw || '';
    const items = ensureBlock(pane.dataset.textKey);
    // Collect (start, end, slug, status, text) for every saved span
    const regions = [];
    Object.keys(items).forEach(slug => {
      const it = items[slug];
      if (!it.spans) return;
      it.spans.forEach(s => {
        if (!s) return;
        let idx = 0;
        while (idx < raw.length) {
          const found = raw.indexOf(s, idx);
          if (found === -1) break;
          regions.push({ start: found, end: found + s.length, slug, status: it.status, text: s });
          idx = found + s.length;
        }
      });
    });
    // Sort by start asc, longer first to prefer longest match on tie
    regions.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));

    // Merge overlapping regions, tracking every slug that overlaps
    const merged = []; // { start, end, members: [{slug,status,text}] }
    regions.forEach(r => {
      const last = merged.length ? merged[merged.length - 1] : null;
      if (last && r.start < last.end) {
        // Overlap: extend end, append member if its slug is new
        if (r.end > last.end) last.end = r.end;
        if (!last.members.some(m => m.slug === r.slug)) {
          last.members.push({ slug: r.slug, status: r.status, text: r.text });
        }
      } else {
        merged.push({
          start: r.start, end: r.end,
          members: [{ slug: r.slug, status: r.status, text: r.text }],
        });
      }
    });

    // Build HTML
    let html = '';
    let prev = 0;
    merged.forEach(reg => {
      html += escapeHtml(raw.slice(prev, reg.start));
      const primary = reg.members[0];
      const cls = 'ann-hl ann-st-' + (primary.status || 'enthalten');
      html += '<span class="' + cls + '"'
            + ' data-slug="' + escapeAttr(primary.slug) + '"'
            + ' data-text="' + escapeAttr(primary.text) + '"'
            + ' title="' + escapeAttr(slugLabel(primary.slug)) + '">'
            + escapeHtml(raw.slice(reg.start, reg.end))
            + '</span>';
      if (reg.members.length > 1) {
        reg.members.forEach((m, i) => {
          const fnCls = 'ann-footnote ann-fn-' + (m.status || 'enthalten');
          html += '<sup class="' + fnCls + '"'
                + ' data-slug="' + escapeAttr(m.slug) + '"'
                + ' data-text="' + escapeAttr(m.text) + '"'
                + ' title="' + escapeAttr(slugLabel(m.slug)) + '">'
                + (i + 1) + '</sup>';
        });
      }
      prev = reg.end;
    });
    html += escapeHtml(raw.slice(prev));
    pane.innerHTML = html;
  }

  function renderAllPanes() {
    document.querySelectorAll('.ann-text-content').forEach(renderPane);
  }

  function slugLabel(slug) {
    const items = document.querySelectorAll('.ann-popup-item');
    for (const b of items) {
      if (b.dataset.slug === slug) return b.dataset.label;
    }
    return slug;
  }

  /* ── Text-pane switcher ─────────────────────────────────────────────── */
  document.querySelectorAll('.ann-text-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tk = btn.dataset.textKey;
      document.querySelectorAll('.ann-text-btn').forEach(b => {
        const active = b.dataset.textKey === tk;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      document.querySelectorAll('.ann-text-pane').forEach(p => {
        const match = p.dataset.textKey === tk;
        p.hidden = !match;
        p.classList.toggle('active', match);
      });
    });
  });

  /* ── Sidebar pill clicks: cycle status without span ─────────────────── */
  /* Click cycles: unrated → nicht_enthalten → enthalten → falsch → unrated.
     Setting to nicht_enthalten clears spans. Setting to enthalten/falsch
     keeps existing spans (or none if there were none). */
  const CYCLE = { unrated: 'nicht_enthalten', nicht_enthalten: 'enthalten', enthalten: 'falsch', falsch: 'unrated' };
  document.querySelectorAll('.ann-status-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const tk = btn.dataset.textKey;
      const slug = btn.dataset.slug;
      const cur = (getItem(tk, slug) || {}).status || 'unrated';
      const next = CYCLE[cur];
      const it = getItem(tk, slug) || { status: null, spans: [] };
      if (next === 'nicht_enthalten') it.spans = [];
      it.status = next === 'unrated' ? null : next;
      setItem(tk, slug, it.status, it.spans);
      refreshSidebar(tk, slug);
      const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(tk) + '"]');
      if (pane) renderPane(pane);
      persistItem(tk, slug);
    });
  });

  /* ── Sidebar search ─────────────────────────────────────────────────── */
  const itemSearch = document.getElementById('ann-item-search');
  if (itemSearch) {
    itemSearch.addEventListener('input', () => {
      const q = itemSearch.value.toLowerCase();
      document.querySelectorAll('.ann-item').forEach(it => {
        const label = (it.dataset.label || '').toLowerCase();
        it.style.display = (!q || label.includes(q)) ? '' : 'none';
      });
    });
  }

  /* ── Text selection → assign popup ──────────────────────────────────── */
  let _pendingRange = null;
  let _pendingText = null;
  let _pendingTextKey = null;

  document.querySelectorAll('.ann-text-content').forEach(pane => {
    pane.addEventListener('mouseup', e => setTimeout(() => onSelection(e, pane), 10));
    pane.addEventListener('click', e => {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().replace(/^\s+|\s+$/g, '').length >= 2) return;
      const fn = e.target.closest('.ann-footnote');
      if (fn) {
        e.stopPropagation();
        // Build a synthetic anchor with the footnote's slug/text so the
        // existing span-popup code can edit that specific overlap member.
        openSpanPopup({ dataset: { slug: fn.dataset.slug, text: fn.dataset.text } }, pane, e);
        return;
      }
      const hl = e.target.closest('.ann-hl');
      if (hl) {
        e.stopPropagation();
        openSpanPopup(hl, pane, e);
      }
    });
  });

  function onSelection(e, pane) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    if (!pane.contains(sel.anchorNode) || !pane.contains(sel.focusNode)) return;
    const text = sel.toString();
    // Trim only at edges to keep precise text
    const trimmed = text.replace(/^\s+|\s+$/g, '');
    if (trimmed.length < 2) return;
    _pendingRange = sel.getRangeAt(0).cloneRange();
    _pendingText = trimmed;
    _pendingTextKey = pane.dataset.textKey;
    openAssignPopup(trimmed, e);
  }

  function openAssignPopup(selectedText, ev) {
    const popup = document.getElementById('ann-assign-popup');
    popup.hidden = false;
    document.getElementById('ann-assign-seltext').textContent =
      '"' + (selectedText.length > 120 ? selectedText.slice(0, 117) + '…' : selectedText) + '"';
    document.getElementById('ann-assign-status').value = 'enthalten';
    const search = document.getElementById('ann-assign-search');
    if (search) { search.value = ''; filterAssign(''); }
    positionPopup(popup, ev);
    if (search) search.focus();
  }

  window.closeAssignPopup = function() {
    document.getElementById('ann-assign-popup').hidden = true;
    _pendingRange = null;
    _pendingText = null;
    _pendingTextKey = null;
    if (window.getSelection) window.getSelection().removeAllRanges();
  };

  function filterAssign(q) {
    q = q.toLowerCase();
    document.querySelectorAll('.ann-popup-item').forEach(b => {
      b.style.display = (!q || (b.dataset.label || '').toLowerCase().includes(q)) ? '' : 'none';
    });
  }
  const assignSearch = document.getElementById('ann-assign-search');
  if (assignSearch) {
    assignSearch.addEventListener('input', () => filterAssign(assignSearch.value));
    assignSearch.addEventListener('keydown', e => { if (e.key === 'Escape') closeAssignPopup(); });
  }

  document.querySelectorAll('.ann-popup-item').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!_pendingTextKey) return;
      const slug = btn.dataset.slug;
      const status = document.getElementById('ann-assign-status').value;
      const text = (_pendingText || (_pendingRange && _pendingRange.toString()) || '').replace(/^\s+|\s+$/g, '');
      if (!text) return closeAssignPopup();
      const it = getItem(_pendingTextKey, slug) || { status: null, spans: [] };
      // Promote status: if currently nicht_enthalten or null, set to selected.
      // If currently enthalten and user picks falsch (or vice versa), use new status.
      it.status = status;
      if (!it.spans) it.spans = [];
      if (it.spans.indexOf(text) === -1) it.spans.push(text);
      setItem(_pendingTextKey, slug, it.status, it.spans);
      refreshSidebar(_pendingTextKey, slug);
      const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(_pendingTextKey) + '"]');
      if (pane) renderPane(pane);
      persistItem(_pendingTextKey, slug);
      closeAssignPopup();
    });
  });

  /* ── Span popup (click existing highlight) ───────────────────────────── */
  let _editSpan = null; // { textKey, slug, text }
  function openSpanPopup(spanEl, pane, ev) {
    _editSpan = {
      textKey: pane.dataset.textKey,
      slug: spanEl.dataset.slug,
      text: spanEl.dataset.text
    };
    document.getElementById('ann-span-label').textContent = slugLabel(_editSpan.slug);
    document.getElementById('ann-span-seltext').textContent =
      '"' + (_editSpan.text.length > 120 ? _editSpan.text.slice(0, 117) + '…' : _editSpan.text) + '"';
    const popup = document.getElementById('ann-span-popup');
    popup.hidden = false;
    positionPopup(popup, ev);
  }
  window.closeSpanPopup = function() {
    document.getElementById('ann-span-popup').hidden = true;
    _editSpan = null;
  };

  document.querySelectorAll('.ann-span-action').forEach(btn => {
    btn.addEventListener('click', e => {
      if (!_editSpan) return;
      const { textKey, slug, text } = _editSpan;
      if (btn.classList.contains('ann-span-addcat')) {
        // Re-use this exact text to assign to another category.
        _pendingRange = null;
        _pendingText = text;
        _pendingTextKey = textKey;
        closeSpanPopup();
        openAssignPopup(text, e);
        return;
      }
      const it = getItem(textKey, slug);
      if (!it) return closeSpanPopup();
      if (btn.classList.contains('ann-span-remove')) {
        it.spans = (it.spans || []).filter(s => s !== text);
        if (it.spans.length === 0) it.status = null;
      } else {
        it.status = btn.dataset.status;
      }
      setItem(textKey, slug, it.status, it.spans);
      refreshSidebar(textKey, slug);
      const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(textKey) + '"]');
      if (pane) renderPane(pane);
      persistItem(textKey, slug);
      closeSpanPopup();
    });
  });

  /* ── Popup positioning + outside-click close ────────────────────────── */
  function positionPopup(popup, ev) {
    const w = popup.offsetWidth || 320;
    const h = popup.offsetHeight || 400;
    const gap = 10;
    const ax = (ev && ev.clientX) || (window.innerWidth / 2);
    const ay = (ev && ev.clientY) || (window.innerHeight / 2);
    let left = ax + gap;
    if (left + w > window.innerWidth - 10) left = ax - w - gap;
    if (left < 10) left = 10;
    let top = ay + gap;
    if (top + h > window.innerHeight - 10) top = ay - h - gap;
    if (top < 10) top = 10;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
  }

  document.addEventListener('mousedown', e => {
    const ap = document.getElementById('ann-assign-popup');
    if (!ap.hidden && !ap.contains(e.target) && !e.target.closest('.ann-text-content')) {
      closeAssignPopup();
    }
    const sp = document.getElementById('ann-span-popup');
    if (!sp.hidden && !sp.contains(e.target) && !e.target.closest('.ann-hl') && !e.target.closest('.ann-footnote')) {
      closeSpanPopup();
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeAssignPopup(); closeSpanPopup(); }
  });

  /* ── Utils ──────────────────────────────────────────────────────────── */
  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function cssEsc(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/"/g, '\\"');
  }

  /* ── Init ───────────────────────────────────────────────────────────── */
  const resetBtn = document.getElementById('ann-reset-btn');
  if (resetBtn) {
    resetBtn.addEventListener('click', async () => {
      const msg = 'Wirklich alle Annotationen verwerfen und auf die LLM-Vorschläge zurücksetzen?\n\nDieser Schritt kann nicht rückgängig gemacht werden.';
      if (!window.confirm(msg)) return;
      resetBtn.disabled = true;
      try {
        const r = await fetch(`/annotate/${CASE_ID}/reset`, { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        window.location.reload();
      } catch (e) {
        alert('Zurücksetzen fehlgeschlagen: ' + e.message);
        resetBtn.disabled = false;
      }
    });
  }

  document.querySelectorAll('.ann-llm-review-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const textKey = btn.dataset.textKey;
      const modeSel = document.querySelector(`.ann-llm-mode[data-text-key="${textKey}"]`);
      const mode = modeSel ? modeSel.value : 'merge';
      const msg = mode === 'overwrite'
        ? `LLM-Review (Alles ersetzen) für „${textKey}" starten?\n\nAlle bestehenden Annotationen für diese Sicht – inklusive Ihrer manuellen Markierungen – werden durch das LLM-Ergebnis ersetzt.`
        : `LLM-Review (Nur ergänzen) für „${textKey}" starten?\n\nManuell von Ihnen bearbeitete Kategorien bleiben unverändert. Alle anderen Kategorien werden mit dem LLM-Ergebnis aktualisiert.`;
      if (!window.confirm(msg)) return;
      const orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = '⏳ LLM läuft…';
      try {
        const r = await fetch(`/annotate/${CASE_ID}/llm-review`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text_key: textKey, mode }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
        const note = data.skipped_human
          ? `\n(${data.skipped_human} manuell bearbeitete Kategorie(n) übersprungen)`
          : '';
        // Reload after a tiny delay so the user can see the alert
        alert(`LLM-Review fertig: ${data.written} Kategorie(n) aktualisiert.${note}`);
        window.location.reload();
      } catch (e) {
        alert('LLM-Review fehlgeschlagen: ' + e.message);
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  });

  renderAllPanes();
  refreshAllSidebar();
  computeAllProgress();
})();

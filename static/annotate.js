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

  function setItem(textKey, slug, status, spans, extra) {
    const items = ensureBlock(textKey);
    if (!status && (!spans || spans.length === 0)) {
      delete items[slug];
      return;
    }
    const prev = items[slug] || {};
    const next = { status: status || null, spans: spans || [] };
    if (status === 'falsch') {
      // Priority: explicit `extra` from the popup > current in-memory item.
      // Empty string in `extra.description` (which is a valid "delete"
      // signal) is preserved as-is by the `typeof === 'string'` guard.
      if (extra && extra.error_category) {
        next.error_category = extra.error_category;
      } else if (prev.error_category) {
        next.error_category = prev.error_category;
      }
      if (extra && typeof extra.description === 'string') {
        next.description = extra.description;
      } else if (prev.description) {
        next.description = prev.description;
      }
    }
    items[slug] = next;
  }

  /* ── Persistence ─────────────────────────────────────────────────────── */
  function save(textKey, slug, status, spans, extra) {
    const body = { text_key: textKey, slug: slug, status: status, spans: spans };
    if (status === 'falsch' && extra) {
      if (extra.error_category) body.error_category = extra.error_category;
      if (typeof extra.description === 'string') body.description = extra.description;
    }
    return fetch('/annotate/' + encodeURIComponent(CASE_ID) + '/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(r => r.json());
  }

  function persistItem(textKey, slug) {
    const it = getItem(textKey, slug) || { status: null, spans: [] };
    const extra = (it.status === 'falsch')
      ? { error_category: it.error_category || 'inhaltlich', description: it.description || '' }
      : null;
    save(textKey, slug, it.status, it.spans, extra).then(res => {
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
      const label = (status === 'unrated') ? 'noch nicht bewertet' : status;
      let title = btn.dataset.textKey + ': ' + label;
      if (status === 'falsch' && it && it.error_category) {
        title += '\nFehlerart: ' + it.error_category;
        if (it.description) title += '\nBegründung: ' + it.description;
      }
      btn.title = title;
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
          regions.push({
            start: found, end: found + s.length,
            slug, status: it.status, text: s,
            error_category: it.error_category || '',
            description: it.description || '',
          });
          idx = found + s.length;
        }
      });
    });
    // Sort by start asc, longer first to prefer longest match on tie
    regions.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));

    // Merge overlapping regions, tracking every slug that overlaps
    const merged = []; // { start, end, members: [{slug,status,text,error_category,description}] }
    regions.forEach(r => {
      const last = merged.length ? merged[merged.length - 1] : null;
      if (last && r.start < last.end) {
        // Overlap: extend end, append member if its slug is new
        if (r.end > last.end) last.end = r.end;
        if (!last.members.some(m => m.slug === r.slug)) {
          last.members.push({
            slug: r.slug, status: r.status, text: r.text,
            error_category: r.error_category, description: r.description,
          });
        }
      } else {
        merged.push({
          start: r.start, end: r.end,
          members: [{
            slug: r.slug, status: r.status, text: r.text,
            error_category: r.error_category, description: r.description,
          }],
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
      const tooltip = buildTooltip(primary);
      html += '<span class="' + cls + '"'
            + ' data-slug="' + escapeAttr(primary.slug) + '"'
            + ' data-text="' + escapeAttr(primary.text) + '"'
            + ' title="' + escapeAttr(tooltip) + '">'
            + escapeHtml(raw.slice(reg.start, reg.end))
            + '</span>';
      if (reg.members.length > 1) {
        reg.members.forEach((m, i) => {
          const fnCls = 'ann-footnote ann-fn-' + (m.status || 'enthalten');
          html += '<sup class="' + fnCls + '"'
                + ' data-slug="' + escapeAttr(m.slug) + '"'
                + ' data-text="' + escapeAttr(m.text) + '"'
                + ' title="' + escapeAttr(buildTooltip(m)) + '">'
                + (i + 1) + '</sup>';
        });
      }
      prev = reg.end;
    });
    html += escapeHtml(raw.slice(prev));
    pane.innerHTML = html;
  }

  // Tooltip text shown on hover over a highlight or footnote.
  // For falsch items we include the error category and description so the
  // researcher / annotator can see the reasoning without opening a popup.
  function buildTooltip(member) {
    const label = slugLabel(member.slug);
    if (member.status !== 'falsch') return label;
    const parts = [label + ' · falsch'];
    if (member.error_category) {
      parts[0] += ' [' + ERROR_CAT_LABEL[member.error_category] + ']';
    }
    if (member.description) parts.push(member.description);
    return parts.join('\n');
  }

  const ERROR_CAT_LABEL = {
    formal:     'Formal',
    inhaltlich: 'Inhaltlich',
    logisch:    'Logisch/medizinisch',
    prozess:    'Prozess/System',
  };

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
     keeps existing spans (or none if there were none).
     Shift+click on a `falsch` pill opens the reason editor without cycling. */
  const CYCLE = { unrated: 'nicht_enthalten', nicht_enthalten: 'enthalten', enthalten: 'falsch', falsch: 'unrated' };
  document.querySelectorAll('.ann-status-pill').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      const tk = btn.dataset.textKey;
      const slug = btn.dataset.slug;

      const cur = (getItem(tk, slug) || {}).status || 'unrated';

      // Shift+click on an already-falsch pill: open the reason editor
      // without changing status.
      if (ev.shiftKey && cur === 'falsch') {
        openReasonPopup(tk, slug, btn);
        return;
      }

      const next = CYCLE[cur];
      const it = getItem(tk, slug) || { status: null, spans: [] };
      if (next === 'nicht_enthalten') it.spans = [];
      it.status = next === 'unrated' ? null : next;
      setItem(tk, slug, it.status, it.spans);
      refreshSidebar(tk, slug);
      const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(tk) + '"]');
      if (pane) renderPane(pane);
      persistItem(tk, slug);

      // When the annotator lands on falsch, auto-open the reason editor.
      if (it.status === 'falsch') {
        openReasonPopup(tk, slug, btn);
      }
    });
  });

  /* ── Sidebar pill hover: light up the matching spans in the text pane ─ */
  document.querySelectorAll('.ann-status-pill').forEach(btn => {
    const slug = btn.dataset.slug;
    const tk   = btn.dataset.textKey;
    const setHover = (on) => {
      const pane = document.querySelector(
        '.ann-text-content[data-text-key="' + cssEsc(tk) + '"]');
      if (!pane) return;
      pane.querySelectorAll(
        '.ann-hl[data-slug="' + cssEsc(slug) + '"], '
        + '.ann-footnote[data-slug="' + cssEsc(slug) + '"]'
      ).forEach(el => el.classList.toggle('ann-hl-pill-hover', on));
    };
    btn.addEventListener('mouseenter', () => setHover(true));
    btn.addEventListener('mouseleave', () => setHover(false));
    btn.addEventListener('focus',      () => setHover(true));
    btn.addEventListener('blur',       () => setHover(false));
  });

  /* ── Reason editor popup (falsch fields for sidebar-pill workflow) ──── */
  const reasonPopup = document.getElementById('ann-reason-popup');
  let _reasonCtx = null;   // { textKey, slug }

  function openReasonPopup(textKey, slug, anchorEl) {
    if (!reasonPopup) return;
    _reasonCtx = { textKey, slug };
    const cur = getItem(textKey, slug);

    document.getElementById('ann-reason-slug').textContent = slugLabel(slug);
    document.getElementById('ann-reason-cat').value =
      (cur && cur.error_category) || 'inhaltlich';
    document.getElementById('ann-reason-desc').value =
      (cur && cur.description) || '';
    reasonPopup.hidden = false;
    positionPopup(reasonPopup, anchorEl && anchorEl.getBoundingClientRect
      ? { clientX: anchorEl.getBoundingClientRect().right + window.scrollX,
          clientY: anchorEl.getBoundingClientRect().top   + window.scrollY }
      : { clientX: window.scrollX + window.innerWidth  / 2,
          clientY: window.scrollY + window.innerHeight / 3 });
    document.getElementById('ann-reason-desc').focus();
  }
  window.closeReasonPopup = function () {
    if (reasonPopup) reasonPopup.hidden = true;
    _reasonCtx = null;
  };
  const reasonCancel = document.getElementById('ann-reason-cancel');
  if (reasonCancel) reasonCancel.addEventListener('click', () => closeReasonPopup());
  const reasonSave = document.getElementById('ann-reason-save');
  if (reasonSave) reasonSave.addEventListener('click', () => {
    if (!_reasonCtx) return closeReasonPopup();
    const { textKey, slug } = _reasonCtx;
    const cat  = document.getElementById('ann-reason-cat').value;
    const desc = document.getElementById('ann-reason-desc').value.trim();
    const it = getItem(textKey, slug) || { status: 'falsch', spans: [] };
    setItem(textKey, slug, 'falsch', it.spans, { error_category: cat, description: desc });
    refreshSidebar(textKey, slug);
    const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(textKey) + '"]');
    if (pane) renderPane(pane);
    persistItem(textKey, slug);
    closeReasonPopup();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && reasonPopup && !reasonPopup.hidden) closeReasonPopup();
  });

  /* ── Sidebar search ─────────────────────────────────────────────────── */
  const itemSearch = document.getElementById('ann-item-search');

  function _applyItemFilter() {
    const q = itemSearch ? itemSearch.value.toLowerCase() : '';
    document.querySelectorAll('.ann-item').forEach(it => {
      const label = (it.dataset.label || '').toLowerCase();
      const matchQ = !q || label.includes(q);
      it.style.display = matchQ ? '' : 'none';
    });
  }

  if (itemSearch) itemSearch.addEventListener('input', _applyItemFilter);

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
    const statusSel = document.getElementById('ann-assign-status');
    statusSel.value = 'enthalten';
    toggleAssignFalschFields();
    document.getElementById('ann-assign-cat').value = 'inhaltlich';
    document.getElementById('ann-assign-desc').value = '';
    const search = document.getElementById('ann-assign-search');
    if (search) { search.value = ''; filterAssign(''); }
    positionPopup(popup, ev);
    if (search) search.focus();
  }

  function toggleAssignFalschFields() {
    const isFalsch = document.getElementById('ann-assign-status').value === 'falsch';
    document.getElementById('ann-assign-falsch-fields').hidden = !isFalsch;
  }
  document.getElementById('ann-assign-status').addEventListener('change', toggleAssignFalschFields);

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
      it.status = status;
      if (!it.spans) it.spans = [];
      if (it.spans.indexOf(text) === -1) it.spans.push(text);
      const extra = status === 'falsch' ? {
        error_category: document.getElementById('ann-assign-cat').value,
        description:    document.getElementById('ann-assign-desc').value.trim(),
      } : null;
      setItem(_pendingTextKey, slug, it.status, it.spans, extra);
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
    // Pre-fill error category + description from current item state.
    const cur = getItem(_editSpan.textKey, _editSpan.slug);
    document.getElementById('ann-span-cat').value =
        (cur && cur.error_category) || 'inhaltlich';
    document.getElementById('ann-span-desc').value =
        (cur && cur.description) || '';
    document.getElementById('ann-span-falsch-fields').hidden = !(cur && cur.status === 'falsch');
    const popup = document.getElementById('ann-span-popup');
    popup.hidden = false;
    positionPopup(popup, ev);
  }
  window.closeSpanPopup = function() {
    document.getElementById('ann-span-popup').hidden = true;
    _editSpan = null;
  };

  // Auto-save error_category + description when the annotator edits them
  // inside the open span popup.
  function spanFalschFieldsChanged() {
    if (!_editSpan) return;
    const { textKey, slug } = _editSpan;
    const it = getItem(textKey, slug);
    if (!it || it.status !== 'falsch') return;
    const extra = {
      error_category: document.getElementById('ann-span-cat').value,
      description:    document.getElementById('ann-span-desc').value.trim(),
    };
    setItem(textKey, slug, it.status, it.spans, extra);
    persistItem(textKey, slug);
    const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(textKey) + '"]');
    if (pane) renderPane(pane);
  }
  document.getElementById('ann-span-cat').addEventListener('change', spanFalschFieldsChanged);
  document.getElementById('ann-span-desc').addEventListener('blur', spanFalschFieldsChanged);

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
      // If we just promoted to falsch, expose the falsch fields inline so the
      // annotator can add category + description without closing the popup.
      if (it.status === 'falsch') {
        document.getElementById('ann-span-falsch-fields').hidden = false;
      } else {
        document.getElementById('ann-span-falsch-fields').hidden = true;
      }
      const extra = it.status === 'falsch' ? {
        error_category: document.getElementById('ann-span-cat').value,
        description:    document.getElementById('ann-span-desc').value.trim(),
      } : null;
      setItem(textKey, slug, it.status, it.spans, extra);
      refreshSidebar(textKey, slug);
      const pane = document.querySelector('.ann-text-content[data-text-key="' + cssEsc(textKey) + '"]');
      if (pane) renderPane(pane);
      persistItem(textKey, slug);
      // Keep popup open after promoting to falsch so user can fill description
      if (it.status === 'falsch' && !btn.classList.contains('ann-span-remove')) {
        return;
      }
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
    const rp = document.getElementById('ann-reason-popup');
    if (rp && !rp.hidden && !rp.contains(e.target) && !e.target.closest('.ann-status-pill')) {
      closeReasonPopup();
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeAssignPopup(); closeSpanPopup(); closeReasonPopup(); }
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

  // Save all text editors in one go — iterates every pane's textarea and
  // POSTs those whose content differs from the currently rendered raw text
  // to /annotate/<case>/text sequentially. Unchanged panes are skipped.
  const saveAllBtn = document.getElementById('ann-save-all-btn');
  if (saveAllBtn) {
    saveAllBtn.addEventListener('click', async () => {
      const panes = Array.from(document.querySelectorAll('.ann-text-pane'));
      const pending = [];
      for (const pane of panes) {
        const key = pane.dataset.textKey;
        const ta  = pane.querySelector('.ann-text-editor-area');
        const raw = pane.querySelector('.ann-text-content');
        if (!ta || !raw) continue;
        const prev = raw.dataset.raw || raw.textContent || '';
        if (ta.value !== prev) pending.push({ key, content: ta.value });
      }
      if (pending.length === 0) {
        alert('Keine Änderungen: Alle Texte entsprechen bereits dem gespeicherten Stand.');
        return;
      }
      const keys = pending.map(p => `• ${p.key}`).join('\n');
      const confirmMsg =
        `Folgende Texte werden auf die Quelldateien überschrieben:\n\n${keys}\n\n` +
        `Markierungen, deren Wortlaut durch die Änderungen nicht mehr im Text vorkommt, ` +
        `werden automatisch entfernt.\n\nFortfahren?`;
      if (!window.confirm(confirmMsg)) return;

      const origLabel = saveAllBtn.textContent;
      saveAllBtn.disabled = true;
      const failures = [];
      let droppedSpans = 0;
      const affected = new Set();
      let done = 0;
      for (const { key, content } of pending) {
        saveAllBtn.textContent = `⏳ Speichere ${++done}/${pending.length}…`;
        try {
          const r = await fetch(`/annotate/${CASE_ID}/text`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text_key: key, content }),
          });
          const data = await r.json();
          if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
          droppedSpans += data.dropped_spans || 0;
          for (const s of (data.items_affected || [])) affected.add(s);
        } catch (e) {
          failures.push(`${key}: ${e.message}`);
        }
      }
      if (failures.length) {
        alert(
          `${pending.length - failures.length}/${pending.length} Texte gespeichert.\n\n` +
          `Fehler:\n${failures.join('\n')}`
        );
        saveAllBtn.disabled = false;
        saveAllBtn.textContent = origLabel;
        return;
      }
      let msg = `${pending.length} Text(e) gespeichert.`;
      if (droppedSpans > 0) {
        msg += `\n\n${droppedSpans} Markierung(en) in ${affected.size} Kategorie(n) ` +
               `wurden entfernt, weil ihr Text nicht mehr exakt im neuen Text vorkommt.`;
      }
      alert(msg);
      window.location.reload();
    });
  }

  // ── Edit-and-overwrite the underlying source text ─────────────────────
  function setEditing(textKey, editing) {
    const pane = document.querySelector(`.ann-text-pane[data-text-key="${textKey}"]`);
    if (!pane) return;
    const content = pane.querySelector('.ann-text-content');
    const editor  = pane.querySelector('.ann-text-editor');
    const btn     = pane.querySelector('.ann-text-edit-btn');
    if (content) content.hidden = editing;
    if (editor)  editor.hidden  = !editing;
    if (btn) {
      btn.classList.toggle('is-editing', editing);
      btn.textContent = editing ? '✕ Bearbeitung beenden' : '✎ Bearbeiten';
    }
    if (editing) {
      const ta = pane.querySelector('.ann-text-editor-area');
      if (ta) {
        ta.focus();
        // Move cursor to end
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
    }
  }

  document.querySelectorAll('.ann-text-edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const textKey = btn.dataset.textKey;
      const pane = document.querySelector(`.ann-text-pane[data-text-key="${textKey}"]`);
      const editor = pane && pane.querySelector('.ann-text-editor');
      const editing = editor && !editor.hidden;
      setEditing(textKey, !editing);
    });
  });

  document.querySelectorAll('.ann-text-editor-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      const textKey = btn.dataset.textKey;
      const pane = document.querySelector(`.ann-text-pane[data-text-key="${textKey}"]`);
      const ta = pane && pane.querySelector('.ann-text-editor-area');
      const content = pane && pane.querySelector('.ann-text-content');
      // Restore textarea to the currently rendered raw text
      if (ta && content) ta.value = content.dataset.raw || content.textContent || '';
      setEditing(textKey, false);
    });
  });

  document.querySelectorAll('.ann-text-editor-save').forEach(btn => {
    btn.addEventListener('click', async () => {
      const textKey = btn.dataset.textKey;
      const pane = document.querySelector(`.ann-text-pane[data-text-key="${textKey}"]`);
      const ta = pane && pane.querySelector('.ann-text-editor-area');
      if (!ta) return;
      const newContent = ta.value;
      const orig = pane.querySelector('.ann-text-content');
      const prev = orig ? (orig.dataset.raw || orig.textContent || '') : '';
      if (newContent === prev) {
        alert('Der Text wurde nicht geändert.');
        setEditing(textKey, false);
        return;
      }
      if (!window.confirm(
        `Originaltext für „${textKey}" wirklich überschreiben?\n\n` +
        `Markierungen, deren Wortlaut durch Ihre Änderung nicht mehr im Text vorkommt, werden automatisch entfernt.`
      )) return;
      btn.disabled = true;
      const origLabel = btn.textContent;
      btn.textContent = '⏳ Speichern…';
      try {
        const r = await fetch(`/annotate/${CASE_ID}/text`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text_key: textKey, content: newContent }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || `HTTP ${r.status}`);
        let msg = 'Text gespeichert.';
        if (data.dropped_spans > 0) {
          msg += `\n\n${data.dropped_spans} Markierung(en) in ${data.items_affected.length} Kategorie(n) ` +
                 `wurden entfernt, weil ihr Text nicht mehr exakt im neuen Text vorkommt.`;
        }
        alert(msg);
        window.location.reload();
      } catch (e) {
        alert('Speichern fehlgeschlagen: ' + e.message);
        btn.disabled = false;
        btn.textContent = origLabel;
      }
    });
  });

  renderAllPanes();
  refreshAllSidebar();
  computeAllProgress();
})();

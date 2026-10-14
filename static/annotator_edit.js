/* Inline overview editor.
   Wires up the sticky toolbar in tab-overview when annotator_mode is on.
   Collects data-edit-path values, supports add/remove list rows, and POSTs
   the diff to /annotate/<case>/dashboard-edit. */
(function () {
  'use strict';

  var root = document.querySelector('.overview-editable');
  if (!root) return;

  var toolbar = root.querySelector('.overview-edit-toolbar');
  if (!toolbar) return;

  var saveBtn = toolbar.querySelector('.oet-save');
  var previewBtn = toolbar.querySelector('.oet-preview');
  var statusEl = toolbar.querySelector('.oet-status');
  var caseId = toolbar.getAttribute('data-case-id');
  var endpoint = toolbar.getAttribute('data-endpoint');
  var previewUrl = toolbar.getAttribute('data-preview-url');

  function setStatus(text, cls) {
    statusEl.textContent = text || '';
    statusEl.className = 'oet-status' + (cls ? ' ' + cls : '');
  }

  // Path setter: 'a.b.0.c' -> nested set. Numeric segments create arrays.
  function setPath(obj, dotted, value) {
    var parts = dotted.split('.');
    var cur = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      var key = parts[i];
      var nextKey = parts[i + 1];
      var nextIsIndex = /^\d+$/.test(nextKey);
      var curKey = /^\d+$/.test(key) ? Number(key) : key;
      if (cur[curKey] == null) cur[curKey] = nextIsIndex ? [] : {};
      cur = cur[curKey];
    }
    var last = parts[parts.length - 1];
    var lastKey = /^\d+$/.test(last) ? Number(last) : last;
    cur[lastKey] = value;
  }

  function readValue(el) {
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      return (el.value || '').trim();
    }
    return (el.innerText || '').trim();
  }

  // Re-number data-edit-path attributes inside a list container.
  // The container has data-edit-list="<basePath>" and rows have class
  // .dash-list-row. Each editable descendant has data-edit-path-suffix
  // (relative to the row) that we combine with the fresh index.
  function reindex(container) {
    var base = container.getAttribute('data-edit-list');
    var rows = container.querySelectorAll(':scope > .dash-list-row');
    rows.forEach(function (row, i) {
      row.setAttribute('data-edit-row-index', i);
      row.querySelectorAll('[data-edit-path-suffix]').forEach(function (el) {
        var suf = el.getAttribute('data-edit-path-suffix');
        el.setAttribute('data-edit-path', base + '.' + i + (suf ? '.' + suf : ''));
      });
    });
  }

  // Wire up add + delete + reindex for every list container.
  root.querySelectorAll('[data-edit-list]').forEach(function (container) {
    reindex(container);

    container.addEventListener('click', function (ev) {
      var del = ev.target.closest('.dash-row-del');
      if (del) {
        var row = del.closest('.dash-list-row');
        if (row) {
          row.remove();
          reindex(container);
        }
        return;
      }
      var add = ev.target.closest('.dash-list-add');
      if (add && add.getAttribute('data-target') === container.getAttribute('data-edit-list')) {
        var tpl = container.querySelector(':scope > .dash-row-template');
        if (!tpl) return;
        var clone = tpl.cloneNode(true);
        clone.classList.remove('dash-row-template');
        clone.classList.add('dash-list-row');
        // Insert before the add button if it's inside the container, else append.
        var btnInside = container.querySelector(':scope > .dash-list-add');
        if (btnInside) {
          container.insertBefore(clone, btnInside);
        } else {
          container.appendChild(clone);
        }
        reindex(container);
      }
    });
  });

  // Some add buttons live outside their list container.
  root.querySelectorAll('.dash-list-add').forEach(function (btn) {
    if (btn.__wired) return;
    btn.__wired = true;
    btn.addEventListener('click', function () {
      var targetSel = btn.getAttribute('data-target');
      var container = root.querySelector('[data-edit-list="' + targetSel + '"]');
      if (!container) return;
      var tpl = container.querySelector(':scope > .dash-row-template');
      if (!tpl) return;
      var clone = tpl.cloneNode(true);
      clone.classList.remove('dash-row-template');
      clone.classList.add('dash-list-row');
      container.appendChild(clone);
      reindex(container);
    });
  });

  // Convert a nested object with numeric keys into plain arrays.
  // The path collector produces objects like {surgery:{0:{date:'…'}}}; we
  // want {surgery:[{date:'…'}]}.
  function normalise(v) {
    if (v == null || typeof v !== 'object') return v;
    if (Array.isArray(v)) return v.map(normalise);
    var keys = Object.keys(v);
    var allNumeric = keys.length > 0 && keys.every(function (k) { return /^\d+$/.test(k); });
    if (allNumeric) {
      var arr = [];
      keys.map(Number).sort(function (a, b) { return a - b; }).forEach(function (i) {
        arr.push(normalise(v[i]));
      });
      return arr;
    }
    var out = {};
    keys.forEach(function (k) { out[k] = normalise(v[k]); });
    return out;
  }

  function collect() {
    var patch = {};
    root.querySelectorAll('[data-edit-path]').forEach(function (el) {
      // Skip elements inside a hidden template row.
      if (el.closest('.dash-row-template')) return;
      var path = el.getAttribute('data-edit-path');
      var val = readValue(el);
      setPath(patch, path, val);
    });

    // Force empty lists to round-trip: when every row in a container has been
    // deleted, no inputs contribute to `patch[basePath]`, so without this the
    // server would preserve the old list ("delete last entry" bug).
    root.querySelectorAll('[data-edit-list]').forEach(function (container) {
      var basePath = container.getAttribute('data-edit-list');
      if (!basePath || basePath === 'mutations') return;  // mutations is a dict
      if (container.querySelector(':scope > .dash-list-row')) return;
      var parts = basePath.split('.');
      var cur = patch;
      for (var i = 0; i < parts.length - 1; i++) {
        var k = parts[i];
        if (typeof cur[k] !== 'object' || cur[k] === null) cur[k] = {};
        cur = cur[k];
      }
      cur[parts[parts.length - 1]] = [];
    });

    // Mutations: DOM rows are {gene, status}; JSON wants {gene: status}.
    var mutContainer = root.querySelector('[data-edit-list="mutations"]');
    if (mutContainer) {
      var out = {};
      mutContainer.querySelectorAll(':scope > .dash-list-row').forEach(function (row) {
        var g = readValue(row.querySelector('[data-mut-gene]'));
        var s = readValue(row.querySelector('[data-mut-status]'));
        if (g) out[g] = s;
      });
      patch.mutations = out;
    }
    delete patch.__mut_rows; // safety

    var normalised = normalise(patch);

    // Clean metastasis pos overrides:
    //   - if x or y is missing/empty, drop the pos block entirely
    //   - else coerce to numbers with 2-decimal precision
    if (Array.isArray(normalised.metastases_detail)) {
      normalised.metastases_detail.forEach(function (m) {
        if (!m || typeof m !== 'object') return;
        if (!m.pos || typeof m.pos !== 'object') { delete m.pos; return; }
        var x = parseFloat(m.pos.x), y = parseFloat(m.pos.y);
        if (!isFinite(x) || !isFinite(y)) { delete m.pos; return; }
        m.pos = { x: +x.toFixed(2), y: +y.toFixed(2) };
      });
    }

    // Clean primary-tumor free-position override (same rules as metastases):
    //   - drop the override entirely if x or y is missing/empty
    //   - else coerce to numbers with 2-decimal precision
    if (normalised.primary_tumor_pos && typeof normalised.primary_tumor_pos === 'object') {
      var ptx = parseFloat(normalised.primary_tumor_pos.x);
      var pty = parseFloat(normalised.primary_tumor_pos.y);
      if (!isFinite(ptx) || !isFinite(pty)) {
        normalised.primary_tumor_pos = null;
      } else {
        normalised.primary_tumor_pos = { x: +ptx.toFixed(2), y: +pty.toFixed(2) };
      }
    }

    // Clean imaging entries:
    //   - coerce has_pdf back to a real bool (or drop if never set)
    //   - drop empty pdf-link strings so the shape stays clean for new rows
    if (Array.isArray(normalised.imaging)) {
      normalised.imaging.forEach(function (img) {
        if (!img || typeof img !== 'object') return;
        if (img.has_pdf === 'true')       img.has_pdf = true;
        else if (img.has_pdf === 'false') img.has_pdf = false;
        else if (img.has_pdf === '')      delete img.has_pdf;
        if (img.img_dir === '')      delete img.img_dir;
        if (img.pdf_filename === '') delete img.pdf_filename;
      });
    }

    return normalised;
  }

  saveBtn.addEventListener('click', function () {
    var body = collect();
    setStatus('Speichere…', 'busy');
    saveBtn.disabled = true;
    fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'invalid response' }; }).then(function (j) { return { r: r, j: j }; }); })
      .then(function (res) {
        if (!res.r.ok || !res.j.ok) {
          setStatus('Fehler: ' + (res.j.error || res.r.status), 'err');
          saveBtn.disabled = false;
          return;
        }
        setStatus('Gespeichert – lade neu…', 'ok');
        // Reload so body-map dots, timelines and sorting reflect the new JSON.
        window.location.reload();
      })
      .catch(function (e) {
        setStatus('Netzwerkfehler: ' + e, 'err');
        saveBtn.disabled = false;
      });
  });

  if (previewBtn && previewUrl) {
    previewBtn.addEventListener('click', function () {
      window.location.assign(previewUrl);
    });
  }
})();

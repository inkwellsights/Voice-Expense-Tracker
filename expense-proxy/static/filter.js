// expense-proxy / filter.js
// Injected into every ExpenseOwl HTML page by the proxy. Adds a
// floating multi-select tag filter (top-right pill) and monkey-patches
// window.fetch so calls to /expenses get filtered client-side before
// ExpenseOwl's own JS processes them. That makes both the pie chart
// (/) and the table (/table) honor the filter uniformly without any
// upstream changes.
//
// Persistence: sessionStorage — sticky across page navigations in the
// same browser session, but reverts to "show all" when the browser
// closes. No localStorage, no server-side state.
//
// Match logic: OR. An entry is shown if ANY of its tags is in the
// selected set. Empty selection = show everything.
(() => {
  "use strict";

  const KEY_SELECTED = "expense-proxy-selected-tags";
  const KEY_KNOWN = "expense-proxy-known-tags";

  const readJSON = (key, fallback) => {
    try {
      const raw = sessionStorage.getItem(key);
      return raw === null ? fallback : (JSON.parse(raw) ?? fallback);
    } catch (e) {
      return fallback;
    }
  };
  const writeJSON = (key, val) => {
    try { sessionStorage.setItem(key, JSON.stringify(val)); } catch (e) {}
  };

  const getSelected = () => readJSON(KEY_SELECTED, []);
  const setSelected = (arr) => writeJSON(KEY_SELECTED, arr);
  const getKnown = () => readJSON(KEY_KNOWN, []);
  const setKnown = (arr) => writeJSON(KEY_KNOWN, arr);

  const ingestTags = (items) => {
    const next = new Set(getKnown());
    let added = false;
    items.forEach(i => (i.tags || []).forEach(t => {
      if (typeof t === "string" && t.length > 0 && !next.has(t)) {
        next.add(t);
        added = true;
      }
    }));
    if (added) {
      setKnown([...next].sort((a, b) => a.localeCompare(b)));
      renderWidget();
    }
  };

  const filterByTags = (items, selected) => {
    if (selected.length === 0) return items;
    const wanted = new Set(selected);
    return items.filter(i => (i.tags || []).some(t => wanted.has(t)));
  };

  // --- fetch hook ---
  // ExpenseOwl's pages all call fetch('/expenses') to get the raw array
  // and then aggregate client-side. We intercept that response, narrow
  // it to the selected tag set, and hand the trimmed array back. The
  // page's own rendering code is untouched.
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string"
      ? input
      : (input && input.url) || "";
    const method = (init && init.method) || (input && input.method) || "GET";

    const response = await origFetch(input, init);

    // Only touch GET /expenses (or /expenses?...). Don't touch the
    // singular /expense/edit, /expense/delete, etc.
    const isExpensesList = /\/expenses(\?|$)/.test(url) && method.toUpperCase() === "GET";
    if (!isExpensesList) return response;

    try {
      const cloned = response.clone();
      const data = await cloned.json();
      const isArray = Array.isArray(data);
      const items = isArray ? data : (Array.isArray(data && data.expenses) ? data.expenses : []);
      ingestTags(items);
      const filtered = filterByTags(items, getSelected());
      const payload = isArray ? filtered : { ...data, expenses: filtered };
      return new Response(JSON.stringify(payload), {
        status: response.status,
        headers: { "Content-Type": "application/json" },
      });
    } catch (e) {
      console.warn("[expense-proxy] filter pass-through:", e);
      return response;
    }
  };

  // --- widget UI ---
  let root = null;
  let expanded = false;

  const esc = (s) => String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const ensureRoot = () => {
    if (root) return;
    root = document.createElement("div");
    root.id = "expense-proxy-widget";
    Object.assign(root.style, {
      position: "fixed",
      top: "12px",
      right: "12px",
      zIndex: "99999",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
      fontSize: "13px",
    });
    document.body.appendChild(root);
  };

  const renderCollapsed = () => {
    const selected = getSelected();
    const known = getKnown();
    const label = selected.length === 0
      ? "Filter: All"
      : `Filter: ${selected.length}/${known.length || selected.length}`;
    const bg = selected.length === 0 ? "rgba(40,40,40,0.88)" : "rgba(56,108,255,0.95)";
    root.innerHTML =
      `<button type="button" id="exp-filter-pill" ` +
      `style="background:${bg};color:#fff;border:none;padding:8px 14px;border-radius:18px;` +
      `cursor:pointer;font-size:13px;font-weight:500;` +
      `box-shadow:0 2px 8px rgba(0,0,0,0.25);">` +
      `${esc(label)}</button>`;
    root.querySelector("#exp-filter-pill").addEventListener("click", () => {
      expanded = true;
      renderWidget();
    });
  };

  const renderExpanded = () => {
    const selected = getSelected();
    const known = getKnown();
    const body = known.length === 0
      ? `<div style="color:#888;font-style:italic;padding:4px 0;">no tags yet — load the dashboard once</div>`
      : known.map(t => {
          const checked = selected.includes(t) ? "checked" : "";
          return (
            `<label style="display:flex;align-items:center;padding:5px 2px;` +
            `cursor:pointer;color:#eee;">` +
            `<input type="checkbox" data-tag="${esc(t)}" ${checked} ` +
            `style="margin-right:8px;cursor:pointer;"/>${esc(t)}</label>`
          );
        }).join("");
    const clearBtn = selected.length > 0
      ? `<button type="button" id="exp-filter-clear" ` +
        `style="margin-top:8px;width:100%;padding:6px;background:#444;color:#fff;` +
        `border:1px solid #555;border-radius:6px;cursor:pointer;font-size:12px;">` +
        `Clear filter</button>`
      : "";
    root.innerHTML =
      `<div style="background:rgba(30,30,30,0.96);color:#eee;border-radius:10px;` +
      `padding:12px 14px;box-shadow:0 4px 16px rgba(0,0,0,0.35);min-width:200px;` +
      `max-height:60vh;overflow:auto;">` +
        `<div style="display:flex;justify-content:space-between;align-items:center;` +
        `margin-bottom:8px;">` +
          `<strong style="font-size:11px;text-transform:uppercase;letter-spacing:0.6px;` +
          `color:#aaa;">Filter by tag (OR)</strong>` +
          `<button type="button" id="exp-filter-close" ` +
          `style="background:none;border:none;cursor:pointer;font-size:20px;` +
          `color:#aaa;padding:0 4px;line-height:1;">×</button>` +
        `</div>` +
        body +
        clearBtn +
      `</div>`;

    root.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", () => {
        const tag = cb.dataset.tag;
        const cur = new Set(getSelected());
        if (cb.checked) cur.add(tag); else cur.delete(tag);
        setSelected([...cur]);
        // Easiest reliable way to re-render BOTH the chart and the
        // table after a filter change: reload. ExpenseOwl's pages
        // re-fetch /expenses on load, and our hook applies the new
        // selection on the way through.
        location.reload();
      });
    });
    const clear = root.querySelector("#exp-filter-clear");
    if (clear) clear.addEventListener("click", () => {
      setSelected([]);
      location.reload();
    });
    root.querySelector("#exp-filter-close").addEventListener("click", () => {
      expanded = false;
      renderWidget();
    });
  };

  const renderWidget = () => {
    ensureRoot();
    if (expanded) renderExpanded(); else renderCollapsed();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderWidget);
  } else {
    renderWidget();
  }
})();

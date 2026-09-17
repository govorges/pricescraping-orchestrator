/* pricescraping-orchestrator panel — shared UI behaviour.
   Sparklines + area charts are hand-drawn SVG (no chart lib). */
(function () {
  "use strict";

  /* ---------- theme ---------- */
  var root = document.documentElement;
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
  }
  try { applyTheme(localStorage.getItem("pso-theme")); } catch (e) {}
  window.toggleTheme = function () {
    var cur = root.getAttribute("data-theme");
    var sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next = cur === "dark" ? "light" : cur === "light" ? "dark" : sysDark ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("pso-theme", next); } catch (e) {}
    document.querySelectorAll("[data-chart]").forEach(redrawChart);
  };

  /* ---------- sparklines ---------- */
  function buildSpark(el) {
    var pts = (el.dataset.spark || "").split(",").map(Number).filter(function (n) { return !isNaN(n); });
    if (pts.length < 2) { el.innerHTML = ""; return; }
    var w = 140, h = 34, p = 3;
    var min = Math.min.apply(null, pts), max = Math.max.apply(null, pts), rng = (max - min) || 1;
    var X = function (i) { return p + i * (w - 2 * p) / (pts.length - 1); };
    var Y = function (v) { return h - p - (v - min) / rng * (h - 2 * p); };
    var line = pts.map(function (v, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); }).join(" ");
    var area = line + " L" + X(pts.length - 1).toFixed(1) + " " + h + " L" + X(0).toFixed(1) + " " + h + " Z";
    el.innerHTML =
      '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path class="sp-area" d="' + area + '"/><path class="sp-line" d="' + line + '"/></svg>';
  }
  window.buildSparks = function (ctx) {
    (ctx || document).querySelectorAll(".spark, .qspark, .nspark").forEach(buildSpark);
  };
  window.buildSparks();
  // Belt-and-suspenders for htmx-polled panels (vitals, node cards): rebuild
  // every sparkline after any htmx swap completes, from a single listener on
  // the document rather than relying on each panel's own inline
  // hx-on::after-swap to be re-wired onto its freshly swapped-in element.
  document.body.addEventListener("htmx:afterSwap", function () { window.buildSparks(); });

  /* ---------- area / line chart ----------
     series: [{ name, points: [numbers], color }]  (color optional; first gets an area fill)
     opts:   { xlabels: [strings], ref: number|null } */
  var NS = "http://www.w3.org/2000/svg";
  var PALETTE = ["#2f6fed", "#22c55e", "#f5a623", "#ef4444", "#a855f7", "#14b8a6", "#fb923c", "#8b93ab"];

  window.drawLineChart = function (svg, series, opts) {
    if (!svg) return;
    opts = opts || {};
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var vb = svg.viewBox.baseVal, W = vb.width, H = vb.height;
    var m = { l: 48, r: 16, t: 14, b: 26 };
    var iw = W - m.l - m.r, ih = H - m.t - m.b;

    var flat = [];
    series.forEach(function (s) { s.points.forEach(function (v) { if (v != null) flat.push(v); }); });
    if (opts.ref != null) flat.push(opts.ref);
    if (!flat.length) { showEmpty(); return; }
    var lo = Math.min.apply(null, flat), hi = Math.max.apply(null, flat);
    var pad = (hi - lo) * 0.15 || Math.max(1, hi * 0.05);
    lo -= pad; hi += pad;
    var n = series[0].points.length;
    var X = function (i) { return m.l + (n < 2 ? iw / 2 : i * iw / (n - 1)); };
    var Y = function (v) { return m.t + ih - (v - lo) / (hi - lo) * ih; };

    function add(tag, attrs, cls) {
      var e = document.createElementNS(NS, tag);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (cls) e.setAttribute("class", cls);
      svg.appendChild(e);
      return e;
    }
    var ticks = 4;
    for (var t = 0; t <= ticks; t++) {
      var val = lo + (hi - lo) * t / ticks, y = Y(val);
      add("line", { x1: m.l, x2: m.l + iw, y1: y.toFixed(1), y2: y.toFixed(1) }, "grid-line");
      add("text", { x: m.l - 8, y: (y + 3).toFixed(1), "text-anchor": "end" }, "axis-txt").textContent =
        (opts.money === false ? "" : "$") + Math.round(val).toLocaleString();
    }
    (opts.xlabels || []).forEach(function (s, i) {
      var arr = opts.xlabels;
      var x = m.l + (arr.length < 2 ? iw / 2 : i * iw / (arr.length - 1));
      add("text", { x: x.toFixed(1), y: H - 8,
        "text-anchor": i === 0 ? "start" : i === arr.length - 1 ? "end" : "middle" }, "axis-txt").textContent = s;
    });
    if (opts.ref != null) {
      var ry = Y(opts.ref);
      add("line", { x1: m.l, x2: m.l + iw, y1: ry.toFixed(1), y2: ry.toFixed(1) }, "ref");
      add("text", { x: m.l + iw, y: (ry - 5).toFixed(1), "text-anchor": "end" }, "ref-txt").textContent =
        (opts.refLabel || "reference") + " $" + Math.round(opts.ref).toLocaleString();
    }
    series.forEach(function (s, si) {
      var color = s.color || PALETTE[si % PALETTE.length];
      var d = "", started = false;
      s.points.forEach(function (v, i) {
        if (v == null) return;
        d += (started ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1) + " ";
        started = true;
      });
      if (!started) return;
      if (si === 0) {
        var fi = 0; while (s.points[fi] == null) fi++;
        var li = s.points.length - 1; while (s.points[li] == null) li--;
        add("path", { d: d + "L" + X(li).toFixed(1) + " " + (m.t + ih) + " L" + X(fi).toFixed(1) + " " + (m.t + ih) + " Z",
          fill: color }, "area");
      }
      add("path", { d: d.trim(), stroke: color }, "line");
      var last = s.points.length - 1; while (s.points[last] == null && last > 0) last--;
      if (s.points[last] != null)
        add("circle", { cx: X(last).toFixed(1), cy: Y(s.points[last]).toFixed(1), r: 3.5, fill: color }, "end-dot");
    });

    function showEmpty() {
      var e = document.createElementNS(NS, "text");
      e.setAttribute("x", W / 2); e.setAttribute("y", H / 2); e.setAttribute("text-anchor", "middle");
      e.setAttribute("class", "axis-txt");
      e.textContent = opts.emptyText || "no data yet";
      svg.appendChild(e);
    }
  };

  function redrawChart(svg) {
    var fn = svg._redraw;
    if (typeof fn === "function") fn();
  }
  window.registerChart = function (svg, fn) { svg._redraw = fn; svg.setAttribute("data-chart", ""); fn(); };

  /* ---------- inline edit-row toggles (categories / vendors tables) ---------- */
  window.toggleDetails = function (id) {
    var d = document.getElementById(id);
    if (!d) return;
    d.open = !d.open;
    if (d.open) d.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  /* ---------- slide-over ---------- */
  function openSlideover(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var scrim = document.getElementById("scrim");
    if (scrim) scrim.hidden = false;
    document.querySelectorAll(".slideover").forEach(function (s) { s.hidden = s.id !== id; });
    var f = el.querySelector("[autofocus], input, select, button");
    if (f) f.focus();
  }
  function closeSlideover() {
    document.querySelectorAll(".slideover").forEach(function (s) { s.hidden = true; });
    var scrim = document.getElementById("scrim");
    if (scrim) scrim.hidden = true;
  }
  window.closeSlideover = closeSlideover;
  document.addEventListener("click", function (e) {
    var open = e.target.closest("[data-slideover]");
    if (open) { e.preventDefault(); openSlideover(open.dataset.slideover); return; }
    if (e.target.closest("[data-slideover-close]") || e.target.id === "scrim") closeSlideover();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeSlideover();
  });

  /* ---------- price-band control ---------- */
  document.querySelectorAll(".band").forEach(function (band) {
    var track = band.querySelector(".band-track");
    var minH = band.querySelector('.band-handle[data-role="min"]');
    var maxH = band.querySelector('.band-handle[data-role="max"]');
    var rangeEl = band.querySelector(".band-range");
    var minOut = band.querySelector('[data-out="min"]');
    var maxOut = band.querySelector('[data-out="max"]');
    var minInput = band.querySelector('input[data-field="min"]');
    var maxInput = band.querySelector('input[data-field="max"]');
    var scaleMax = parseFloat(band.dataset.scaleMax) || 5000;
    var step = parseFloat(band.dataset.step) || 10;
    var state = {
      min: clampPct(pctOf(parseFloat(band.dataset.min) || 0)),
      max: clampPct(pctOf(band.dataset.max === "" || band.dataset.max == null ? scaleMax : parseFloat(band.dataset.max)))
    };
    function pctOf(v) { return v / scaleMax * 100; }
    function valOf(pct) { return Math.round(pct / 100 * scaleMax / step) * step; }
    function clampPct(p) { return Math.max(0, Math.min(100, p)); }
    function render() {
      minH.style.left = state.min + "%";
      maxH.style.left = state.max + "%";
      rangeEl.style.left = state.min + "%";
      rangeEl.style.right = (100 - state.max) + "%";
      var lo = valOf(state.min), hi = valOf(state.max);
      if (minOut) minOut.textContent = "$" + lo.toLocaleString();
      if (maxOut) maxOut.textContent = hi >= scaleMax ? "no cap" : "$" + hi.toLocaleString();
      if (minInput) minInput.value = lo;
      if (maxInput) maxInput.value = hi >= scaleMax ? "" : hi;
    }
    function drag(handle, key) {
      handle.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        handle.setPointerCapture(e.pointerId);
        function move(ev) {
          var r = track.getBoundingClientRect();
          var pct = clampPct((ev.clientX - r.left) / r.width * 100);
          if (key === "min") pct = Math.min(pct, state.max - 3);
          else pct = Math.max(pct, state.min + 3);
          state[key] = pct; render();
        }
        function up() { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up); }
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
      });
    }
    if (minH && maxH) { drag(minH, "min"); drag(maxH, "max"); render(); }
  });

  /* ---------- chip editors (sync to a hidden textarea, newline-joined) ---------- */
  document.querySelectorAll("[data-chipeditor]").forEach(function (box) {
    var store = document.querySelector(box.dataset.chipeditor);
    var extra = box.dataset.chipClass || "";
    function sync() {
      var vals = [];
      box.querySelectorAll(".chip[data-val]").forEach(function (c) { vals.push(c.dataset.val); });
      if (store) store.value = vals.join("\n");
    }
    function addChip(val) {
      val = (val || "").trim();
      if (!val) return;
      var c = document.createElement("span");
      c.className = "chip " + extra;
      c.dataset.val = val;
      c.innerHTML = "";
      c.append(document.createTextNode(val + " "));
      var b = document.createElement("button");
      b.type = "button"; b.textContent = "×"; b.setAttribute("aria-label", "Remove " + val);
      b.addEventListener("click", function () { c.remove(); sync(); });
      c.appendChild(b);
      box.insertBefore(c, box.querySelector(".chip-input, .chip-add"));
      sync();
    }
    box.querySelectorAll(".chip[data-val] button").forEach(function (b) {
      b.addEventListener("click", function () { b.closest(".chip").remove(); sync(); });
    });
    var input = box.querySelector(".chip-input");
    if (input) {
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addChip(input.value); input.value = ""; }
      });
      input.addEventListener("blur", function () { if (input.value.trim()) { addChip(input.value); input.value = ""; } });
    }
    sync();
  });

  /* ---------- steppers (generic) ---------- */
  document.querySelectorAll(".stepper").forEach(function (s) {
    var target = s.querySelector(".v, input");
    var btns = s.querySelectorAll("button");
    if (!target || btns.length < 2) return;
    function get() { return parseInt(target.textContent || target.value || "0", 10) || 0; }
    function set(v) { v = Math.max(0, v); if (target.value !== undefined && target.tagName === "INPUT") target.value = v; else target.textContent = v; }
    btns[0].addEventListener("click", function (e) { e.preventDefault(); set(get() - 1); });
    btns[1].addEventListener("click", function (e) { e.preventDefault(); set(get() + 1); });
  });

  /* ---------- fade to the next page on navigation ----------
     Every link click here is a real full-page navigation (no client-side
     router), so the only way to fade "out" is: intercept the click, let the
     CSS opacity transition play, then navigate for real. Skipped entirely
     when the browser already does its own cross-document view-transition
     (Chromium) -- this is a fallback for everyone else, not a second effect
     layered on top. Registered last so e.defaultPrevented correctly reflects
     clicks the slide-over handler above already claimed. */
  if (!("startViewTransition" in document)) {
    var FADE_MS = 100;
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest("a[href]");
      if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
      var url;
      try { url = new URL(a.href, location.href); } catch (err) { return; }
      if (url.origin !== location.origin) return;
      if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
      e.preventDefault();
      document.body.classList.add("is-leaving");
      setTimeout(function () { location.href = a.href; }, FADE_MS);
    });
    // A page restored from the back/forward cache keeps its old DOM state --
    // without this it could still show mid-fade-out (opacity: 0) forever.
    window.addEventListener("pageshow", function () {
      document.body.classList.remove("is-leaving");
    });
  }
})();

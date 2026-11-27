#!/usr/bin/env python3
"""
Generated page extras for the Flamingo & Cactus site.

Three pieces live here, all rendered at build time so the published pages
stay static and the page bodies in data/pages/ stay editable:

  * the Financials dashboard: the Flank ledger is parsed once, reduced to
    public amounts and dates, and embedded as JSON. finance.js turns it
    into two Chart.js charts (balance over time, month by month) with a
    month filter and running totals. No notes are ever embedded.
  * the live GitHub repository strip used on Products and the home page.
    The build writes the shell; the shared site.js fills in stars, open
    issues, the latest commit and the newest release from GitHub's public
    API.
  * the Giscus-ting forum: GitHub Discussions comments in a new tab.

Stdlib only. The ledger parser itself stays in build_flank.py so there is
one definition of a transaction, one date parser and one money parser.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import sys
from pathlib import Path

CHART_VERSION = "4.4.7"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# ---------------------------------------------------------------------------
# money / dates
# ---------------------------------------------------------------------------
def money(value) -> str:
    """$1,234.50 (or -$1,234.50) -- the same shape the ledger uses."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return ("-" if number < 0 else "") + "${:,.2f}".format(abs(number))


def _month_key(iso: str) -> str:
    return (iso or "")[:7]


def _month_label(key: str) -> str:
    try:
        return _dt.datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return key


def _month_short(key: str) -> str:
    try:
        return _dt.datetime.strptime(key, "%Y-%m").strftime("%b %y")
    except ValueError:
        return key


def _month_range(first: str, last: str) -> list[str]:
    try:
        fy, fm = (int(x) for x in first.split("-"))
        ly, lm = (int(x) for x in last.split("-"))
    except ValueError:
        return []
    out = []
    y, m = fy, fm
    while (y, m) <= (ly, lm) and len(out) < 600:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


# ---------------------------------------------------------------------------
# Financials
# ---------------------------------------------------------------------------
def load_finance(ledger_path: Path) -> dict:
    """Parse the ledger into the small, public shape the charts need.

    Only amounts, transaction numbers and dates are kept. Private notes are
    already ciphertext in the repo copy and are not copied here at all.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_flank  # local import: build_flank owns the ledger parser

    records, _filled = build_flank.parse_records(Path(ledger_path))
    tx = [r for r in records if not r["empty"]]
    tx.sort(key=lambda r: r["n"])

    running = None
    for r in tx:
        deposit = r.get("deposit") or 0.0
        withdraw = r.get("withdraw") or 0.0
        if r.get("balance") is not None:
            running = round(float(r["balance"]), 2)
        elif running is not None:
            running = round(running + deposit - withdraw, 2)
        r["_run"] = running

    dated = [r for r in tx if r.get("date")]
    keys = _month_range(_month_key(dated[0]["date"]), _month_key(dated[-1]["date"])) if dated else []

    # The ledger records the balance *after* each entry. Before the first
    # entry we can still infer the opening balance from the first amount,
    # so a filtered first month does not start at a misleading $0.
    initial_opening = None
    if tx and tx[0].get("balance") is not None:
        initial_opening = round(
            float(tx[0]["balance"]) + (tx[0].get("withdraw") or 0.0)
            - (tx[0].get("deposit") or 0.0), 2)

    def last_run_before(key: str):
        best = None
        for r in tx:
            if r.get("date") and _month_key(r["date"]) < key and r["_run"] is not None:
                best = r["_run"]
        if best is None and keys and key == keys[0]:
            return initial_opening
        return best

    months = []
    for key in keys:
        in_month = [r for r in tx if r.get("date") and _month_key(r["date"]) == key]
        opening = last_run_before(key)
        closing = opening
        for r in reversed(in_month):
            if r["_run"] is not None:
                closing = r["_run"]
                break
        deposited = round(sum((r.get("deposit") or 0.0) for r in in_month), 2)
        withdrawn = round(sum((r.get("withdraw") or 0.0) for r in in_month), 2)
        months.append({
            "key": key,
            "label": _month_label(key),
            "short": _month_short(key),
            "deposit": deposited,
            "withdraw": withdrawn,
            "net": round(deposited - withdrawn, 2),
            "opening": opening if opening is not None else 0.0,
            "close": closing if closing is not None else 0.0,
            "count": len(in_month),
        })

    undated = [r for r in tx if not r.get("date")]
    total_in = round(sum((r.get("deposit") or 0.0) for r in tx), 2)
    total_out = round(sum((r.get("withdraw") or 0.0) for r in tx), 2)
    closing = next((r["_run"] for r in reversed(tx) if r["_run"] is not None), 0.0)

    public_tx = [{
        "n": r["n"],
        "date": r.get("date"),
        "dateText": r.get("dateText") or "",
        "deposit": round(float(r.get("deposit") or 0.0), 2),
        "withdraw": round(float(r.get("withdraw") or 0.0), 2),
        "balance": r["_run"],
    } for r in tx]

    return {
        "tx": public_tx,
        "months": months,
        "totals": {
            "deposit": total_in,
            "withdraw": total_out,
            "net": round(total_in - total_out, 2),
            "close": round(float(closing), 2),
            "count": len(tx),
        },
        "undated": {
            "count": len(undated),
            "deposit": round(sum((r.get("deposit") or 0.0) for r in undated), 2),
            "withdraw": round(sum((r.get("withdraw") or 0.0) for r in undated), 2),
        },
        "range": {
            "first": dated[0]["dateText"] if dated else "",
            "last": dated[-1]["dateText"] if dated else "",
        },
    }


def finance_section_html(data: dict, asset_version: str = "") -> str:
    """Server-rendered money-in/out summary + JSON + Chart.js."""
    totals = data.get("totals", {})
    months = data.get("months", [])
    undated = data.get("undated", {})
    version = asset_version or "1"

    options = ['<option value="all">All time</option>']
    for m in reversed(months):
        options.append(
            f'<option value="{html.escape(m["key"])}">{html.escape(m["label"])}</option>')

    undated_note = ""
    if undated.get("count") == 1:
        undated_note = " One undated entry is counted in the all-time totals."
    elif undated.get("count"):
        undated_note = (
            f' {undated["count"]} undated entries are counted in the all-time totals.')

    public_months = [{
        "key": m["key"],
        "label": m["label"],
        "short": m["short"],
        "deposit": m["deposit"],
        "withdraw": m["withdraw"],
        "count": m["count"],
    } for m in months]
    payload = json.dumps({
        "months": public_months,
        "totals": {
            "deposit": totals.get("deposit", 0),
            "withdraw": totals.get("withdraw", 0),
            "count": totals.get("count", 0),
        },
        "undated": {"count": undated.get("count", 0)},
        "range": data.get("range", {}),
    }, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    return f'''<section class="fin" id="flank-finance" aria-labelledby="flank-finance-title">
  <div class="fin-head">
    <h2 class="sec" id="flank-finance-title">The ledger at a glance</h2>
    <p>Two numbers, one month at a time. Pick a month to filter the totals; the bars stay
    in view for comparison. Every transaction, date and note is in The Flank.</p>
  </div>
  <div class="fin-controls">
    <label for="fin-month">Month</label>
    <select id="fin-month">{''.join(options)}</select>
    <span class="fin-range" id="fin-range">All time &middot; {totals.get("count", 0)} entries</span>
  </div>
  <div class="fin-tiles" id="fin-tiles">
    <div class="fin-tile"><span class="k">Money in</span><span class="v up" id="fin-in">{money(totals.get("deposit", 0))}</span></div>
    <div class="fin-tile"><span class="k">Money out</span><span class="v down" id="fin-out">{money(totals.get("withdraw", 0))}</span></div>
  </div>
  <div class="fin-chartcard">
    <div class="fin-charthead">
      <h3>Money in and out by month</h3>
      <span>Click a month to filter</span>
    </div>
    <div class="fin-canvas">
      <canvas id="fin-monthly" role="img" aria-label="Bar chart of money in and money out for every month in the ledger">Your browser cannot draw charts. The full ledger is a click away.</canvas>
    </div>
  </div>
  <p class="fin-note" id="fin-note">Money in and money out, grouped by month.{undated_note} The full ledger has every transaction, date, note and running balance.</p>
  <p class="ac fin-cta"><a href="flank/index.html" class="btn">See every transaction in The Flank</a></p>
  <noscript><p class="fin-note">JavaScript is off, so the month filter and chart cannot run. The all-time totals above are still correct, and the full ledger is <a href="flank/index.html">in The Flank</a>.</p></noscript>
  <script type="application/json" id="flank-finance-data">{payload}</script>
  <script src="assets/chart.umd.min.js?v={CHART_VERSION}"></script>
  <script src="assets/finance.js?v={html.escape(version)}"></script>
</section>'''


FINANCE_JS = r'''/* Flamingo & Cactus -- Financials at a glance.
   The ledger data is embedded by scripts/page_extras.py. This file only
   draws the money-in / money-out totals and one monthly bar chart. The
   full transaction list and the balance chart live in The Flank. */
(function () {
  "use strict";

  var dataEl = document.getElementById("flank-finance-data");
  if (!dataEl) { return; }
  var DATA;
  try { DATA = JSON.parse(dataEl.textContent); } catch (err) { return; }
  if (!DATA || !DATA.totals) { return; }

  var monthSelect = document.getElementById("fin-month");
  var charts = [];
  var selected = "all";
  var monthMap = {};
  (DATA.months || []).forEach(function (m) { monthMap[m.key] = m; });

  function byId(id) { return document.getElementById(id); }
  function setText(id, text) {
    var el = byId(id);
    if (el) { el.textContent = text; }
  }
  function money(n) {
    n = Number(n) || 0;
    return (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function tickMoney(n) {
    n = Number(n) || 0;
    var sign = n < 0 ? "-" : "";
    if (Math.abs(n) >= 1000) {
      return sign + "$" + (Math.abs(n) / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "k";
    }
    return sign + "$" + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
  function css(name, fallback) {
    var value = getComputedStyle(document.body).getPropertyValue(name).trim();
    return value || fallback;
  }
  function dark() {
    var theme = document.body.dataset.theme;
    return theme === "ink" || theme === "midnight";
  }
  function palette() {
    var night = dark();
    return {
      night: night,
      ink: css("--ink", night ? "#f5efe0" : "#141414"),
      ink2: css("--ink2", night ? "#ddd2c0" : "#3d3d3d"),
      muted: css("--muted", night ? "#b8ac92" : "#6b6b6b"),
      paper: css("--paper", night ? "#262220" : "#ffffff"),
      rule: css("--rule", night ? "#6b6152" : "#d9d9d9"),
      gold: night ? "#e3b83b" : "#b8860b",
      out: night ? "#ff8a80" : "#9c1f16",
      outFill: night ? "rgba(255,138,128,.72)" : "rgba(156,31,22,.78)"
    };
  }
  function alpha(hex, a) {
    var m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) { return hex; }
    var n = parseInt(m[1], 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }
  function tooltipTheme(p) {
    return {
      backgroundColor: p.paper,
      titleColor: p.ink,
      bodyColor: p.ink2,
      footerColor: p.muted,
      borderColor: p.rule,
      borderWidth: 1,
      padding: 10,
      cornerRadius: 3,
      displayColors: true,
      titleFont: { family: '"Helvetica Neue", Arial, sans-serif', size: 12, weight: "600" },
      bodyFont: { family: 'Georgia, "Times New Roman", serif', size: 12.5 },
      footerFont: { family: '"Helvetica Neue", Arial, sans-serif', size: 11 }
    };
  }
  function baseOptions(p) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: tooltipTheme(p)
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: p.rule },
          ticks: {
            color: p.muted,
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 8,
            font: { family: '"Helvetica Neue", Arial, sans-serif', size: 10.5 }
          }
        },
        y: {
          beginAtZero: false,
          grid: { color: alpha(p.rule, p.night ? 0.55 : 0.75) },
          border: { display: false },
          ticks: {
            color: p.muted,
            maxTicksLimit: 6,
            callback: function (value) { return tickMoney(value); },
            font: { family: '"Helvetica Neue", Arial, sans-serif', size: 10.5 }
          }
        }
      }
    };
  }
  function destroyCharts() {
    charts.forEach(function (chart) { try { chart.destroy(); } catch (err) {} });
    charts = [];
  }
  function renderTiles(month) {
    var inValue = month ? month.deposit : DATA.totals.deposit;
    var outValue = month ? month.withdraw : DATA.totals.withdraw;
    setText("fin-in", money(inValue));
    setText("fin-out", money(outValue));
    setText("fin-range", month
      ? month.label + " \u00b7 " + month.count + (month.count === 1 ? " entry" : " entries")
      : "All time \u00b7 " + DATA.totals.count + " entries");
  }
  function renderMonthly(p) {
    var canvas = byId("fin-monthly");
    if (!canvas) { return; }
    var card = canvas.closest(".fin-chartcard");
    if (!DATA.months.length) { if (card) card.hidden = true; return; }
    var inColors = [];
    var outColors = [];
    var borderColors = [];
    var borderWidths = [];
    DATA.months.forEach(function (m) {
      var active = selected === "all" || selected === m.key;
      inColors.push(active ? p.gold : alpha(p.gold, 0.3));
      outColors.push(active ? p.outFill : alpha(p.out, 0.3));
      borderColors.push(selected === m.key ? p.ink : "transparent");
      borderWidths.push(selected === m.key ? 1.5 : 0);
    });
    var config = {
      type: "bar",
      data: {
        labels: DATA.months.map(function (m) { return m.short; }),
        datasets: [
          {
            label: "Money in",
            data: DATA.months.map(function (m) { return m.deposit; }),
            backgroundColor: inColors,
            borderColor: borderColors,
            borderWidth: borderWidths,
            borderRadius: 3,
            maxBarThickness: 22
          },
          {
            label: "Money out",
            data: DATA.months.map(function (m) { return m.withdraw; }),
            backgroundColor: outColors,
            borderColor: borderColors,
            borderWidth: borderWidths,
            borderRadius: 3,
            maxBarThickness: 22
          }
        ]
      },
      options: baseOptions(p)
    };
    config.options.interaction = { mode: "index", intersect: false };
    config.options.plugins.legend = {
      display: true,
      position: "bottom",
      labels: {
        color: p.ink2,
        usePointStyle: true,
        boxWidth: 8,
        boxHeight: 8,
        padding: 14,
        font: { family: '"Helvetica Neue", Arial, sans-serif', size: 11 }
      }
    };
    config.options.plugins.tooltip.callbacks = {
      title: function (items) {
        var m = DATA.months[items[0].dataIndex];
        return m ? m.label : "";
      },
      label: function (ctx) { return ctx.dataset.label + " " + money(ctx.parsed.y); }
    };
    config.options.scales.y.beginAtZero = true;
    config.options.onClick = function (event, elements) {
      if (!elements || !elements.length) { return; }
      var m = DATA.months[elements[0].index];
      if (!m) { return; }
      selected = selected === m.key ? "all" : m.key;
      if (monthSelect) { monthSelect.value = selected; }
      // The click is still inside Chart.js's event dispatch. Rebuilding
      // synchronously would destroy the chart it is about to finish with,
      // so let the event settle first.
      setTimeout(render, 0);
    };
    config.options.scales.x.ticks.maxTicksLimit = 10;
    var chart = new Chart(canvas.getContext("2d"), config);
    charts.push(chart);
    canvas.setAttribute("aria-label",
      "Monthly money in and money out for " + DATA.months.length + " months. " +
      (selected === "all" ? "All months shown." : "Selected month: " + (monthMap[selected] || {}).label + "."));
  }
  function render() {
    var month = selected === "all" ? null : monthMap[selected];
    renderTiles(month);
    destroyCharts();
    if (!window.Chart) {
      setText("fin-note", "Charts could not load. The full ledger is in The Flank.");
      return;
    }
    renderMonthly(palette());
  }

  if (monthSelect) {
    monthSelect.addEventListener("change", function () {
      selected = monthSelect.value || "all";
      render();
    });
  }
  var observer = new MutationObserver(function (changes) {
    for (var i = 0; i < changes.length; i++) {
      if (changes[i].attributeName === "data-theme") { render(); return; }
    }
  });
  observer.observe(document.body, { attributes: true, attributeFilter: ["data-theme"] });
  render();
})();
'''


# ---------------------------------------------------------------------------
# GitHub repository strip
# ---------------------------------------------------------------------------
GITHUB_MARK = (
    '<svg class="gh-mark" viewBox="0 0 16 16" width="18" height="18" '
    'aria-hidden="true" focusable="false"><path fill="currentColor" '
    'd="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 '
    '0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13'
    '-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07'
    '-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08'
    '-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2'
    ' .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 '
    '2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 '
    '2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>'
)


def repo_stats_html(repo: str, kicker: str = "Open source") -> str:
    """The editable-page marker's replacement: a quiet repository strip."""
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        return ""
    url = "https://github.com/" + repo
    kicker = (kicker or "Open source").strip()[:60] or "Open source"
    return f'''<aside class="gh-repo" data-gh-repo="{html.escape(repo, quote=True)}" aria-label="GitHub repository: {html.escape(repo, quote=True)}">
  <div class="gh-id">
    {GITHUB_MARK}
    <div class="gh-idtext">
      <span class="gh-kicker">{html.escape(kicker)}</span>
      <a class="gh-name" href="{url}" target="_blank" rel="noopener">{html.escape(repo)}</a>
    </div>
  </div>
  <div class="gh-live" aria-live="polite">
    <span class="gh-stat"><b data-gh="stars">&mdash;</b> <span class="gh-unit" data-gh="stars-unit">stars</span></span>
    <a class="gh-stat" href="{url}/issues" target="_blank" rel="noopener"><b data-gh="issues">&mdash;</b> <span class="gh-unit" data-gh="issues-unit">open issues</span></a>
    <span class="gh-stat gh-stat-wide"><span class="gh-k">Latest commit</span><b class="gh-commit" data-gh="commit">Loading&hellip;</b><span class="gh-when" data-gh="commit-when"></span></span>
    <span class="gh-stat gh-stat-wide"><span class="gh-k">Latest release</span><b class="gh-release" data-gh="release">Loading&hellip;</b><span class="gh-when" data-gh="release-when"></span></span>
  </div>
  <div class="gh-actions">
    <a class="gh-link" href="{url}" target="_blank" rel="noopener">View repo <span aria-hidden="true">&rarr;</span></a>
    <a class="gh-link gh-talk" href="giscus.html">Discuss <span aria-hidden="true">&rarr;</span></a>
  </div>
</aside>'''


# ---------------------------------------------------------------------------
# Giscus-ting
# ---------------------------------------------------------------------------
_GISCUS_TEMPLATE = r'''<div class="giscus-wrap" id="giscus-wrap">
  <div class="giscus" id="giscus-host" hidden></div>
  <p class="giscus-loading" id="giscus-loading">Loading the forum&hellip;</p>
  <p class="giscus-fallback" id="giscus-fallback" hidden>The forum could not load here. <a href="__DISCUSSIONS__" target="_blank" rel="noopener">Open Giscus-ting on GitHub</a>.</p>
  <noscript><p class="giscus-fallback">JavaScript is off, so the embedded forum cannot load. <a href="__DISCUSSIONS__" target="_blank" rel="noopener">Open Giscus-ting on GitHub</a>.</p></noscript>
</div>
<script>
(function () {
  "use strict";
  var cfg = __CFG__;
  var host = document.getElementById("giscus-host");
  var fallback = document.getElementById("giscus-fallback");
  var loading = document.getElementById("giscus-loading");
  var wrap = document.getElementById("giscus-wrap");
  if (!host || !cfg) { return; }
  function frameTheme() {
    var t = document.body.dataset.theme;
    return (t === "ink" || t === "midnight") ? "dark" : "light";
  }
  var current = frameTheme();
  var loaded = false;
  function showFallback() {
    if (fallback) { fallback.hidden = false; }
    if (loading) { loading.hidden = true; }
    if (host) { host.hidden = true; }
    if (wrap) {
      wrap.classList.add("is-fallback");
      wrap.classList.remove("is-ready");
    }
  }
  function hideFrame() {
    var frame = host.querySelector("iframe");
    if (frame) { frame.style.display = "none"; }
  }
  window.addEventListener("message", function (event) {
    if (event.origin !== "https://giscus.app") { return; }
    var payload = event.data && event.data.giscus;
    if (!payload) { return; }
    if (payload.discussion) {
      loaded = true;
      if (fallback) { fallback.hidden = true; }
      if (loading) { loading.hidden = true; }
      if (host) { host.hidden = false; }
      if (wrap) {
        wrap.classList.add("is-ready");
        wrap.classList.remove("is-fallback");
      }
      var frame = host.querySelector("iframe");
      if (frame) { frame.style.display = ""; }
    }
    if (payload.error) { showFallback(); hideFrame(); }
  });
  function mount() {
    var s = document.createElement("script");
    s.src = "https://giscus.app/client.js";
    s.async = true;
    s.crossOrigin = "anonymous";
    var attrs = {
      "data-repo": cfg.repo,
      "data-repo-id": cfg.repoId,
      "data-category": cfg.category,
      "data-category-id": cfg.categoryId,
      "data-mapping": cfg.mapping,
      "data-term": cfg.term,
      "data-strict": "0",
      "data-reactions-enabled": "1",
      "data-emit-metadata": "1",
      "data-input-position": "top",
      "data-theme": current,
      "data-lang": "en"
    };
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        s.setAttribute(key, attrs[key]);
      }
    }
    s.onerror = showFallback;
    host.appendChild(s);
    setTimeout(function () {
      if (!loaded) { showFallback(); hideFrame(); }
    }, 8000);
  }
  mount();
  new MutationObserver(function () {
    var next = frameTheme();
    if (next === current) { return; }
    current = next;
    var frame = host.querySelector("iframe.giscus-frame");
    if (frame && frame.contentWindow) {
      frame.contentWindow.postMessage({ giscus: { setConfig: { theme: next } } }, "https://giscus.app");
    }
  }).observe(document.body, { attributes: true, attributeFilter: ["data-theme"] });
})();
</script>'''


def giscus_block(cfg: dict | None) -> str:
    """The Giscus-ting forum embed, or a useful fallback when it is off."""
    cfg = cfg or {}
    repo = str(cfg.get("repo", "")).strip()
    discussions = "https://github.com/" + repo + "/discussions" if REPO_RE.match(repo) else \
        "https://github.com/bearjhartjen/flamingo-site/discussions"
    if not (REPO_RE.match(repo) and cfg.get("repo_id") and cfg.get("category_id")):
        return (
            '<div class="giscus-wrap"><p class="giscus-fallback">The embedded forum is not '
            'configured in this build. <a href="' + html.escape(discussions, quote=True) +
            '" target="_blank" rel="noopener">Open Giscus-ting on GitHub</a>.</p></div>')
    payload = {
        "repo": repo,
        "repoId": str(cfg.get("repo_id", "")),
        "category": str(cfg.get("category", "General")),
        "categoryId": str(cfg.get("category_id", "")),
        "mapping": str(cfg.get("mapping", "number")),
        "term": str(cfg.get("term", "")),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (_GISCUS_TEMPLATE
            .replace("__DISCUSSIONS__", html.escape(discussions, quote=True))
            .replace("__CFG__", encoded))


# ---------------------------------------------------------------------------
# marker injection
# ---------------------------------------------------------------------------
REPO_MARKER_RE = re.compile(
    r"<p[^>]*>\s*\[github-stats:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:\s*\|\s*([^\]\n]{0,60}))?\s*\]\s*</p>", re.I)
REPO_MARKER_LOOSE = re.compile(
    r"\[github-stats:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"(?:\s*\|\s*([^\]\n]{0,60}))?\s*\]", re.I)
FINANCE_MARKER_RE = re.compile(r"<p[^>]*>\s*\[flank-finance\]\s*</p>", re.I)
GISCUS_MARKER_RE = re.compile(r"<p[^>]*>\s*\[giscus\]\s*</p>", re.I)


def inject_blocks(body: str, slug: str, finance_html: str = "",
                  giscus_html: str = "") -> str:
    """Swap the editable markers for generated blocks.

    Markers are plain text in data/pages/*.html, so the owner can move them
    in the builder. If a page loses its marker but is still the page that
    owns a generated block, the block is appended rather than silently
    dropped.
    """
    def repo_sub(match):
        return repo_stats_html(match.group(1), match.group(2) or "Open source")

    used_finance = False
    used_giscus = False
    body = REPO_MARKER_RE.sub(repo_sub, body)
    body = REPO_MARKER_LOOSE.sub(repo_sub, body)

    if finance_html:
        match = FINANCE_MARKER_RE.search(body)
        if match:
            body = body[:match.start()] + finance_html + body[match.end():]
            used_finance = True
        elif "[flank-finance]" in body:
            body = body.replace("[flank-finance]", finance_html)
            used_finance = True
        if slug == "financials" and not used_finance:
            body += "\n" + finance_html
    else:
        body = FINANCE_MARKER_RE.sub("", body).replace("[flank-finance]", "")

    if giscus_html:
        match = GISCUS_MARKER_RE.search(body)
        if match:
            body = body[:match.start()] + giscus_html + body[match.end():]
            used_giscus = True
        elif "[giscus]" in body:
            body = body.replace("[giscus]", giscus_html)
            used_giscus = True
        if slug == "giscus" and not used_giscus:
            body += "\n" + giscus_html
    else:
        body = GISCUS_MARKER_RE.sub("", body).replace("[giscus]", "")

    # No marker text may ever reach a published page.
    body = REPO_MARKER_LOOSE.sub("", body)
    body = body.replace("[flank-finance]", "").replace("[giscus]", "")
    return body

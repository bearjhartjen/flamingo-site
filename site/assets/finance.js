/* Flamingo & Cactus -- Financials at a glance.
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

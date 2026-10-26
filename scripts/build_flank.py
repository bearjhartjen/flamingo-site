#!/usr/bin/env python3
"""
Build the hidden Flank Ledger pages inside the Flamingo & Cactus site.

    python3 scripts/build_flank.py            # parse data/The Flank.md -> site/flank/
    python3 scripts/build_flank.py --check    # build into a temp dir and verify

This is the ledger generator moved over from the old Flank-ledger repo: same
input, same pages, now published as the unlisted site/flank/ directory of
this repo (linked only from the Financials page) instead of its own site.

Repo layout (all paths are resolved relative to this script's location, so
the repo can be checked out anywhere):

    data/The Flank.md   the ledger, synced from ~/Desktop/The Flank.md (input)
    assets/logo.png     the Flamingo & Cactus logo (input, 320x320 PNG,
                        black mark on transparent)
    scripts/build_flank.py  this script
    site/flank/         output: index.html, about.html

The main site builder (scripts/build.py) calls this automatically, so a
normal build already includes the ledger. The root 404.html, favicon.png
and CNAME stay owned by scripts/build.py.

The build is idempotent: same inputs produce byte-identical output.
Stdlib only -- no pip packages, no image libraries.
"""

import argparse
import base64
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATA = ROOT / "data" / "The Flank.md"
DEFAULT_LOGO = ROOT / "assets" / "logo-flank.png"
DEFAULT_OUT = ROOT / "site" / "flank"

# Transactions blurred until the code is entered. Everything else is public.
PRIVATE_TX = frozenset({6, 18, 22, 24, 25, 27, 30, 31, 33, 34, 36, 39, 40, 44,
                        48, 53, 54, 55, 56, 58, 59, 60, 61, 62, 65, 68})


# --------------------------------------------------------------------------- logo
def logo_data_uri(path: Path) -> str:
    """Base64-encode the logo PNG as a data URI.

    The asset is already a black-mark-on-transparent PNG, so it is embedded
    as-is -- no image processing.
    """
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------- parse
def parse_ledger(text: str):
    blocks = re.split(r"^##\s+Transaction\s+(\d+)\s*$", text, flags=re.M)
    txs = []
    for i in range(1, len(blocks), 2):
        num = int(blocks[i])
        fields = {}
        for line in blocks[i + 1].splitlines():
            m = re.match(r"^-\s+\*\*(.+?):\*\*\s*(.*)$", line.strip())
            if m:
                fields[m.group(1).strip().lower()] = m.group(2).strip()
        txs.append((num, fields))
    return txs


def money(raw):
    """(value_or_None, display). Only '$'-prefixed or bare-numeric values are money."""
    if raw is None:
        return None, ""
    s = raw.strip()
    if s == "$":
        return None, ""
    m = re.fullmatch(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", s)
    if m:
        return float(m.group(1).replace(",", "")), s
    return None, s


def parse_date(raw):
    """(iso_or_None, display)."""
    if raw is None:
        return None, ""
    s = raw.strip()
    if not s:
        return None, ""
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        mo, da, yr = (int(x) for x in m.groups())
        try:
            dt = datetime(yr, mo, da)
        except ValueError:
            return None, s
        return dt.strftime("%Y-%m-%d"), dt.strftime("%b %d, %Y")
    if s.lower() == "unknown":
        return None, "Unknown"
    return None, s


def parse_records(data_path: Path):
    """Parse the ledger markdown into the record list embedded in the page."""
    records = []
    for num, f in parse_ledger(data_path.read_text(encoding="utf-8")):
        w_val, w_txt = money(f.get("withdraw of"))
        d_val, d_txt = money(f.get("deposit of"))
        b_val, b_txt = money(f.get("new balance"))
        iso, disp = parse_date(f.get("transaction was made on"))
        notes = f.get("notes", "").replace("\\$", "$")
        empty = w_val is None and d_val is None and b_val is None and not notes
        records.append(
            {
                "n": num,
                "withdraw": w_val,
                "withdrawText": w_txt,
                "deposit": d_val,
                "depositText": d_txt,
                "balance": b_val,
                "balanceText": b_txt,
                "silver": f.get("silver holdings", ""),
                "notes": notes,
                "date": iso,
                "dateText": disp,
                "empty": empty,
                "private": num in PRIVATE_TX,
            }
        )
    filled = [r for r in records if not r["empty"]]
    return records, filled


# --------------------------------------------------------------------------- pages
CSS = r'''  :root{
    --paper:#ffffff;
    --paper2:#fbfaf7;
    --ink:#141414;
    --ink2:#3d3d3d;
    --muted:#6b6b6b;
    --faint:#8c8c8c;
    --rule:#c9c9c9;
    --rule2:#e3e1da;
    --link:#0b2a8c;
    --up:#1c6b3a;
    --upfill:#ffffff;
    --down:#9c1f16;
    --gold:#8a6a12;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:var(--paper);
    color:var(--ink);
    font:15px/1.55 Georgia,"Times New Roman",Times,serif;
    -webkit-font-smoothing:antialiased;
  }
  img{max-width:100%;height:auto}
  .page{max-width:1080px;margin:0 auto;padding:26px 28px 64px}

  /* ---------- main-site link ---------- */
  .mainsite{
    position:fixed;top:14px;left:14px;z-index:50;
    border:1px solid var(--rule);border-radius:6px;background:var(--paper);
    color:var(--ink2);text-decoration:none;
    font:12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;
    padding:8px 12px;box-shadow:0 1px 3px rgba(0,0,0,.12);
  }
  .mainsite:hover{border-color:var(--ink);color:var(--ink)}
  @media print{.mainsite{display:none}}

  /* ---------- masthead ---------- */
  .masthead{text-align:center;padding:6px 0 4px}
  .masthead img{display:block;margin:0 auto;width:96px;height:96px;object-fit:contain}
  h1{
    font-size:34px;font-weight:400;letter-spacing:2.5px;text-transform:uppercase;
    margin:14px 0 2px;
  }
  .tagline{color:var(--muted);font-size:14px;font-style:italic;margin:0 0 18px}
  .hr{height:2px;background:var(--ink);margin:0}
  .hr.thin{height:1px;background:var(--rule)}

  .headline{text-align:center;padding:24px 0 6px}
  .headline .item{margin:0 auto 26px}
  .headline .k{font-size:11px;letter-spacing:2.4px;text-transform:uppercase;color:var(--muted);font-family:"Helvetica Neue",Arial,sans-serif}
  .headline .v{font-size:40px;font-variant-numeric:tabular-nums;line-height:1.15;margin-top:5px}
  .headline .v.gold{color:var(--gold)}
  .headline .n{font-size:12.5px;color:var(--faint);font-family:"Helvetica Neue",Arial,sans-serif}
  .spotflag{
    text-align:right;min-height:16px;
    font:11px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;
    text-transform:uppercase;color:var(--faint);
  }
  a.spotflag{text-decoration:none;color:inherit;display:block}
  a.spotflag:hover #spotflag-txt{text-decoration:underline}
  .spotflag .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#1c6b3a;margin-right:6px;vertical-align:1px}
  .spotflag.off .dot{background:var(--down)}
  .spotflag.old .dot{background:var(--faint)}
  .silverval{font-size:12px;color:var(--faint);font-family:"Helvetica Neue",Arial,sans-serif;margin-top:4px}
  .silverval b{color:var(--muted);font-weight:600}

  .stats{
    text-align:center;color:var(--muted);font-size:13.5px;padding:0 0 20px;
    font-family:"Helvetica Neue",Arial,sans-serif;
  }
  .stats b{color:var(--ink2);font-weight:600}

  /* ---------- chart ---------- */
  .chartwrap{position:relative;margin:6px 0 4px;overflow-x:auto}
  svg.chart{display:block;width:100%;min-width:640px}
  .candle-up{fill:var(--upfill);stroke:var(--up);stroke-width:1.4}
  .candle-dn{fill:var(--down);stroke:var(--down);stroke-width:1.4}
  .wick{stroke:#55585c;stroke-width:1}
  .gl{stroke:var(--rule2);stroke-width:1}
  .avgline{stroke:#1c4fd8;stroke-width:1.6;stroke-dasharray:7 5}
  .mavg{fill:none;stroke:#1c4fd8;stroke-width:2.2;stroke-linejoin:round;stroke-linecap:round}
  .mavgdot{fill:#1c4fd8}
  .avglbl{font:11px "Helvetica Neue",Arial,sans-serif;fill:#1c4fd8;font-weight:600;paint-order:stroke;stroke:#ffffff;stroke-width:3px}
  .leader{stroke:var(--ink);stroke-width:1}
  .axlbl{font:11px "Helvetica Neue",Arial,sans-serif;fill:var(--muted)}
  .axlbl.b{font-weight:600;fill:var(--ink)}
  .tooltip{
    position:absolute;pointer-events:none;background:#fff;border:1px solid var(--ink);
    padding:8px 11px;font:12.5px/1.5 "Helvetica Neue",Arial,sans-serif;color:var(--ink);
    box-shadow:2px 2px 0 rgba(0,0,0,.14);min-width:168px;opacity:0;transition:opacity .08s;
  }
  .tooltip .tt-t{font-weight:700;letter-spacing:.4px;text-transform:uppercase;font-size:11px;color:var(--muted);margin-bottom:4px}
  .tooltip .tt-r{display:flex;justify-content:space-between;gap:18px}
  .tooltip .tt-r span:last-child{font-variant-numeric:tabular-nums}
  .caption{font:12.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint);text-align:center;margin:6px 0 26px}
  .note{
    font:12.5px/1.5 "Helvetica Neue",Arial,sans-serif;color:var(--muted);
    text-align:center;margin:2px auto 14px;max-width:760px;
    border-left:2px solid var(--rule);padding-left:12px;
  }
  .note b{color:var(--ink2)}
  .legend{
    display:flex;gap:22px;justify-content:center;
    font:12px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin:0 0 20px;
  }
  .legend .sw{
    display:inline-block;width:11px;height:11px;border-radius:2px;
    margin-right:6px;vertical-align:-1px;
  }

  /* ---------- toolbar ---------- */
  .toolbar{display:flex;gap:10px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
  input[type=search],select{
    font:16px Georgia,serif;color:var(--ink);background:#fff;
    border:1px solid var(--rule);border-radius:2px;padding:7px 10px;
  }
  input[type=search]{min-width:300px}
  input[type=search]:focus,select:focus{outline:2px solid var(--ink);outline-offset:-1px}
  .btn{
    font:13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;text-transform:uppercase;
    background:#fff;color:var(--ink);border:1px solid var(--ink);border-radius:2px;
    padding:7px 14px;cursor:pointer;
  }
  .btn:hover{background:var(--ink);color:#fff}
  .count{margin-left:auto;font:13px "Helvetica Neue",Arial,sans-serif;color:var(--muted)}

  /* ---------- table ---------- */
  .tablewrap{position:relative;overflow-x:auto}
  table{width:100%;min-width:720px;border-collapse:collapse;font-size:14px}
  thead th{
    font:11.5px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.3px;text-transform:uppercase;
    color:var(--ink);text-align:left;padding:8px 10px;border-bottom:2px solid var(--ink);
    white-space:nowrap;
  }
  thead th.r{text-align:right}
  tbody td{padding:8px 10px;border-bottom:1px solid var(--rule2);vertical-align:top}
  tbody tr:hover{background:var(--paper2)}
  td.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  td.n{color:var(--faint);text-align:right;width:44px;font-variant-numeric:tabular-nums}
  td.date{white-space:nowrap;color:var(--muted)}
  td.silver{white-space:nowrap;color:var(--gold)}
  td.notes{color:var(--ink2);font-size:13.5px;line-height:1.45}
  .with{color:var(--down)}
  .dep{color:var(--up)}
  .bal{font-weight:600}
  .empty-msg{color:var(--muted);text-align:center;padding:26px;font-style:italic}

  /* ---------- lock ---------- */
  table.locked tr.priv td.notes{filter:blur(5px);user-select:none;pointer-events:none}
  .lockbox{margin-top:20px;text-align:center;font:13px "Helvetica Neue",Arial,sans-serif;color:var(--muted)}
  .lockbox input{
    font:14px Georgia,serif;color:var(--ink);background:#fff;text-align:center;letter-spacing:3px;
    border:1px solid var(--rule);border-radius:2px;padding:7px 10px;width:130px;margin:8px 0 0;
  }
  .lockbox input:focus{outline:2px solid var(--ink);outline-offset:-1px}
  .lockbox .btn{margin-left:8px}
  .lockmsg{min-height:18px;font-size:12.5px;color:var(--down);margin-top:6px}

  /* ---------- prose pages (about / 404) ---------- */
  .prose{max-width:760px;margin:8px auto 0;font-size:15px;line-height:1.65}
  .prose h2{
    font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.6px;
    text-transform:uppercase;color:var(--ink2);margin:26px 0 6px;
  }
  .prose p{margin:0 0 12px;color:var(--ink2)}
  .prose a{color:var(--link)}
  .prose code{
    font:12.5px Consolas,"Liberation Mono",Menlo,monospace;
    background:var(--paper2);border:1px solid var(--rule2);border-radius:2px;padding:1px 4px;
  }
  .nf{max-width:640px;margin:38px auto 0;text-align:center}
  .nf .big{
    font-size:30px;font-weight:400;letter-spacing:2px;text-transform:uppercase;
    margin:0 0 10px;
  }
  .nf p{color:var(--ink2);margin:0 0 14px}

  footer{
    margin-top:30px;padding-top:14px;border-top:1px solid var(--rule);
    font:12.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint);text-align:center;
  }
  footer a{color:var(--link)}

  /* ---------- small screens: same ledger, reshaped ---------- */
  @media (max-width:700px){
    body{font-size:14px}
    .page{padding:16px 12px 48px}
    .masthead img{width:72px;height:72px}
    h1{font-size:24px;letter-spacing:1.5px}
    .headline{padding:14px 0 2px}
    .headline .v{font-size:30px}
    .headline .item{margin-bottom:18px}
    .spotflag{font-size:10px}
    .legend{flex-wrap:wrap;gap:10px 16px}
    .toolbar{gap:8px}
    input[type=search]{min-width:0;flex:1 1 100%}
    select{flex:1 1 auto}
    .count{margin-left:0;width:100%;text-align:right}
    .mainsite{top:8px;left:8px;font-size:11px;padding:6px 9px}
    td.notes{font-size:12.5px}
  }

  @media print{
    body{background:#fff;font-size:11pt}
    .page{max-width:none;padding:0}
    .toolbar,.chartwrap,.caption,.btn{display:none}
    thead th{position:static}
    tbody tr{break-inside:avoid}
  }

  /* ---------- midnight dark: carried over from the main site ---------- */
  body[data-theme="midnight"]{
    --paper:#221c2c;
    --paper2:#2b2336;
    --ink:#f0e9f7;
    --ink2:#d9cff0;
    --muted:#b3a8cd;
    --faint:#8b80a6;
    --rule:#4d3c6b;
    --rule2:#3b2d54;
    --link:#d3b8ff;
    --up:#7ee2a8;
    --upfill:#2b2336;
    --down:#ff9d9d;
    --gold:#e3b83b;
  }
  body[data-theme="ink"]{
    --paper:#262220;
    --paper2:#2f2b28;
    --ink:#f5efe0;
    --ink2:#ddd2c0;
    --muted:#b8ac92;
    --faint:#928872;
    --rule:#6b6152;
    --rule2:#544b3e;
    --link:#9db8ff;
    --up:#7ee2a8;
    --upfill:#2f2b28;
    --down:#ff8a80;
    --gold:#d29922;
  }
  body[data-theme="midnight"] .tooltip,body[data-theme="ink"] .tooltip{background:#383046;border-color:#9a87c0;box-shadow:2px 2px 0 rgba(0,0,0,.5)}
  body[data-theme="ink"] .tooltip{background:#3f3830;border-color:#857763}
  body[data-theme="midnight"] input[type=search],body[data-theme="midnight"] select,
  body[data-theme="ink"] input[type=search],body[data-theme="ink"] select{background:#383046;border-color:#9a87c0;color:var(--ink)}
  body[data-theme="ink"] input[type=search],body[data-theme="ink"] select{background:#3f3830;border-color:#857763}
  body[data-theme="midnight"] input[type=search]:focus,body[data-theme="midnight"] select:focus,
  body[data-theme="ink"] input[type=search]:focus,body[data-theme="ink"] select:focus{outline-color:var(--link)}
  body[data-theme="midnight"] .btn,body[data-theme="ink"] .btn{background:#383046;color:var(--ink);border-color:var(--ink2)}
  body[data-theme="ink"] .btn{background:#3f3830;border-color:var(--ink2)}
  body[data-theme="midnight"] .btn:hover{background:var(--ink2);color:#221c2c}
  body[data-theme="ink"] .btn:hover{background:var(--ink2);color:#262220}
  body[data-theme="midnight"] .count,body[data-theme="ink"] .count{color:var(--muted)}
  body[data-theme="midnight"] .wick,body[data-theme="ink"] .wick{stroke:#a89cc2}
  body[data-theme="ink"] .wick{stroke:#b8ac92}
  body[data-theme="midnight"] .avgline,body[data-theme="midnight"] .mavg{stroke:#8fa8ff}
  body[data-theme="midnight"] .mavgdot,body[data-theme="ink"] .mavgdot,body[data-theme="ink"] .avgline,body[data-theme="ink"] .mavg{fill:#8fa8ff;stroke:#8fa8ff}
  body[data-theme="midnight"] .avglbl{fill:#8fa8ff;stroke:#221c2c}
  body[data-theme="ink"] .avglbl{fill:#8fa8ff;stroke:#262220}
  body[data-theme="midnight"] .spotflag .dot,body[data-theme="ink"] .spotflag .dot{background:#7ee2a8}
  body[data-theme="midnight"] .spotflag.off .dot{background:#ff9d9d}
  body[data-theme="ink"] .spotflag.off .dot{background:#ff8a80}
  body[data-theme="midnight"] .leader{stroke:var(--ink2)}
  body[data-theme="ink"] .leader{stroke:var(--ink2)}'''

INDEX = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Flank Ledger &mdash; Flamingo &amp; Cactus</title>
<style>
__CSS__
</style>
<script>/* carries the main site's dark mode over: same key, same origin */
(function(){try{var k="fc-theme";var get=function(){try{return localStorage.getItem(k);}catch(_){return "";}};var set=function(){var t=get();if(t==="midnight"||t==="ink"){document.body.dataset.theme=t;}else delete document.body.dataset.theme;};set();
if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",set);}else{set();}
try{window.addEventListener("storage",function(e){if(e&&e.key===k){if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",set);}else{set();}}});}catch(_){}
} catch(_) {} })();</script>
</head>
<body>
<a class="mainsite" href="../index.html">Head to main site</a>
<div class="page">

  <a class="spotflag" id="spotflag" href="https://gold-api.com/" target="_blank" rel="noopener" title="Live silver spot (XAG/USD per troy oz) — click to check the feed"><span class="dot"></span><span id="spotflag-txt">Ag spot &mdash;</span></a>

  <div class="masthead">
    <img src="__LOGO__" alt="Flamingo and Cactus">
    <h1>The Flank Ledger</h1>
    <p class="tagline">Flamingo &amp; Cactus Group</p>
  </div>
  <div class="hr"></div>

  <div class="headline">
    <div class="item">
      <div class="k">Current Balance</div>
      <div class="v" id="h-balance">&mdash;</div>
      <div class="n" id="h-balance-note"></div>
    </div>
    <div class="item last">
      <div class="k">Silver Holdings</div>
      <div class="v gold" id="h-silver">&mdash;</div>
      <div class="n">physical reserve</div>
      <div class="silverval" id="h-silver-val"></div>
    </div>
  </div>

  <div class="stats" id="stats"></div>
  <div class="hr thin"></div>

  <div class="chartwrap">
    <svg class="chart" id="chart"></svg>
    <div class="tooltip" id="tip"></div>
  </div>
  <p class="note"><b>Note:</b> this graph tracks <b>US dollar holdings</b> only. The silver holdings
  (<span id="note-silver">200g, 2.6oz</span>) are a physical reserve held separately &mdash; not
  included in the plotted line, the moving average, or the totals above. An approximate
  dollar value at the live spot price is shown under Silver Holdings for reference.</p>
  <p class="caption">
    One candle per transaction &mdash; hollow for a transaction that left the balance higher, solid for one that left it lower.
    Hover a candle for detail.
  </p>
  <div class="legend">
    <span><span class="sw" style="background:#ffffff;border:1.4px solid var(--up)"></span>Balance rose</span>
    <span><span class="sw" style="background:var(--down)"></span>Balance fell</span>
    <span><span class="sw" style="height:0;border-top:2px solid #1c4fd8;border-radius:0;width:18px"></span>Moving average (last&nbsp;10&nbsp;days)</span>
  </div>
  <div class="hr thin"></div>

  <div style="height:22px"></div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="Search notes, dates, amounts&hellip;">
    <select id="filter">
      <option value="all">All transactions</option>
      <option value="in">Deposits only</option>
      <option value="out">Withdrawals only</option>
      <option value="2026">Year 2026</option>
      <option value="2025">Year 2025</option>
    </select>
    <button class="btn" id="print">Print</button>
    <span class="count" id="count">__COUNT__ shown of __COUNT__</span>
  </div>

  <div class="tablewrap">
  <table id="ledger" class="locked">
    <thead>
       <tr>
         <th class="n">#</th>
         <th>Date</th>
         <th class="r">Withdraw</th>
         <th class="r">Deposit</th>
         <th class="r">Balance</th>
         <th>Silver</th>
         <th>Notes</th>
       </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  </div>

  <div class="lockbox" id="lockbox">
    <div id="lockprompt">Some transactions are locked. Enter the code to reveal them.</div>
    <input type="password" id="code" inputmode="numeric" autocomplete="off" maxlength="16" placeholder="•••••">
    <button class="btn" id="lockbtn">Unlock</button>
    <div class="lockmsg" id="lockmsg"></div>
  </div>
</div>

<script>
const TX = __DATA__;

const num = v => typeof v === "number" ? v : null;
const money = n => n === null || n === undefined
  ? "\u2014"
  : "$" + n.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ------------------------------------------------------------------ summary */
const totals = TX.reduce((a,t)=>{
  a.deposits   += num(t.deposit)  || 0;
  a.withdrawals+= num(t.withdraw) || 0;
  if (t.balance !== null) a.lastBalance = t.balance;
  if (t.silver) a.silver = t.silver;
  a.count++;
  return a;
},{deposits:0, withdrawals:0, lastBalance:null, silver:"", count:0});

const dated = TX.filter(t => t.date);
const lastTx = TX[TX.length-1];

document.getElementById("h-balance").textContent = money(totals.lastBalance);
document.getElementById("h-balance-note").textContent =
  "as of " + (lastTx.dateText || "the last entry");
document.getElementById("h-silver").textContent = totals.silver || "\u2014";
document.getElementById("note-silver").textContent = totals.silver || "none on record";

document.getElementById("stats").innerHTML =
  `<b>${totals.count}</b> transactions &nbsp;&middot;&nbsp; ` +
  (dated.length ? dated[0].dateText + " &ndash; " + dated[dated.length-1].dateText : "undated") +
  ` &nbsp;&middot;&nbsp; deposits <b>${money(totals.deposits)}</b>` +
  ` &nbsp;&middot;&nbsp; withdrawals <b>${money(totals.withdrawals)}</b>`;

/* ------------------------------------------------------------------ silver spot
   Live XAG/USD (per troy oz) from the free, keyless, CORS-open
   api.gold-api.com feed. The site itself stays fully static: the browser
   fetches the price on load and refreshes it every 60 s (plus whenever the
   tab becomes visible again). Offline or blocked, the flag falls back to
   the last price kept in localStorage and says so.
   Holdings strings look like "200g, 2.6oz": grams convert at 31.1034768 g
   per troy oz, and bare "oz" is read as troy oz, the precious-metal unit. */
const G_PER_OZT = 31.1034768;
const SPOT_URL = "https://api.gold-api.com/price/XAG";
const SPOT_EVERY_MS = 60000;

function silverOzt(s){
  if (!s) return 0;
  let ozt = 0, m;
  const grams = s.matchAll(/([\d,]+(?:\.\d+)?)\s*g\b/gi);
  for (m of grams) ozt += parseFloat(m[1].replace(/,/g,"")) / G_PER_OZT;
  const ounces = s.matchAll(/([\d,]+(?:\.\d+)?)\s*oz\b/gi);
  for (m of ounces) ozt += parseFloat(m[1].replace(/,/g,""));
  return ozt;
}
const HOLDINGS_OZT = silverOzt(totals.silver);

function renderSpot(price, mode, atMs){
  const flag = document.getElementById("spotflag");
  const txt  = document.getElementById("spotflag-txt");
  const val  = document.getElementById("h-silver-val");
  if (price === null || !(price > 0)){
    flag.className = "spotflag off";
    txt.textContent = "Ag spot unavailable";
    if (val) val.textContent = "";
    return;
  }
  const p = money(price) + "/ozt";
  if (mode === "live"){
    flag.className = "spotflag";
    /* the clock time proves the refresh happened: spot can sit on one cent
       for hours, and without it a faithful 60 s refresh reads as stuck */
    txt.textContent = "Ag " + p + " \u00b7 live \u00b7 " + new Date().toLocaleTimeString();
  } else {
    flag.className = "spotflag " + (mode === "cached" ? "old" : "off");
    const age = atMs ? Math.max(0, Date.now() - atMs) : null;
    const when = age === null ? "" : age < 90e3 ? " \u00b7 just now"
      : age < 3600e3 ? " \u00b7 " + Math.round(age/60e3) + "m ago"
      : " \u00b7 " + Math.round(age/3600e3) + "h ago";
    txt.textContent = "Ag " + p + (mode === "cached" ? " \u00b7 last seen" + when : when);
  }
  if (val) val.innerHTML = HOLDINGS_OZT > 0
    ? "&#8776; <b>" + money(price * HOLDINGS_OZT) + "</b> at spot " + p
    : "";
}

async function refreshSpot(){
  try {
    const ctl = new AbortController();
    const to = setTimeout(()=>ctl.abort(), 10000);
    const res = await fetch(SPOT_URL, { cache: "no-store", signal: ctl.signal });
    clearTimeout(to);
    if (!res.ok) throw new Error("spot HTTP " + res.status);
    const j = await res.json();
    const price = Number(j.price);
    if (!isFinite(price) || price <= 0) throw new Error("bad spot payload");
    const atMs = Date.parse(j.updatedAt) || Date.now();
    try { localStorage.setItem("flankAgSpot", JSON.stringify({ price, atMs })); } catch(e){}
    renderSpot(price, "live", atMs);
  } catch(e){
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem("flankAgSpot") || "null"); } catch(e2){}
    if (cached && isFinite(cached.price) && cached.price > 0)
      renderSpot(cached.price, "cached", cached.atMs || 0);
    else
      renderSpot(null);
  }
}

(function(){
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem("flankAgSpot") || "null"); } catch(e){}
  if (cached && isFinite(cached.price) && cached.price > 0)
    renderSpot(cached.price, "cached", cached.atMs || 0);
  refreshSpot();
  setInterval(refreshSpot, SPOT_EVERY_MS);
  document.addEventListener("visibilitychange", ()=>{ if (!document.hidden) refreshSpot(); });
})();

/* ------------------------------------------------------------------ candles */
function buildCandles(){
  /* One candle per transaction.
     open  = the balance carried in from the previous transaction
     close = the new balance after this one
     high  = max(open, close), low = min(open, close)
     The ledger itself has two places where the running balance jumps by more
     than the recorded deposit/withdrawal; those candles get flagged. */
  const out = [];
  let prevClose = null;
  for (const t of TX){
    if (t.balance === null) continue;
    const dep = num(t.deposit)  || 0;
    const wd  = num(t.withdraw) || 0;
    const implied = +(t.balance - (dep - wd)).toFixed(2);
    const open  = prevClose === null ? implied : prevClose;
    const close = t.balance;
    const gap   = +(close - open - (dep - wd)).toFixed(2);
    out.push({
      n: t.n, label: "#" + t.n, date: t.dateText || "Unknown", iso: t.date || "",
      open, close,
      high: Math.max(open, close), low: Math.min(open, close),
      deposits: dep, withdrawals: wd,
      gap: Math.abs(gap) > 0.005 ? gap : 0
    });
    prevClose = close;
  }
  return out;
}

const CANDLES = buildCandles();

/* Trailing 10-day window for the blue moving-average line.

   The window is measured in calendar days rather than in transactions, so a
   busy week and a quiet one weigh the same. A burst of five transactions on
   a single day no longer fills the whole window, and a month with nothing
   in it no longer pretends to be an average of the last few old entries.

   For each candle the window is the ten calendar days ending on that
   candle's date -- its own day plus the nine before it -- and the average
   is taken over the ending balance of every transaction dated inside it.

   One ledger entry carries no date. It borrows the date of the entry before
   it, so it keeps a place on the timeline and stays in the averages around
   it.

   Date maths is done in UTC on the YYYY-MM-DD strings, so no local timezone
   can shift a transaction into or out of a window. */
const MA_WINDOW_DAYS = 10;

function dayNumber(iso){
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  if (!m) return null;
  return Date.UTC(+m[1], +m[2]-1, +m[3]) / 86400000;
}

/* every candle gets a day number; undated ones borrow the previous date */
const DAYS = CANDLES.map(c => dayNumber(c.iso));
for (let i = 0; i < DAYS.length; i++){
  if (DAYS[i] !== null) continue;
  const before = DAYS.slice(0, i).reverse().find(d => d !== null);
  const after  = DAYS.slice(i+1).find(d => d !== null);
  DAYS[i] = before !== undefined ? before
           : after  !== undefined ? after
           : null;
}

/* the moving average, plus how many transactions fed it */
const MA = CANDLES.map((c,i)=>{
  const d = DAYS[i];
  if (d === null) return { value: c.close, count: 1 };
  let s = 0, n = 0;
  for (let k = 0; k < CANDLES.length; k++){
    if (DAYS[k] === null) continue;
    const age = d - DAYS[k];                 /* days before this candle's day */
    if (age >= 0 && age < MA_WINDOW_DAYS){ s += CANDLES[k].close; n++; }
  }
  return { value: n ? s/n : c.close, count: n };
});

(function(){
  const svg = document.getElementById("chart");
  const tip = document.getElementById("tip");
  if (!CANDLES.length){ svg.innerHTML = '<text x="10" y="30" class="axlbl">No data</text>'; return; }

  const H = 330, PL = 74, PR = 18, PT = 26, PB = 34;
  let lo = Infinity, hi = -Infinity;
  CANDLES.forEach(c=>{ lo = Math.min(lo,c.low); hi = Math.max(hi,c.high); });
  const pad = (hi-lo)*0.08 || 10;
  lo = Math.max(0, lo-pad); hi = hi+pad;

  function draw(){
    const W = Math.max(640, svg.parentElement.clientWidth);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    svg.style.height = H+"px";

    const plotW = W - PL - PR;
    const step  = plotW / CANDLES.length;
    const bw    = Math.max(3, Math.min(22, step*0.6));
    const cx    = i => PL + step*i + step/2;
    const y     = v => PT + (1-(v-lo)/(hi-lo))*(H-PT-PB);

    let grid = "", labs = "";
    for (let i=0;i<=4;i++){
      const v = lo + (hi-lo)*i/4, yy = y(v);
      grid += `<line class="gl" x1="${PL}" y1="${yy.toFixed(1)}" x2="${W-PR}" y2="${yy.toFixed(1)}"/>`;
      labs += `<text class="axlbl" x="${PL-9}" y="${(yy+4).toFixed(1)}" text-anchor="end">${"$"+Math.round(v).toLocaleString()}</text>`;
    }

    let xlabels = "", body = "";
    CANDLES.forEach((c,i)=>{
      const up = c.close >= c.open;
      const x  = cx(i);
      const yt = y(Math.max(c.open,c.close)), yb = y(Math.min(c.open,c.close));
      const h  = Math.max(1.5, yb-yt);
      body += `<line class="wick" x1="${x.toFixed(1)}" y1="${y(c.high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(c.low).toFixed(1)}"/>`;
      body += `<rect class="${up?"candle-up":"candle-dn"}" x="${(x-bw/2).toFixed(1)}" y="${yt.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}"/>`;
      body += `<rect class="hit" data-i="${i}" x="${(x-step/2).toFixed(1)}" y="${PT}" width="${step.toFixed(1)}" height="${H-PT-PB}" fill="transparent"/>`;
    });

    /* label every Nth candle with its transaction number */
    const every = Math.max(1, Math.ceil(CANDLES.length/14));
    CANDLES.forEach((c,i)=>{
      if (i % every === 0 || i === CANDLES.length-1)
        xlabels += `<text class="axlbl" x="${cx(i).toFixed(1)}" y="${H-10}" text-anchor="middle">${c.label}</text>`;
    });

    /* blue moving-average line following the value of the holdings */
    const maPath = MA.map((m,i)=>({ i, x: cx(i), v: m.value }));
    const maD = maPath.map((p,i)=>(i?"L":"M")+p.x.toFixed(1)+" "+y(p.v).toFixed(1)).join(" ");
    const lastMa = maPath[maPath.length-1];
    const maLine =
      `<path class="mavg" d="${maD}"/>` +
      `<circle class="mavgdot" cx="${lastMa.x.toFixed(1)}" cy="${y(lastMa.v).toFixed(1)}" r="3"/>` +
      `<text class="avglbl" x="${(lastMa.x-8).toFixed(1)}" y="${(y(lastMa.v)-12).toFixed(1)}" text-anchor="end">10-day avg ${money(lastMa.v)}</text>`;

    svg.innerHTML = grid + labs + body + maLine + xlabels;

    /* hover tooltips */
    const hits = svg.querySelectorAll(".hit");
    hits.forEach(el=>{
      el.addEventListener("mouseenter", ()=>{
        const c = CANDLES[+el.dataset.i];
        const up = c.close >= c.open;
        const ma = MA[+el.dataset.i];
        tip.innerHTML =
          `<div class="tt-t">Transaction ${c.n} &mdash; ${c.date}</div>` +
          `<div class="tt-r"><span>Opened at</span><span>${money(c.open)}</span></div>` +
          `<div class="tt-r"><span>Closed at</span><span>${money(c.close)}</span></div>` +
          `<div class="tt-r"><span>Change</span><span style="color:${up?"var(--up)":"var(--down)"}">${(c.close-c.open>=0?"+":"")+money(c.close-c.open)}</span></div>` +
          `<div class="tt-r"><span>10-day avg</span><span style="color:#1c4fd8">${money(ma.value)}</span></div>` +
          `<div class="tt-r"><span>in window</span><span>${ma.count} transaction${ma.count===1?"":"s"}</span></div>` +
          (c.deposits  ? `<div class="tt-r"><span>Deposit</span><span>${money(c.deposits)}</span></div>`  : "") +
          (c.withdrawals?`<div class="tt-r"><span>Withdrawal</span><span>${money(c.withdrawals)}</span></div>`:"") +
          (c.gap ? `<div class="tt-r"><span>Unrecorded</span><span>${money(c.gap)}</span></div>` : "");
        const svgRect = svg.getBoundingClientRect();
        const wrapRect = svg.parentElement.getBoundingClientRect();
        const i = +el.dataset.i;
        const px = svgRect.left - wrapRect.left + cx(i);
        const py = svgRect.top  - wrapRect.top  + y(c.high);
        tip.style.opacity = "1";
        /* park the box on the roomier side of the candle so it never sits
           on top of it, then draw a leader line from the candle to the box */
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        const wrapW = wrapRect.width;
        let left = (cx(i) > W/2) ? px - tw - 16 : px + 16;
        left = Math.max(0, Math.min(wrapW - tw, left));
        const below = py < th + 28;
        const top = Math.max(0, below ? py + 20 : py - th - 14);
        tip.style.left = left + "px";
        tip.style.top  = top + "px";
        let leader = svg.querySelector("#leader");
        if (!leader){
          leader = document.createElementNS("http://www.w3.org/2000/svg","line");
          leader.setAttribute("id","leader");
          leader.setAttribute("class","leader");
          svg.appendChild(leader);
        }
        const ox = svgRect.left - wrapRect.left, oy = svgRect.top - wrapRect.top;
        leader.setAttribute("x1", cx(i).toFixed(1));
        leader.setAttribute("y1", y(c.high).toFixed(1));
        leader.setAttribute("x2", (left + tw/2 - ox).toFixed(1));
        leader.setAttribute("y2", ((below ? top : top + th) - oy).toFixed(1));
      });
    });
    svg.addEventListener("mouseleave", ()=>{
      tip.style.opacity = "0";
      const leader = svg.querySelector("#leader");
      if (leader) leader.remove();
    });
  }

  draw();
  let t;
  window.addEventListener("resize", ()=>{ tip.style.opacity = "0"; clearTimeout(t); t = setTimeout(draw,120); });
})();

/* ------------------------------------------------------------------ table */
const q = document.getElementById("q");
const filter = document.getElementById("filter");
const rows = document.getElementById("rows");
const count = document.getElementById("count");

function visible(){
  const term = q.value.trim().toLowerCase();
  const f = filter.value;
  const locked = document.getElementById("ledger").classList.contains("locked");
  return TX.filter(t=>{
    if (f==="in"  && num(t.deposit)  === null) return false;
    if (f==="out" && num(t.withdraw) === null) return false;
    if ((f==="2025" || f==="2026") && !(t.date && t.date.startsWith(f))) return false;
    if (term){
      /* while locked, a private row's notes stay out of the search index,
         so probing note text cannot confirm a blurred row exists */
      const hay = [t.n,t.dateText,t.silver,t.withdrawText,t.depositText,t.balanceText]
                  .concat((locked && t.private) ? [] : [t.notes])
                  .join(" ").toLowerCase();
      if (!hay.includes(term)) return false;
    }
    return true;
  }).slice().reverse();          /* newest first */
}

function render(){
  const v = visible();
  count.textContent = v.length + " shown of " + TX.length;
  if (!v.length){
    rows.innerHTML = '<tr><td colspan="7" class="empty-msg">No transactions match.</td></tr>';
    return;
  }
  rows.innerHTML = v.map(t=>{
    const w = num(t.withdraw), d = num(t.deposit);
    const wCell = w === null
      ? (t.withdrawText ? '<span class="with">' + esc(t.withdrawText) + "</span>" : "&mdash;")
      : '<span class="with">' + money(w) + "</span>";
    const dCell = d === null
      ? (t.depositText ? '<span style="color:var(--gold)">' + esc(t.depositText) + "</span>" : "&mdash;")
      : '<span class="dep">' + money(d) + "</span>";
    return '<tr' + (t.private ? ' class="priv"' : "") + ">"
      + '<td class="n">' + t.n + "</td>"
      + '<td class="date">' + esc(t.dateText || "—") + "</td>"
      + '<td class="r">' + wCell + "</td>"
      + '<td class="r">' + dCell + "</td>"
      + '<td class="r bal">' + money(t.balance) + "</td>"
      + '<td class="silver">' + esc(t.silver || "—") + "</td>"
      + '<td class="notes">' + esc(t.notes || "") + "</td>"
      + "</tr>";
  }).join("");
}

q.addEventListener("input", render);
filter.addEventListener("change", render);
document.getElementById("print").addEventListener("click", ()=>window.print());
render();

/* ------------------------------------------------------------------ lock
   Only the private transactions (see PRIVATE_TX in the builder) load
   blurred; every other row is public. The code typed at the bottom of the
   page is SHA-256 hashed in the browser and compared against a stored hash,
   so the code itself never appears in the page source. Unlock lasts for
   this browser tab only (sessionStorage): a fresh visit locks again.
   This keeps honest visitors out, not a determined reader of the source. */
const CODE_HASH = "b6568e6a00d2bd0444c92d1de175744c3e55b539f116538f83e96aa4ba5aab58";
const lockTable = document.getElementById("ledger");
const lockprompt = document.getElementById("lockprompt");
const codeInput = document.getElementById("code");
const lockbtn = document.getElementById("lockbtn");
const lockmsg = document.getElementById("lockmsg");

function setLocked(locked){
  lockTable.classList.toggle("locked", locked);
  lockprompt.textContent = locked
    ? "Some transactions are locked. Enter the code to reveal them."
    : "Unlocked for this visit.";
  lockbtn.textContent = locked ? "Unlock" : "Lock";
  codeInput.value = "";
  lockmsg.textContent = "";
  codeInput.style.display = locked ? "" : "none";
  render();
}

async function sha256hex(s){
  if (window.crypto && crypto.subtle){
    try {
      const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
      return [...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,"0")).join("");
    } catch(e){ /* fall through to the pure-JS version */ }
  }
  return jsSha256(s);
}

/* Pure-JS SHA-256, used when crypto.subtle is unavailable. Plain HTTP is
   not a secure context, so browsers withhold the WebCrypto API there and
   the check above would always fail on an http:// page. This computes the
   same digest without it. */
function jsSha256(str){
  const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  let H = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const bytes = unescape(encodeURIComponent(str));
  const bitLen = bytes.length * 8;
  let msg = bytes + "\x80";
  while (msg.length % 64 !== 56) msg += "\x00";
  msg += String.fromCharCode(0, 0, 0, 0,
    (bitLen >>> 24) & 255, (bitLen >>> 16) & 255, (bitLen >>> 8) & 255, bitLen & 255);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  const blocks = Math.ceil(msg.length / 64);
  for (let b = 0; b < blocks; b++){
    const w = new Array(64);
    for (let t = 0; t < 16; t++){
      const o = b * 64 + t * 4;
      w[t] = (msg.charCodeAt(o) << 24) | (msg.charCodeAt(o + 1) << 16) |
             (msg.charCodeAt(o + 2) << 8) | msg.charCodeAt(o + 3);
    }
    for (let t = 16; t < 64; t++){
      const s0 = rotr(w[t-15], 7) ^ rotr(w[t-15], 18) ^ (w[t-15] >>> 3);
      const s1 = rotr(w[t-2], 17) ^ rotr(w[t-2], 19) ^ (w[t-2] >>> 10);
      w[t] = (w[t-16] + s0 + w[t-7] + s1) | 0;
    }
    let [a, b0, c, d, e, f, g, h] = H;
    for (let t = 0; t < 64; t++){
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[t] + w[t]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const mj = (a & b0) ^ (a & c) ^ (b0 & c);
      const t2 = (S0 + mj) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b0; b0 = a; a = (t1 + t2) | 0;
    }
    H = H.map((v, i) => (v + [a, b0, c, d, e, f, g, h][i]) | 0);
  }
  const hex = H.map(v => ("00000000" + (v >>> 0).toString(16)).slice(-8)).join("");
  return hex;
}

async function tryUnlock(){
  lockmsg.textContent = "";
  let hex = "";
  try { hex = await sha256hex(codeInput.value.trim()); }
  catch(e){ lockmsg.textContent = "Wrong code."; return; }
  if (!hex){ lockmsg.textContent = "Wrong code."; return; }
  if (hex === CODE_HASH){
    try { sessionStorage.setItem("flankUnlocked","1"); } catch(e){}
    setLocked(false);
  } else {
    lockmsg.textContent = "Wrong code.";
  }
}

lockbtn.addEventListener("click", ()=>{
  if (lockTable.classList.contains("locked")) tryUnlock();
  else {
    try { sessionStorage.removeItem("flankUnlocked"); } catch(e){}
    setLocked(true);
  }
});
codeInput.addEventListener("keydown", e=>{ if (e.key === "Enter") tryUnlock(); });

(function(){
  let open = false;
  try { open = sessionStorage.getItem("flankUnlocked") === "1"; } catch(e){}
  setLocked(!open);
})();
</script>
</body>
</html>
'''

ABOUT = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About &mdash; The Flank Ledger</title>
<style>
__CSS__
</style>
<script>/* carries the main site's dark mode over: same key, same origin */
(function(){try{var k="fc-theme";var get=function(){try{return localStorage.getItem(k);}catch(_){return "";}};var set=function(){var t=get();if(t==="midnight"||t==="ink"){document.body.dataset.theme=t;}else delete document.body.dataset.theme;};set();
if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",set);}else{set();}
try{window.addEventListener("storage",function(e){if(e&&e.key===k){if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",set);}else{set();}}});}catch(_){}
} catch(_) {} })();</script>
</head>
<body>
<a class="mainsite" href="../index.html">Head to main site</a>
<div class="page">

  <div class="masthead">
    <img src="__LOGO__" alt="Flamingo and Cactus">
    <h1>The Flank Ledger</h1>
    <p class="tagline">Flamingo &amp; Cactus Group &mdash; about this site</p>
  </div>
  <div class="hr"></div>

  <div class="prose">
    <h2>What the Flank Ledger is</h2>
    <p>The Flank Ledger is the running record of the Flamingo &amp; Cactus
    group&rsquo;s money: every deposit and withdrawal, the US dollar balance
    after each transaction, and the group&rsquo;s physical silver holdings.</p>

    <h2>Where the numbers come from</h2>
    <p>The authoritative source is <code>The Flank.md</code>, a markdown file
    kept on the owner&rsquo;s Desktop. This website is generated from it:
    <code>scripts/build_flank.py</code> reads that file and writes the ledger
    pages into <code>site/flank/</code> &mdash; the ledger itself and this
    page &mdash; as part of the main site build. The ledger is a read-only
    viewer with nothing to log into and nothing to edit: when the ledger
    changes, the markdown is edited and the build is re-run. It is plain,
    dependency-free Python &mdash; no packages to install.</p>

    <h2>What the graph plots</h2>
    <p>The plotted figures are <b>US dollar holdings only</b>. There is one
    candlestick per transaction &mdash; hollow when the balance rose, solid
    when it fell &mdash; and the blue line is a moving average of the running
    balance over the <b>ten calendar days</b> ending on each transaction&rsquo;s
    date. The window is counted in days rather than in transactions, so a busy
    week and a quiet one weigh the same. Silver is a physical reserve, tracked
    separately in grams and ounces in the ledger; it is not included in the
    plotted line, the moving average, or the headline totals. The ledger page
    shows an approximate dollar value for it under Silver Holdings, figured
    at the live spot price (troy ounces at XAG/USD) for reference. Where a candle jumps by more than the recorded deposit or
    withdrawal, its tooltip flags the difference as <b>Unrecorded</b>.</p>

    <h2>Where the live silver price comes from</h2>
    <p>The silver spot flag at the top of the ledger page and the approximate
    value under Silver Holdings are fetched live in the browser from the free
    public feed at <a href="https://gold-api.com/">api.gold-api.com</a> (no key, XAG/USD per troy
    ounce), refreshed about once a minute. The flag also stamps the time of
    the last successful refresh, so it reads as alive even while the price
    sits on one cent. If the feed cannot be reached, the
    page shows the last price it saw and says so.</p>

    <h2>The rest of the page</h2>
    <p>Beneath the chart is the full ledger as a table, newest first. The
    search box filters by date, amount, or note text, and the dropdown
    narrows the list to deposits, withdrawals, or a single year. Everything
    on the site is plain static HTML &mdash; the logo and the ledger data are
    embedded directly in the page, so the ledger itself works offline and can be published
    as-is, for example from GitHub Pages. Only the live silver spot flag needs
    a connection; without one it shows the last price seen.</p>

    <h2>Why some transaction notes are blurred</h2>
    <p>Most of the ledger is public, but the notes on __PRIVCOUNT__ transactions
    load blurred, because they name real people. The code box below the table
    reveals those notes for that browser tab only
    &mdash; a fresh visit locks them again. This keeps casual visitors out, not
    someone reading the page source &mdash; the ledger data is still embedded
    in the page.</p>
  </div>

  <footer>
    <a href="index.html">&larr; Back to the Flank Ledger</a><br>
    Built by <code>scripts/build_flank.py</code> from <code>The Flank.md</code>.
  </footer>
</div>
</body>
</html>
'''

def render(template: str, **subs) -> str:
    for key, val in subs.items():
        template = template.replace("__" + key + "__", val)
    return template


# --------------------------------------------------------------------------- build
def build(data_path: Path, logo_path: Path, out_dir: Path):
    """Parse the ledger and write index.html and about.html into out_dir.
    Returns the filled record list. The root 404.html, favicon.png and CNAME
    stay owned by scripts/build.py."""
    records, filled = parse_records(data_path)
    print(
        f"parsed {len(records)} blocks, {len(filled)} filled, "
        f"{len(records) - len(filled)} blank"
    )
    if not filled:
        raise SystemExit(f"no transactions found in {data_path}")
    print("last balance:", filled[-1]["balance"], "| last silver:", filled[-1]["silver"])

    data_json = json.dumps(filled, separators=(",", ":")).replace("</", "<\\/")
    logo_uri = logo_data_uri(logo_path)
    print(f"logo embedded ({len(logo_uri) // 1024} KiB data URI)")

    out_dir.mkdir(parents=True, exist_ok=True)

    privcount = sum(1 for r in filled if r["private"])
    print(f"private transactions: {privcount} of {len(filled)}")

    index_html = render(
        INDEX, CSS=CSS, DATA=data_json, LOGO=logo_uri, COUNT=str(len(filled)),
        PRIVCOUNT=str(privcount),
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    print("wrote", out_dir / "index.html", f"{(out_dir / 'index.html').stat().st_size // 1024} KiB")

    about_html = render(ABOUT, CSS=CSS, LOGO=logo_uri, PRIVCOUNT=str(privcount))
    (out_dir / "about.html").write_text(about_html, encoding="utf-8")
    print("wrote", out_dir / "about.html", f"{(out_dir / 'about.html').stat().st_size // 1024} KiB")

    return filled


def check(data_path: Path, logo_path: Path):
    """Build into a temp dir and verify the output, without writing to site/."""
    tmp = Path(tempfile.mkdtemp(prefix="flank-ledger-check-"))
    try:
        filled = build(data_path, logo_path, tmp)
        n = len(filled)
        index_html = (tmp / "index.html").read_text(encoding="utf-8")
        about_html = (tmp / "about.html").read_text(encoding="utf-8")

        ok = True

        def expect(cond, msg):
            nonlocal ok
            print(("PASS  " if cond else "FAIL  ") + msg)
            ok = ok and cond

        expect(len(index_html) > 0, "index.html is non-empty")
        expect("10-day avg" in index_html, "index.html contains the moving-average label")
        expect(
            "Moving average (last&nbsp;10&nbsp;days)" in index_html,
            "index.html legend says the average covers the last 10 days",
        )
        expect(
            "MA_WINDOW_DAYS = 10" in index_html,
            "index.html window is 10 calendar days",
        )
        expect(
            "MA_WINDOW = 9" not in index_html and "9&nbsp;transactions" not in index_html,
            "index.html no longer uses a 9-transaction window",
        )
        expect(
            f"{n} shown of {n}" in index_html,
            f"index.html shows the transaction count ({n})",
        )
        expect("Unrecorded" in index_html, "index.html keeps the Unrecorded gap flagging")
        expect("9</option>" not in index_html and "2026" in index_html, "index.html keeps the filter dropdown")
        expect("spotflag" in index_html, "index.html has the silver spot flag")
        expect('id="spotflag" href="https://gold-api.com/"' in index_html,
               "spot flag links out to the feed it quotes")
        expect("width=device-width" in index_html, "ledger scales on phones (viewport meta)")
        expect("@media (max-width:" in index_html, "ledger reshapes on small screens")
        expect("overflow-x:auto" in index_html.replace(" ", ""), "wide chart/table scroll on small screens")
        expect("h-silver-val" in index_html, "index.html has the holdings value line")
        expect("api.gold-api.com/price/XAG" in index_html, "index.html fetches the live XAG feed")
        expect("HOLDINGS_OZT" in index_html, "index.html converts holdings to troy oz")
        expect('"leader"' in index_html, "index.html draws a leader line to the tooltip")
        expect("table.locked tr.priv td.notes" in index_html, "index.html blurs private rows' notes when locked")
        expect("table.locked td.notes{" not in index_html, "index.html no longer blurs the whole notes column")
        expect("t.private" in index_html, "index.html flags private rows for the blur")
        expect("flankUnlocked" in index_html, "index.html keeps the unlock to the tab session")
        expect("jsSha256" in index_html, "index.html has a pure-JS SHA-256 fallback (works over plain HTTP)")
        expect("This browser cannot check the code" not in index_html,
               "index.html never refuses to check the code")
        expect('id="lockveil"' not in index_html, "index.html has no blur-explainer veil")
        expect("lockveil" not in index_html, "no veil markup, styles or selectors left on the page")
        expect(logo_path.name == "logo-flank.png", "ledger uses the black logo, not the color masthead")
        expect("b6568e6a00d2bd0444c92d1de175744c3e55b539f116538f83e96aa4ba5aab58" in index_html, "index.html stores the code as a hash")
        expect("73774" not in index_html, "index.html never contains the code in plaintext")
        expect("scripts/build_flank.py" in about_html, "about.html names the build script")
        expect('href="https://gold-api.com/"' in about_html, "about.html links the feed it names")
        expect("Why some transaction notes are blurred" in about_html, "about.html explains the per-transaction blur")
        expect('href="index.html"' in about_html, "about.html links back to index.html")
        expect("US dollar holdings" in about_html, "about.html states the dollar-only scope")
        expect(
            "ten calendar days" in about_html,
            "about.html explains the 10-day moving average",
        )
        expect('href="../index.html"' in index_html, "ledger links back to the main site relatively")
        expect('href="../index.html"' in about_html, "about page links back to the main site relatively")
        expect("bearjhartjen.github.io/Flank-ledger" not in index_html + about_html,
               "no links left to the old standalone ledger site")

        m = re.search(r"const TX = (\[.*\]);\n", index_html)
        expect(m is not None, "embedded ledger data is present")
        if m:
            tx = json.loads(m.group(1))
            expect(len(tx) == n, f"embedded ledger parses back to {n} records")
            priv = sorted(t["n"] for t in tx if t.get("private"))
            expect(priv == sorted(PRIVATE_TX),
                   f"embedded data flags the {len(PRIVATE_TX)} private transactions")

        if not ok:
            raise SystemExit("check FAILED")
        print(f"check passed: all output verified in {tmp}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the hidden Flank Ledger pages from The Flank.md (stdlib only)."
    )
    ap.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help="path to the ledger markdown (default: %(default)s)",
    )
    ap.add_argument(
        "--logo",
        type=Path,
        default=DEFAULT_LOGO,
        help="path to the logo PNG (default: %(default)s)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="output directory (default: %(default)s); ignored by --check",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="build into a temp dir and verify the output instead of writing to --out",
    )
    args = ap.parse_args(argv)

    data_path = args.data.expanduser().resolve()
    logo_path = args.logo.expanduser().resolve()
    if not data_path.is_file():
        raise SystemExit(f"data file not found: {data_path}")
    if not logo_path.is_file():
        raise SystemExit(f"logo file not found: {logo_path}")

    if args.check:
        check(data_path, logo_path)
    else:
        out_dir = args.out.expanduser().resolve()
        build(data_path, logo_path, out_dir)


if __name__ == "__main__":
    main()

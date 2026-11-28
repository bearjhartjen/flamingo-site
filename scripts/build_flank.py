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

The private notes arrive here already encrypted: the Desktop ledger is
synced into data/The Flank.md by scripts/encrypt_flank.py, which replaces
each "Private: yes" note with an [[FCEnc ...]] token (see
scripts/flank_crypto.py). This builder embeds the ciphertext in the page
and the page's lock code decrypts it in the browser (PBKDF2 + AES-CBC via
the Web Crypto API). A private row found with plaintext notes is a hard
build error, never a warning.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flank_crypto as fc

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATA = ROOT / "data" / "The Flank.md"
DEFAULT_LOGO = ROOT / "assets" / "logo-flank.png"
DEFAULT_OUT = ROOT / "site" / "flank"
CHART_SRC = ROOT / "assets" / "chart.umd.min.js"

# Fallback only: which transactions are private when a row carries no
# "- **Private:**" field at all. The field in the ledger is the source of
# truth; scripts/encrypt_flank.py encrypts every "yes" row on sync.
PRIVATE_TX = frozenset({6, 18, 22, 24, 25, 27, 30, 31, 33, 34, 36, 39, 40, 44,
                        48, 53, 54, 55, 56, 58, 59, 60, 61, 62, 65, 68, 74, 76})


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
def parse_records(data_path: Path):
    """Parse the ledger markdown into the record list embedded in the page.

    Private rows must arrive already encrypted (scripts/encrypt_flank.py
    does that on sync): a "Private: yes" row whose Notes still hold
    plaintext is a hard build error, so a stale sync can never publish
    note text -- the repo copy is not allowed to carry it at all.
    """
    records = []
    unflagged = []
    for num, f in fc.parse_ledger(data_path.read_text(encoding="utf-8")):
        w_val, w_txt = money(f.get("withdraw of"))
        d_val, d_txt = money(f.get("deposit of"))
        b_val, b_txt = money(f.get("new balance"))
        iso, disp = parse_date(f.get("transaction was made on"))
        raw_notes = f.get("notes", "")
        flag = fc.private_flag(f.get("private"))
        if flag is None:
            flag = num in PRIVATE_TX
            unflagged.append(num)
        enc = None
        nlen = 0
        notes = ""
        if flag:
            tok = fc.parse_token(raw_notes)
            if tok is None:
                if raw_notes.strip():
                    raise SystemExit(
                        f"transaction {num} is marked private but its notes are "
                        f"plaintext in {data_path.name} -- run "
                        "scripts/encrypt_flank.py before building (the repo copy "
                        "must never hold plaintext notes)"
                    )
                # private with an empty note: nothing to encrypt
            else:
                nlen, iv_b64, ct_b64 = tok
                enc = {"i": iv_b64, "c": ct_b64}
        else:
            notes = raw_notes.replace("\\$", "$")
        empty = w_val is None and d_val is None and b_val is None and not notes and enc is None
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
                "gold": f.get("gold holdings", ""),
                "cash": money(f.get("cash", ""))[0],
                "coin": money(f.get("coin", ""))[0],
                "checking": money(f.get("checking", ""))[0],
                "savings": money(f.get("savings", ""))[0],
                "deepSavings": money(f.get("deep savings", ""))[0],
                "notes": notes,
                "enc": enc,
                "nlen": nlen,
                "date": iso,
                "dateText": disp,
                "empty": empty,
                "private": flag,
            }
        )
    if unflagged:
        print(
            f"note: {len(unflagged)} transaction(s) without a Private: field, "
            f"falling back to the built-in set: {unflagged}"
        )
    filled = [r for r in records if not r["empty"]]
    return records, filled


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

  /* ---------- holdings allocation ---------- */
  .allocation{max-width:720px;margin:2.5rem auto 1rem;padding:1.1rem 0 0;border-top:1px solid #d8d1c2;color:#302c24;font-family:Georgia,"Times New Roman",serif;position:relative}
  .allocation__heading{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;margin-bottom:.8rem}
  .allocation__heading h2{margin:0;color:#514630;font-size:1rem;font-weight:600;letter-spacing:.025em}
  .allocation__heading p{margin:0;color:#70695d;font-size:.88rem}
  .allocation__heading strong{color:#302c24;font-weight:600}
  .allocation__bar{display:flex;width:100%;height:13px;overflow:hidden;border:1px solid #c8c0b0;border-radius:2px;background:#eee9df}
  .allocation__bar .segment{display:block;height:100%;cursor:pointer;transition:filter .12s ease}
  .allocation__bar .segment:hover{filter:brightness(1.12)}
  .segment.gold{background:#8a6a12}
  .segment.silver{background:#aeb1ad}
  .segment.cash{background:#6b8f6b}
  .segment.coin{background:#bd9360}
  .segment.checking{background:#657b8a}
  .segment.savings{background:#8997a1}
  .segment.deep-savings{background:#a7aa8b}
  .allocation__bar .segment + .segment{box-shadow:inset 1px 0 rgba(255,255,255,.65)}
  .allocation__legend{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.55rem 1rem;margin:.85rem 0 0;padding:0;list-style:none;color:#5c5549;font:0.78rem/1.3 Arial,sans-serif}
  .allocation__legend li{display:flex;align-items:center;gap:.4rem;white-space:nowrap;cursor:default}
  .allocation__legend b{margin-left:auto;color:#302c24;font-weight:600}
  .swatch{width:9px;height:9px;flex:0 0 9px;border:1px solid rgba(48,44,36,.12);border-radius:50%}
  .swatch.gold{background:#8a6a12}
  .swatch.silver{background:#aeb1ad}
  .swatch.cash{background:#6b8f6b}
  .swatch.coin{background:#bd9360}
  .swatch.checking{background:#657b8a}
  .swatch.savings{background:#8997a1}
  .swatch.deep-savings{background:#a7aa8b}
  .allocation__tooltip{position:absolute;background:#302c24;color:#f5f1e8;font:12px/1.4 "Helvetica Neue",Arial,sans-serif;padding:6px 9px;border-radius:4px;pointer-events:none;white-space:nowrap;z-index:5;transform:translate(-50%,-130%)}
  @media (max-width:560px){.allocation{margin-top:2rem}.allocation__legend{grid-template-columns:repeat(2,minmax(0,1fr))}}

  .stats{
    text-align:center;color:var(--muted);font-size:13.5px;padding:0 0 20px;
    font-family:"Helvetica Neue",Arial,sans-serif;
  }
  .stats b{color:var(--ink2);font-weight:600}

  /* ---------- charts (Chart.js) ---------- */
  .chartblock{margin:4px 0 18px}
  .charthead{
    display:flex;justify-content:space-between;align-items:baseline;gap:12px;
    margin:0 0 7px;font:11px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);
  }
  .charthead b{
    font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;
    text-transform:none;color:var(--ink2);white-space:nowrap;
  }
  .chartcanvas{position:relative;height:320px}
  .chartcanvas.short{height:230px}
  .chartfallback{
    margin:0 0 16px;padding:10px 12px;border:1px solid var(--rule);
    background:var(--paper2);font:12.5px/1.5 "Helvetica Neue",Arial,sans-serif;
    color:var(--muted);text-align:center;
  }
  .chartfallback[hidden]{display:none}
  @media (max-width:760px){
    .chartcanvas{height:250px}
    .chartcanvas.short{height:210px}
    .charthead{font-size:10px;letter-spacing:1px}
  }
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
    .toolbar,.chartblock,.chartfallback,.caption,.btn{display:none}
    thead th{position:static}
    tbody tr{break-inside:avoid}
  }

  /* the ledger keeps its own look: it never follows the main site's theme */'''

INDEX = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>The Flank Ledger &mdash; Flamingo &amp; Cactus</title>
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="../favicon.png">
<style>
__CSS__
</style>
</head>
<body>
<a class="mainsite" href="../index.html">Head to main site</a>
<div class="page">

  <a class="spotflag" id="spotflag" href="https://gold-api.com/" target="_blank" rel="noopener" title="Live silver spot (XAG/USD per troy oz) — click to check the feed"><span class="dot"></span><span id="spotflag-txt">Ag spot &mdash;</span></a>
  <a class="spotflag" id="goldspotflag" href="https://gold-api.com/" target="_blank" rel="noopener" title="Live gold spot (XAU/USD per troy oz) — click to check the feed"><span class="dot" style="background:#8a6a12"></span><span id="goldspotflag-txt">Au spot &mdash;</span></a>

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
    <div class="item">
      <div class="k">Gold Holdings</div>
      <div class="v gold" id="h-gold">&mdash;</div>
      <div class="n">physical reserve</div>
      <div class="silverval" id="h-gold-val"></div>
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

  <div class="chartblock">
    <div class="charthead"><span>Balance over time</span><b id="ma-note">10-day avg &mdash;</b></div>
    <div class="chartcanvas">
      <canvas id="chart-balance" role="img" aria-label="Line chart of the Flank balance after every transaction, with the 10-day moving average">Your browser cannot draw charts. The ledger table below has every transaction.</canvas>
    </div>
  </div>
  <div class="chartblock">
    <div class="charthead"><span>Month by month</span><span>Money in vs money out</span></div>
    <div class="chartcanvas short">
      <canvas id="chart-monthly" role="img" aria-label="Bar chart of deposits and withdrawals for every month in the ledger">Your browser cannot draw charts. The ledger table below has every transaction.</canvas>
    </div>
  </div>
  <p class="chartfallback" id="chartfallback" hidden>Charts could not load. The ledger table below has every transaction.</p>
  <p class="note"><b>Note:</b> these charts track <b>US dollar holdings</b> only. The silver holdings
  (<span id="note-silver">200g, 2.6oz</span>) are a physical reserve held separately &mdash; not
  included in the plotted lines, the moving average, or the totals above. An approximate
  dollar value at the live spot price is shown under Silver Holdings for reference.</p>
  <p class="caption">
    One point per transaction &mdash; hollow when the balance rose, solid when it fell.
    The blue line is the 10-day moving average. Hover any point for detail.
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
         <th>Gold</th>
         <th>Notes</th>
       </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  </div>

  <div class="lockbox" id="lockbox">
    <div id="lockprompt">Some transactions are locked. Enter the password to reveal them.</div>
    <input type="password" id="code" autocomplete="off" maxlength="64" placeholder="&bull;&bull;&bull;&bull;&bull;">
    <button class="btn" id="lockbtn">Unlock</button>
    <div class="lockmsg" id="lockmsg"></div>
  </div>

  <section class="allocation" id="allocation" aria-labelledby="allocation-title">
    <div class="allocation__heading">
      <h2 id="allocation-title">Holdings allocation</h2>
      <p>Total value <strong id="allocation-total">$0.00</strong></p>
    </div>
    <div class="allocation__bar" id="allocation-bar" role="img" aria-label="Holdings allocation">
      <span class="segment gold" data-cat="gold" style="width:0%"></span>
      <span class="segment silver" data-cat="silver" style="width:0%"></span>
      <span class="segment cash" data-cat="cash" style="width:0%"></span>
      <span class="segment coin" data-cat="coin" style="width:0%"></span>
      <span class="segment checking" data-cat="checking" style="width:0%"></span>
      <span class="segment savings" data-cat="savings" style="width:0%"></span>
      <span class="segment deep-savings" data-cat="deepSavings" style="width:0%"></span>
    </div>
    <ul class="allocation__legend" id="allocation-legend">
      <li data-cat="gold"><span class="swatch gold"></span>Gold <b>0%</b></li>
      <li data-cat="silver"><span class="swatch silver"></span>Silver <b>0%</b></li>
      <li data-cat="cash"><span class="swatch cash"></span>Cash <b>0%</b></li>
      <li data-cat="coin"><span class="swatch coin"></span>Coin <b>0%</b></li>
      <li data-cat="checking"><span class="swatch checking"></span>Checking <b>0%</b></li>
      <li data-cat="savings"><span class="swatch savings"></span>Savings <b>0%</b></li>
      <li data-cat="deepSavings"><span class="swatch deep-savings"></span>Deep savings &middot; 3.65% <b>0%</b></li>
    </ul>
    <div class="allocation__tooltip" id="allocation-tooltip" hidden></div>
  </section>
</div>

<script src="chart.umd.min.js"></script>
<script>
const TX = __DATA__;

const num = v => typeof v === "number" ? v : null;
const money = n => n === null || n === undefined
  ? "\u2014"
  : "$" + n.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* Locked private rows carry no readable text -- only ciphertext. Until the
   password unlocks them they show a blurred placeholder sized to the note's
   real length (nlen), so the locked table keeps the same shape it always had. */
function placeholderText(nlen){
  let s = "";
  const base = "Private note. ";
  while (s.length < nlen) s += base;
  return s.slice(0, nlen);
}
function noteCell(t){
  if (t.notes) return esc(t.notes);
  if (t.enc) return esc(placeholderText(typeof t.nlen === "number" ? t.nlen : 40));
  return "";
}

/* ------------------------------------------------------------------ summary */
const totals = TX.reduce((a,t)=>{
  a.deposits   += num(t.deposit)  || 0;
  a.withdrawals+= num(t.withdraw) || 0;
  if (t.balance !== null) a.lastBalance = t.balance;
  if (t.silver) a.silver = t.silver;
  if (t.gold) a.gold = t.gold;
  if (t.cash !== null && t.cash !== undefined) a.cash = t.cash;
  if (t.coin !== null && t.coin !== undefined) a.coin = t.coin;
  if (t.checking !== null && t.checking !== undefined) a.checking = t.checking;
  if (t.savings !== null && t.savings !== undefined) a.savings = t.savings;
  if (t.deepSavings !== null && t.deepSavings !== undefined) a.deepSavings = t.deepSavings;
  a.count++;
  return a;
},{deposits:0, withdrawals:0, lastBalance:null, silver:"", gold:"", cash:null, coin:null, checking:null, savings:null, deepSavings:null, count:0});

const dated = TX.filter(t => t.date);
const lastTx = TX[TX.length-1];

/* Cash defaults to the ledger balance when no explicit Cash column exists. */
if (totals.cash === null || totals.cash === undefined) totals.cash = totals.lastBalance;

document.getElementById("h-balance").textContent = money(totals.lastBalance);
document.getElementById("h-balance-note").textContent =
  "as of " + (lastTx.dateText || "the last entry");
document.getElementById("h-silver").textContent = totals.silver || "\u2014";
document.getElementById("note-silver").textContent = totals.silver || "none on record";
document.getElementById("h-gold").textContent = totals.gold || "\u2014";

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
let SILVER_PRICE = null;
let GOLD_PRICE = null;
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
const HOLDINGS_GOLD_OZT = silverOzt(totals.gold);

function renderSpot(price, mode, atMs){
  const flag = document.getElementById("spotflag");
  const txt  = document.getElementById("spotflag-txt");
  const val  = document.getElementById("h-silver-val");
  if (price === null || !(price > 0)){
    flag.className = "spotflag off";
    txt.textContent = "Ag spot unavailable";
    if (val) val.textContent = "";
    SILVER_PRICE = null;
    updateAllocation();
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
  SILVER_PRICE = price;
  updateAllocation();
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

/* update silver renderSpot to record price: monkeypatch by editing renderSpot? Instead add assignment inside. */
/* We'll instead modify renderSpot and renderGoldSpot to set globals + call updateAllocation. */

/* ------------------------------------------------------------------ gold spot */
const GOLD_SPOT_URL = "https://api.gold-api.com/price/XAU";

function renderGoldSpot(price, mode, atMs){
  const flag = document.getElementById("goldspotflag");
  const txt  = document.getElementById("goldspotflag-txt");
  const val  = document.getElementById("h-gold-val");
  if (price === null || !(price > 0)){
    flag.className = "spotflag off";
    txt.textContent = "Au spot unavailable";
    if (val) val.textContent = "";
    GOLD_PRICE = null;
    updateAllocation();
    return;
  }
  GOLD_PRICE = price;
  const p = money(price) + "/ozt";
  if (mode === "live"){
    flag.className = "spotflag";
    txt.textContent = "Au " + p + " \u00b7 live \u00b7 " + new Date().toLocaleTimeString();
  } else {
    flag.className = "spotflag " + (mode === "cached" ? "old" : "off");
    const age = atMs ? Math.max(0, Date.now() - atMs) : null;
    const when = age === null ? "" : age < 90e3 ? " \u00b7 just now"
      : age < 3600e3 ? " \u00b7 " + Math.round(age/60e3) + "m ago"
      : " \u00b7 " + Math.round(age/3600e3) + "h ago";
    txt.textContent = "Au " + p + (mode === "cached" ? " \u00b7 last seen" + when : when);
  }
  if (val) val.innerHTML = HOLDINGS_GOLD_OZT > 0
    ? "&#8776; <b>" + money(price * HOLDINGS_GOLD_OZT) + "</b> at spot " + p
    : "";
  updateAllocation();
}

async function refreshGoldSpot(){
  try {
    const ctl = new AbortController();
    const to = setTimeout(()=>ctl.abort(), 10000);
    const res = await fetch(GOLD_SPOT_URL, { cache: "no-store", signal: ctl.signal });
    clearTimeout(to);
    if (!res.ok) throw new Error("spot HTTP " + res.status);
    const j = await res.json();
    const price = Number(j.price);
    if (!isFinite(price) || price <= 0) throw new Error("bad spot payload");
    const atMs = Date.parse(j.updatedAt) || Date.now();
    try { localStorage.setItem("flankAuSpot", JSON.stringify({ price, atMs })); } catch(e){}
    renderGoldSpot(price, "live", atMs);
  } catch(e){
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem("flankAuSpot") || "null"); } catch(e2){}
    if (cached && isFinite(cached.price) && cached.price > 0)
      renderGoldSpot(cached.price, "cached", cached.atMs || 0);
    else
      renderGoldSpot(null);
  }
}

(function(){
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem("flankAuSpot") || "null"); } catch(e){}
  if (cached && isFinite(cached.price) && cached.price > 0)
    renderGoldSpot(cached.price, "cached", cached.atMs || 0);
  refreshGoldSpot();
  setInterval(refreshGoldSpot, SPOT_EVERY_MS);
  document.addEventListener("visibilitychange", ()=>{ if (!document.hidden) refreshGoldSpot(); });
})();

/* ------------------------------------------------------------------ allocation bar */
const ALLOC_CATS = ["gold","silver","cash","coin","checking","savings","deepSavings"];
const ALLOC_LABELS = {gold:"Gold",silver:"Silver",cash:"Cash",coin:"Coin",checking:"Checking",savings:"Savings",deepSavings:"Deep savings"};
let ALLOC_TOTAL = 0;

function allocationValue(cat){
  if (cat === "gold") return (HOLDINGS_GOLD_OZT || 0) * (GOLD_PRICE || 0);
  if (cat === "silver") return (HOLDINGS_OZT || 0) * (SILVER_PRICE || 0);
  if (cat === "cash") return totals.cash || 0;
  if (cat === "coin") return totals.coin || 0;
  if (cat === "checking") return totals.checking || 0;
  if (cat === "savings") return totals.savings || 0;
  if (cat === "deepSavings") return totals.deepSavings || 0;
  return 0;
}

function updateAllocation(){
  if (typeof ALLOC_CATS === "undefined" || !ALLOC_CATS) return;
  const vals = ALLOC_CATS.map(c => ({ cat:c, value: allocationValue(c) }));
  ALLOC_TOTAL = vals.reduce((s,v)=>s+v.value,0);
  const totalEl = document.getElementById("allocation-total");
  if (totalEl) totalEl.textContent = money(ALLOC_TOTAL);
  const bar = document.getElementById("allocation-bar");
  vals.forEach(v => {
    const seg = bar ? bar.querySelector('.segment[data-cat="' + v.cat + '"]') : null;
    const pct = ALLOC_TOTAL > 0 ? (v.value / ALLOC_TOTAL * 100) : 0;
    if (seg){ seg.style.width = pct + "%"; seg.dataset.value = v.value; seg.dataset.pct = pct; }
    const b = document.querySelector('#allocation-legend li[data-cat="' + v.cat + '"] b');
    if (b) b.textContent = pct.toFixed(1) + "%";
  });
  if (bar) bar.setAttribute("aria-label", "Holdings allocation: " + vals.map(v => ALLOC_LABELS[v.cat] + " " + (ALLOC_TOTAL>0 ? (v.value/ALLOC_TOTAL*100).toFixed(0) : 0) + "%").join(", ") + ".");
}

function showAllocationTooltip(seg){
  const tooltip = document.getElementById("allocation-tooltip");
  if (!tooltip) return;
  const cat = seg.dataset.cat;
  const value = parseFloat(seg.dataset.value) || 0;
  const pct = parseFloat(seg.dataset.pct) || 0;
  let extra = "";
  if (cat === "gold") extra = " \u00b7 " + (HOLDINGS_GOLD_OZT || 0).toFixed(4) + " troy oz";
  if (cat === "silver") extra = " \u00b7 " + (HOLDINGS_OZT || 0).toFixed(4) + " troy oz";
  if (cat === "deepSavings") extra = " \u00b7 3.65% APY";
  tooltip.textContent = ALLOC_LABELS[cat] + ": " + money(value) + " (" + pct.toFixed(1) + "%)" + extra;
  tooltip.hidden = false;
  const alloc = document.querySelector(".allocation");
  const r = seg.getBoundingClientRect();
  const ar = alloc.getBoundingClientRect();
  tooltip.style.left = (r.left - ar.left + r.width/2) + "px";
  tooltip.style.top = (r.top - ar.top - 4) + "px";
}

(function(){
  document.querySelectorAll(".allocation__bar .segment").forEach(seg => {
    seg.addEventListener("mouseenter", () => showAllocationTooltip(seg));
    seg.addEventListener("mouseleave", () => {
      const t = document.getElementById("allocation-tooltip");
      if (t) t.hidden = true;
    });
  });
  updateAllocation();
})();

/* ------------------------------------------------------------------ points */
function buildPoints(){
  /* One point per transaction.
     open  = the balance carried in from the previous transaction
     close = the new balance after this one
     high  = max(open, close), low = min(open, close)
     The ledger itself has two places where the running balance jumps by more
     than the recorded deposit/withdrawal; those points get flagged. */
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

const POINTS = buildPoints();

/* Trailing 10-day window for the blue moving-average line.

   The window is measured in calendar days rather than in transactions, so a
   busy week and a quiet one weigh the same. A burst of five transactions on
   a single day no longer fills the whole window, and a month with nothing
   in it no longer pretends to be an average of the last few old entries.

   For each point the window is the ten calendar days ending on that
   point's date -- its own day plus the nine before it -- and the average
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

/* every point gets a day number; undated ones borrow the previous date */
const DAYS = POINTS.map(c => dayNumber(c.iso));
for (let i = 0; i < DAYS.length; i++){
  if (DAYS[i] !== null) continue;
  const before = DAYS.slice(0, i).reverse().find(d => d !== null);
  const after  = DAYS.slice(i+1).find(d => d !== null);
  DAYS[i] = before !== undefined ? before
           : after  !== undefined ? after
           : null;
}

/* the moving average, plus how many transactions fed it */
const MA = POINTS.map((c,i)=>{
  const d = DAYS[i];
  if (d === null) return { value: c.close, count: 1 };
  let s = 0, n = 0;
  for (let k = 0; k < POINTS.length; k++){
    if (DAYS[k] === null) continue;
    const age = d - DAYS[k];                 /* days before this point's day */
    if (age >= 0 && age < MA_WINDOW_DAYS){ s += POINTS[k].close; n++; }
  }
  return { value: n ? s/n : c.close, count: n };
});

/* ------------------------------------------------------------------ charts */
(function(){
  const fallback = document.getElementById("chartfallback");
  const balanceCanvas = document.getElementById("chart-balance");
  const monthlyCanvas = document.getElementById("chart-monthly");
  if (!POINTS.length) { if (fallback) fallback.hidden = false; return; }
  if (!window.Chart || !balanceCanvas || !monthlyCanvas) {
    if (fallback) fallback.hidden = false;
    return;
  }

  const C = {
    ink: "#141414", ink2: "#3d3d3d", muted: "#6b6b6b",
    rule: "#c9c9c9", rule2: "#e3e1da", paper: "#ffffff",
    gold: "#8a6a12", up: "#1c6b3a", down: "#9c1f16", blue: "#1c4fd8"
  };
  Chart.defaults.font.family = '"Helvetica Neue", Arial, sans-serif';
  Chart.defaults.font.size = 11;
  Chart.defaults.color = C.muted;

  const moneyTick = v => "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const signedMoney = n => n === 0 ? money(0) : (n < 0 ? "-" : "+") + money(Math.abs(n));
  const tooltipBase = {
    backgroundColor: C.paper, titleColor: C.ink, bodyColor: C.ink2,
    borderColor: C.ink, borderWidth: 1, padding: 10, cornerRadius: 2,
    titleFont: { family: '"Helvetica Neue", Arial, sans-serif', size: 11, weight: "700" },
    bodyFont: { family: '"Helvetica Neue", Arial, sans-serif', size: 12 },
    displayColors: false
  };

  const lastMa = MA[MA.length - 1];
  const maNote = document.getElementById("ma-note");
  if (maNote && lastMa) maNote.textContent = "10-day avg " + money(lastMa.value);

  const pointBg = POINTS.map(c => c.close >= c.open ? C.paper : C.down);
  const pointBorder = POINTS.map(c => c.close >= c.open ? C.up : C.down);

  new Chart(balanceCanvas.getContext("2d"), {
    type: "line",
    data: {
      labels: POINTS.map(c => c.label),
      datasets: [
        {
          label: "Balance",
          data: POINTS.map(c => c.close),
          borderColor: C.ink,
          backgroundColor: "rgba(201,162,39,.14)",
          borderWidth: 2,
          fill: true,
          tension: 0.18,
          pointRadius: POINTS.length > 60 ? 2.5 : 3.2,
          pointHoverRadius: 5,
          pointBackgroundColor: pointBg,
          pointBorderColor: pointBorder,
          pointBorderWidth: 1.4,
          spanGaps: true
        },
        {
          label: "10-day average",
          data: MA.map(m => m.value),
          borderColor: C.blue,
          borderWidth: 1.8,
          borderDash: [6, 4],
          pointRadius: 0,
          pointHoverRadius: 0,
          fill: false,
          tension: 0.18
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign({}, tooltipBase, {
          filter: item => item.datasetIndex === 0,
          callbacks: {
            title: items => {
              const c = POINTS[items[0].dataIndex];
              return "#" + c.n + " \u00b7 " + c.date;
            },
            label: item => "Close " + money(item.parsed.y),
            afterBody: items => {
              const i = items[0].dataIndex;
              const c = POINTS[i];
              const ma = MA[i];
              const lines = [
                "Opened at " + money(c.open),
                "Change " + signedMoney(+(c.close - c.open).toFixed(2)),
                "10-day avg " + money(ma.value) + " (" + ma.count + " in window)"
              ];
              if (c.deposits) lines.push("Deposit " + money(c.deposits));
              if (c.withdrawals) lines.push("Withdrawal " + money(c.withdrawals));
              if (c.gap) lines.push("Unrecorded " + money(c.gap));
              return lines;
            }
          }
        })
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: C.rule },
          ticks: { color: C.muted, maxTicksLimit: 12, maxRotation: 0, autoSkip: true }
        },
        y: {
          beginAtZero: false,
          grid: { color: C.rule2 },
          border: { display: false },
          ticks: { color: C.muted, maxTicksLimit: 6, callback: moneyTick }
        }
      }
    }
  });

  /* Month buckets for the second chart. Undated entries borrow the date of
     the entry before them, the same rule the moving average uses, so every
     dollar stays in a month. */
  const MONTH_NAMES = ["January","February","March","April","May","June","July",
                       "August","September","October","November","December"];
  const MONTH_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const buckets = {};
  const keys = [];
  POINTS.forEach((c, i) => {
    if (DAYS[i] === null) return;
    const d = new Date(DAYS[i] * 86400000);
    const y = d.getUTCFullYear(), m = d.getUTCMonth() + 1;
    const key = y + "-" + String(m).padStart(2, "0");
    if (!buckets[key]) {
      buckets[key] = { key, y, m, in: 0, out: 0, close: 0, count: 0 };
      keys.push(key);
    }
    const row = buckets[key];
    row.in += c.deposits;
    row.out += c.withdrawals;
    row.close = c.close;
    row.count++;
  });
  keys.sort();
  const MONTHS = [];
  if (keys.length) {
    let y = +keys[0].slice(0, 4), m = +keys[0].slice(5, 7);
    const endY = +keys[keys.length - 1].slice(0, 4);
    const endM = +keys[keys.length - 1].slice(5, 7);
    while (y < endY || (y === endY && m <= endM)) {
      const key = y + "-" + String(m).padStart(2, "0");
      const row = buckets[key] || { key, y, m, in: 0, out: 0, close: 0, count: 0 };
      row.in = +row.in.toFixed(2);
      row.out = +row.out.toFixed(2);
      row.net = +(row.in - row.out).toFixed(2);
      row.label = MONTH_NAMES[m - 1] + " " + y;
      row.short = MONTH_SHORT[m - 1] + " " + String(y).slice(2);
      MONTHS.push(row);
      m++;
      if (m === 13) { m = 1; y++; }
    }
    let prevClose = 0;
    MONTHS.forEach(row => {
      if (!row.count) row.close = prevClose;
      else prevClose = row.close;
    });
  }

  const monthBlock = monthlyCanvas.closest(".chartblock");
  if (!MONTHS.length) {
    if (monthBlock) monthBlock.hidden = true;
    return;
  }

  new Chart(monthlyCanvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: MONTHS.map(row => row.short),
      datasets: [
        {
          label: "Money in",
          data: MONTHS.map(row => row.in),
          backgroundColor: C.gold,
          borderRadius: 2,
          maxBarThickness: 18
        },
        {
          label: "Money out",
          data: MONTHS.map(row => row.out),
          backgroundColor: C.down,
          borderRadius: 2,
          maxBarThickness: 18
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: C.muted, usePointStyle: true, boxWidth: 8, boxHeight: 8,
            padding: 14
          }
        },
        tooltip: Object.assign({}, tooltipBase, {
          displayColors: true,
          callbacks: {
            title: items => MONTHS[items[0].dataIndex].label,
            label: item => item.dataset.label + " " + money(item.parsed.y),
            afterBody: items => {
              const row = MONTHS[items[0].dataIndex];
              return "Net " + money(row.net) + " \u00b7 ending " + money(row.close);
            }
          }
        })
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: C.rule },
          ticks: { color: C.muted, maxTicksLimit: 10, maxRotation: 0, autoSkip: true }
        },
        y: {
          beginAtZero: true,
          grid: { color: C.rule2 },
          border: { display: false },
          ticks: { color: C.muted, maxTicksLimit: 6, callback: moneyTick }
        }
      }
    }
  });
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
      + '<td class="silver">' + esc(t.gold || "—") + "</td>"
      + '<td class="notes">' + noteCell(t) + "</td>"
      + "</tr>";
  }).join("");
}

q.addEventListener("input", render);
filter.addEventListener("change", render);
document.getElementById("print").addEventListener("click", ()=>window.print());
render();

/* ------------------------------------------------------------------ lock
   The private notes are AES-encrypted inside the page data (see
   scripts/flank_crypto.py in the repo). The password typed below is turned
   into an AES-256 key with PBKDF2 (SHA-256, 600000 rounds, fixed public
   salt) and the notes are decrypted right here in the browser -- the
   password itself is never stored in the page or the repo. A wrong
   password fails the padding and length checks, so it reveals nothing.
   The derived key is kept in sessionStorage, so the unlock lasts this
   browser tab only; a fresh visit locks again. Decryption needs the Web
   Crypto API, which browsers only provide in secure contexts (https:// or
   localhost) -- there is no pure-JS fallback, by design. */
const ENC = { salt: "__ENCSALT__", iters: 600000 };  /* salt: base64 */
const lockTable = document.getElementById("ledger");
const lockprompt = document.getElementById("lockprompt");
const codeInput = document.getElementById("code");
const lockbtn = document.getElementById("lockbtn");
const lockmsg = document.getElementById("lockmsg");

function setLocked(locked){
  lockTable.classList.toggle("locked", locked);
  lockprompt.textContent = locked
    ? "Some transactions are locked. Enter the password to reveal them."
    : "Unlocked for this visit.";
  lockbtn.textContent = locked ? "Unlock" : "Lock";
  codeInput.value = "";
  lockmsg.textContent = "";
  codeInput.style.display = locked ? "" : "none";
  if (locked){
    /* re-locking wipes the decrypted notes; they are re-derived from the
       ciphertext the next time the password is entered */
    for (const t of TX) if (t.enc) t.notes = "";
  }
  render();
}

const b64bytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
const bytesB64 = u8 => btoa(String.fromCharCode.apply(null, u8));

async function deriveKeyBits(password){
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: b64bytes(ENC.salt), iterations: ENC.iters },
    material, 256));
}

const importAesKey = raw =>
  crypto.subtle.importKey("raw", raw, { name: "AES-CBC" }, false, ["decrypt"]);

async function decryptNote(key, t){
  const buf = await crypto.subtle.decrypt(
    { name: "AES-CBC", iv: b64bytes(t.enc.i) }, key, b64bytes(t.enc.c));
  const text = new TextDecoder("utf-8", { fatal: true }).decode(buf);
  if (typeof t.nlen === "number" && text.length !== t.nlen)
    throw new Error("decrypted length does not match");
  return text;
}

/* Decrypt the first note as the password test, then the rest. A wrong
   password almost always dies on the PKCS#7 padding; anything that slips
   past that fails the strict UTF-8 decode or the exact length check. */
async function revealWith(rawBits){
  const key = await importAesKey(rawBits);
  const priv = TX.filter(t => t.enc);
  if (priv.length) await decryptNote(key, priv[0]);
  for (const t of priv) t.notes = await decryptNote(key, t);
}

async function tryUnlock(){
  lockmsg.textContent = "";
  if (!(window.crypto && crypto.subtle)){
    lockmsg.textContent =
      "This browser will not decrypt here: unlock needs a secure context (https:// or localhost).";
    return;
  }
  try {
    const rawBits = await deriveKeyBits(codeInput.value);
    await revealWith(rawBits);
    try { sessionStorage.setItem("flankKey", bytesB64(rawBits)); } catch(e){}
    setLocked(false);
  } catch(e){
    lockmsg.textContent = "Wrong password.";
  }
}

lockbtn.addEventListener("click", ()=>{
  if (lockTable.classList.contains("locked")) tryUnlock();
  else {
    try { sessionStorage.removeItem("flankKey"); } catch(e){}
    setLocked(true);
  }
});
codeInput.addEventListener("keydown", e=>{ if (e.key === "Enter") tryUnlock(); });

(async function(){
  let rawBits = null;
  try { const b = sessionStorage.getItem("flankKey"); if (b) rawBits = b64bytes(b); } catch(e){}
  if (!rawBits){ setLocked(true); return; }
  try {
    await revealWith(rawBits);
    setLocked(false);
  } catch(e){
    try { sessionStorage.removeItem("flankKey"); } catch(e2){}
    setLocked(true);
  }
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
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="../favicon.png">
<style>
__CSS__
</style>
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
    kept on the owner&rsquo;s Desktop &mdash; the only copy of the ledger in
    plain text. This website is generated from it:
    <code>scripts/build_flank.py</code> reads that file and writes the ledger
    pages into <code>site/flank/</code> &mdash; the ledger itself and this
    page &mdash; as part of the main site build. On the way in, the notes of
    the private transactions are encrypted
    (<code>scripts/encrypt_flank.py</code>), so the copy in this repository
    and the published page carry those notes only as ciphertext. The ledger
    is a read-only viewer with nothing to log into and nothing to edit: when
    the ledger changes, the markdown is edited and the build is re-run. It is
    plain, dependency-free Python &mdash; no packages to install.</p>

    <h2>What the charts plot</h2>
    <p>The plotted figures are <b>US dollar holdings only</b>. There is one
    point per transaction &mdash; hollow when the balance rose, solid when it
    fell &mdash; and the blue line is a moving average of the running
    balance over the <b>ten calendar days</b> ending on each transaction&rsquo;s
    date. The window is counted in days rather than in transactions, so a busy
    week and a quiet one weigh the same. A second chart groups the same ledger
    by month and shows money in and money out. Silver is a physical reserve, tracked
    separately in grams and ounces in the ledger; it is not included in the
    plotted lines, the moving average, or the headline totals. The ledger page
    shows an approximate dollar value for it under Silver Holdings, figured
    at the live spot price (troy ounces at XAG/USD) for reference. Where a point jumps by more than the recorded deposit or
    withdrawal, its tooltip flags the difference as <b>Unrecorded</b>.</p>

    <h2>Where the live silver and gold prices come from</h2>
    <p>The silver spot flag and the approximate value under Silver Holdings are
    fetched live in the browser from the free public feed at
    <a href="https://gold-api.com/">api.gold-api.com</a> (no key, XAG/USD per troy
    ounce), refreshed about once a minute. A second flag tracks gold (XAU/USD)
    and the approximate value under Gold Holdings the same way. Both flags stamp
    the time of the last successful refresh. If a feed cannot be reached, the
    page shows the last price it saw and says so.</p>

    <h2>Holdings allocation</h2>
    <p>At the bottom of the ledger page, a holdings allocation bar compares the
    total US dollar value of Gold, Silver, Cash, Coin, Checking, Savings and
    Deep savings (the last at 3.65%). Values come from the ledger&rsquo;s per-transaction
    columns plus the live spot prices for gold and silver. Hovering a partition
    shows its dollar value, share and, for the metals, the weight in troy ounces.</p>

    <h2>The rest of the page</h2>
    <p>Beneath the charts is the full ledger as a table, newest first. The
    search box filters by date, amount, or note text, and the dropdown
    narrows the list to deposits, withdrawals, or a single year. Everything
    on the site is plain static HTML &mdash; the logo and the ledger data are
    embedded directly in the page, so the ledger itself works offline and can be published
    as-is, for example from GitHub Pages. Only the live silver spot flag needs
    a connection; without one it shows the last price seen.</p>

    <h2>Why some transaction notes are locked</h2>
    <p>Most of the ledger is public, but the notes on __PRIVCOUNT__ transactions
    name real people. Those notes are not merely hidden &mdash; they are stored
    <b>encrypted</b>. The repo copy of the ledger and this page carry them only
    as AES-256 ciphertext; the readable text exists solely in the owner&rsquo;s
    local copy of the ledger file. Until the password is entered, those rows
    load blurred.</p>
    <p>The box below the table takes the password, runs it through PBKDF2
    (600,000 rounds) in the browser to derive the AES key, and decrypts the
    notes on the spot &mdash; a wrong password simply fails. The unlocked state
    lasts for that browser tab only; a fresh visit locks again. The password is
    never stored in the page, the repo, or the published site. Amounts, dates
    and balances stay public on every row. One honest limit: the protection is
    only as strong as the password behind it &mdash; this stops casual visitors
    and a reader of the page source, not someone willing to brute-force a
    short password offline against the encrypted text.</p>
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
    if CHART_SRC.is_file():
        shutil.copyfile(CHART_SRC, out_dir / "chart.umd.min.js")
    else:
        print(f"warning: {CHART_SRC} is missing; the ledger charts will fall back to the table",
              file=sys.stderr)

    privcount = sum(1 for r in filled if r["private"])
    print(f"private transactions: {privcount} of {len(filled)}")

    index_html = render(
        INDEX, CSS=CSS, DATA=data_json, LOGO=logo_uri, COUNT=str(len(filled)),
        PRIVCOUNT=str(privcount), ENCSALT=fc.SALT_B64,
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
        data_text = data_path.read_text(encoding="utf-8")

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
        expect("chart.umd.min.js" in index_html and (tmp / "chart.umd.min.js").is_file(),
               "the ledger ships its own Chart.js runtime")
        expect("chart-balance" in index_html and "chart-monthly" in index_html
               and "new Chart" in index_html,
               "the ledger draws its balance and monthly charts with Chart.js")
        expect("table.locked tr.priv td.notes" in index_html, "index.html blurs private rows' notes when locked")
        expect("table.locked td.notes{" not in index_html, "index.html no longer blurs the whole notes column")
        expect("t.private" in index_html, "index.html flags private rows for the blur")
        expect("flankKey" in index_html, "index.html keeps the derived key to the tab session")
        expect("PBKDF2" in index_html and "AES-CBC" in index_html,
               "index.html decrypts in the browser with PBKDF2 + AES-CBC")
        expect("CODE_HASH" not in index_html and "jsSha256" not in index_html,
               "the old code-hash lock is fully gone")
        expect("secure context" in index_html,
               "graceful message when WebCrypto is unavailable (plain http)")
        expect("placeholderText" in index_html and "Private note. " in index_html,
               "locked private rows show a blurred placeholder sized to the note")
        expect('const ENC = { salt: "' + fc.SALT_B64 + '"' in index_html,
               "index.html carries the public PBKDF2 salt (base64) for the browser")
        expect('id="lockveil"' not in index_html, "index.html has no blur-explainer veil")
        expect("lockveil" not in index_html, "no veil markup, styles or selectors left on the page")
        expect(logo_path.name == "logo-flank.png", "ledger uses the black logo, not the color masthead")
        # The passphrase itself must never be embedded. Checked against the
        # keyfile rather than spelled out here -- a literal in this source
        # would put the password in the repository, which is exactly what
        # the encryption exists to prevent.
        if fc.KEYFILE.is_file():
            passphrase = fc.load_passphrase()
            for label, text_ in (("index.html", index_html), ("about.html", about_html),
                                 ("the repo ledger", data_text)):
                expect(passphrase not in text_,
                       f"{label} never contains the passphrase in plaintext")
        else:
            print("SKIP  passphrase-absence check (keyfile not present)")
        expect("scripts/build_flank.py" in about_html, "about.html names the build script")
        expect('href="https://gold-api.com/"' in about_html, "about.html links the feed it names")
        expect("Why some transaction notes are locked" in about_html, "about.html explains the encrypted notes")
        expect("AES-256 ciphertext" in about_html, "about.html states the notes are stored encrypted")
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
        tx = None
        if m:
            tx = json.loads(m.group(1))
            expect(len(tx) == n, f"embedded ledger parses back to {n} records")
            priv = sorted(t["n"] for t in tx if t.get("private"))
            expected_priv = sorted(r["n"] for r in filled if r.get("private"))
            expect(priv == expected_priv,
                   f"embedded data flags the {len(expected_priv)} private transactions")
            encd = [t for t in tx if t.get("enc")]
            expect(len(encd) == len(priv),
                   f"every private record carries an encrypted blob ({len(encd)})")
            expect(all(t.get("notes") == "" for t in encd),
                   "no private record embeds readable notes")
            expect(all(isinstance(t.get("nlen"), int) and t["nlen"] > 0 for t in encd),
                   "every encrypted blob carries its note length for the placeholder")

        # The repo copy and the built pages must not contain any private note
        # text. When the Desktop original and the passphrase keyfile are both
        # present, also decrypt every embedded blob and compare it to the
        # original note -- the full roundtrip. Skipped on machines without
        # them (e.g. CI): the build itself never needs the passphrase.
        desktop = Path.home() / "Desktop" / "The Flank.md"
        if not (desktop.is_file() and fc.KEYFILE.is_file()):
            print("SKIP  roundtrip (Desktop copy or passphrase keyfile not present)")
        elif tx is not None:
            src = dict(fc.parse_ledger(desktop.read_text(encoding="utf-8")))
            key = fc.derive_key(fc.load_passphrase())
            rt_fail = 0
            for t in tx:
                if not t.get("enc"):
                    continue
                expected = src.get(t["n"], {}).get("notes", "").replace("\\$", "$")
                try:
                    plain = fc.decrypt_note(
                        key, t["n"], fc.b64_bytes(t["enc"]["i"]), fc.b64_bytes(t["enc"]["c"]))
                except fc.CryptoError as exc:
                    print(f"FAIL  transaction {t['n']}: decryption failed: {exc}")
                    ok = False
                    rt_fail += 1
                    continue
                if plain != expected or len(plain) != t["nlen"]:
                    print(f"FAIL  transaction {t['n']}: decrypted note != Desktop note")
                    ok = False
                    rt_fail += 1
                    continue
                if expected and (expected in data_text or expected in index_html
                                 or expected in about_html):
                    print(f"FAIL  transaction {t['n']}: plaintext note text in the repo copy or built pages")
                    ok = False
                    rt_fail += 1
            if rt_fail == 0:
                expect(True, "every blob decrypts to the exact Desktop note, plaintext nowhere")
            expect(data_text.count("**Private:** yes") == sum(1 for t in tx if t.get("enc")),
                   "the repo ledger marks exactly the encrypted rows private")

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

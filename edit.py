#!/usr/bin/env python3
"""
Flamingo & Cactus website builder. Desktop only, single user.

GoDaddy-style, local-only: a page manager down the left, a live canvas in
the middle, blocks (headings, text, pictures, galleries, buttons, dividers)
and themes. Save rebuilds every page in site/ so a GitKraken commit carries
the whole site (same flow as the Flank ledger).

    python3 edit.py                 # serve on 127.0.0.1:8767 and open browser
    python3 edit.py --no-open       # serve without opening a browser

Stdlib only. Binds to loopback only.
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "site.json"
PAGES = ROOT / "data" / "pages"
LOGO = ROOT / "assets" / "logo.png"
UPLOADS = ROOT / "assets" / "uploads"
SITE = ROOT / "site"

sys.path.insert(0, str(ROOT / "scripts"))
import build as site_build  # noqa: E402


def read_site() -> dict:
    return site_build.load_site(DATA)


def read_body(slug: str) -> str:
    return site_build.read_body(PAGES, slug)


def write_text_atomic(path: Path, text: str) -> None:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=".tmp.", suffix=".tmp", delete=False)
    try:
        tmp.write(text)
        tmp.close()
        # mkstemp-style temp files are 0600; match the repo's 0644 files.
        os.chmod(tmp.name, 0o644)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def git_add(paths) -> str:
    """Stage files if inside a git repo. Never commit. Returns note."""
    if not (ROOT / ".git").exists():
        return "not a git repo, skipped staging"
    rel = [str(Path(p).resolve().relative_to(ROOT.resolve())) for p in paths]
    try:
        subprocess.run(["git", "add", "--"] + rel, cwd=str(ROOT),
                       check=True, capture_output=True, text=True, timeout=30)
        return "staged " + str(len(rel)) + " file(s)"
    except FileNotFoundError:
        return "git not found, skipped staging"
    except subprocess.CalledProcessError as e:
        return "git add failed: " + (e.stderr or e.stdout or "").strip()[-300:]


def rebuild() -> None:
    site_build.build(DATA, PAGES, LOGO, UPLOADS, SITE)


def safe_upload_name(name: str, mime: str | None = None) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "image.png")).strip("-.") or "image.png"
    if mime:
        if "." in base:
            base = base.rsplit(".", 1)[0] or "image"
        ext = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "gif": ".gif"}.get(
            (mime or "").lower().split("/")[-1], ".png")
        base = base + ext
    if "." not in base:
        base += ".png"
    return base[:80]


def live_base() -> str | None:
    """Public URL of the published site: the custom domain when site/CNAME
    exists, otherwise the GitHub Pages project URL from the origin remote."""
    try:
        cname = (SITE / "CNAME").read_text(encoding="utf-8").strip()
        if cname and re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", cname):
            return f"https://{cname}/"
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10).stdout.strip()
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", out)
        if m:
            return f"https://{m.group(1)}.github.io/{m.group(2)}/"
    except Exception:
        pass
    return None


def slugify(nav: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", nav.lower()).strip("-")[:40] or "page"
    return slug


EDITOR_CSS = site_build.CSS + r'''
  #app{display:flex;align-items:stretch;min-height:100vh}
  #side{
    width:232px;flex:0 0 232px;background:#141414;color:#eee;
    font:13px "Helvetica Neue",Arial,sans-serif;
    padding:14px 12px;display:flex;flex-direction:column;gap:8px;
    position:sticky;top:0;height:100vh;overflow:auto;
  }
  #side .side-h{
    font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:#999;margin:8px 2px 2px;
  }
  #side ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
  #side li{
    display:flex;align-items:center;gap:4px;border-radius:3px;padding:5px 6px;
  }
  #side li.cur{background:#2c2c2c}
  #side li .nm{flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #side li.cur .nm{font-weight:700}
  #side li button{
    background:none;border:none;color:#999;cursor:pointer;font-size:13px;padding:0 4px;
  }
  #side li button:hover{color:#fff}
  #side label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:#bbb;margin:0 2px}
  #side input,#side select{
    background:#222;border:1px solid #444;color:#eee;border-radius:2px;
    font-size:13px;padding:5px 7px;width:100%;
  }
  #side .row{display:flex;gap:6px;margin:0 2px}
  #side .row button,#side .widebtn{
    flex:1;background:#fff;color:#141414;border:1px solid #fff;border-radius:2px;
    font-size:11px;letter-spacing:.6px;text-transform:uppercase;padding:6px 8px;cursor:pointer;
  }
  #side .widebtn{width:100%;margin:0 2px}
  #side a{color:#9db8ff;font-size:12px;margin:0 2px}
  #main{flex:1;min-width:0}
  .editbar{
    position:sticky;top:0;z-index:50;background:#141414;color:#fff;
    font:13px "Helvetica Neue",Arial,sans-serif;
    display:flex;gap:10px;align-items:center;padding:9px 14px;
  }
  .editbar .spacer{flex:1}
  .editbar button{
    font:12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;text-transform:uppercase;
    background:#fff;color:#141414;border:1px solid #fff;border-radius:2px;
    padding:6px 12px;cursor:pointer;
  }
  .editbar button.warn{background:#141414;color:#fff;border-color:#fff}
  .editbar button:disabled{opacity:.45;cursor:default}
  .editbar a{color:#fff}
  .editbar .status{color:#bbb;font-size:12.5px}
  .toolbar{
    display:none;gap:6px;align-items:center;flex-wrap:wrap;
    background:#f2f0eb;border-bottom:1px solid var(--rule);
    padding:8px 14px;position:sticky;top:39px;z-index:40;
    font:12px "Helvetica Neue",Arial,sans-serif;
  }
  .editing .toolbar{display:flex}
  .toolbar button{
    background:#fff;border:1px solid #999;border-radius:2px;
    padding:4px 10px;cursor:pointer;font-size:12px;
  }
  .toolbar button:hover{background:#141414;color:#fff;border-color:#141414}
  .toolbar button:disabled{opacity:.4;cursor:default}
  .toolbar button.swatch{
    width:16px;height:16px;padding:0;box-sizing:border-box;border:1px solid #777;
  }
  .toolbar button.swatch:hover{border-color:#141414;box-shadow:0 0 0 1px #141414}
  .toolbar #sizeNum{
    width:58px;text-align:center;font-weight:700;font-size:12px;color:#141414;
    background:#fff;border:1px solid #999;border-radius:2px;padding:3px 4px;
    -moz-appearance:textfield;
  }
  .toolbar #sizeNum::-webkit-outer-spin-button,
  .toolbar #sizeNum::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
  .toolbar #sizeNum:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  .toolbar #fontSel{
    max-width:118px;font-size:12px;color:#141414;
    background:#fff;border:1px solid #999;border-radius:2px;padding:4px 2px;
  }
  .toolbar #fontSel:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  /* the live-site star would sit under the dark page list: shift it right */
  .star-badge{left:250px;top:120px;}
  .gem-badge{top:120px;}
  .toolbar input[type=color]{
    width:26px;height:22px;padding:1px;border:1px solid #999;border-radius:2px;
    background:#fff;cursor:pointer;vertical-align:middle;
  }
  .toolbar .sep{width:1px;height:20px;background:#bbb;margin:0 4px}
  #linkStat{font:12px "Helvetica Neue",Arial,sans-serif;color:#bbb;max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ld-back{position:fixed;inset:0;background:rgba(20,20,20,.5);z-index:300;display:flex;align-items:flex-start;justify-content:center;padding:12vh 12px 12px}
  .ld-back[hidden]{display:none}
  .ld-box{background:#fff;color:#141414;border-radius:4px;padding:16px 18px;width:440px;max-width:94vw;box-shadow:0 12px 40px rgba(0,0,0,.4);font:13px "Helvetica Neue",Arial,sans-serif}
  .ld-box h3{margin:0 0 4px;font-size:14px;letter-spacing:.8px;text-transform:uppercase}
  .ld-box label{display:block;margin:10px 0 3px;font-size:12px;color:#444}
  .ld-box input[type=text]{width:100%;box-sizing:border-box;font-size:13px;padding:7px 9px;border:1px solid #999;border-radius:2px}
  .ld-box input[type=text]:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  .ld-hint{min-height:16px;font-size:12px;margin-top:4px;color:#b3261e}
  .ld-pages{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px}
  .ld-pages button{background:#f2f0eb;border:1px solid #999;border-radius:2px;padding:4px 10px;font-size:12px;cursor:pointer;color:#141414}
  .ld-pages button:hover{background:#141414;color:#fff;border-color:#141414}
  .ld-pages button.on{background:#141414;color:#fff;border-color:#141414}
  .ld-box label.ld-check{display:flex;align-items:center;gap:7px;margin:8px 0 0;font-size:12.5px;color:#222}
  .ld-btns{display:flex;gap:8px;margin-top:14px}
  .ld-btns button{flex:1;padding:8px;font-size:12px;letter-spacing:.6px;text-transform:uppercase;border-radius:2px;cursor:pointer}
  #linkApply{background:#141414;color:#fff;border:1px solid #141414}
  #linkApply:disabled{opacity:.4;cursor:default}
  #linkCancel,#linkRemove{background:#fff;color:#141414;border:1px solid #999}
  .editing [data-ed]{outline:1px dashed #1c4fd8;outline-offset:4px;min-height:1em}
  .editing #canvas{outline-offset:8px;min-height:45vh}
  .editing [data-ed]:focus,.editing #canvas:focus{outline:2px solid #1c4fd8}
  .canvas img.sel{outline:3px solid #1c4fd8}
  #hint{
    display:none;color:var(--faint);font-style:italic;
    padding:8px 0 40px;pointer-events:none;
  }
'''


def editor_page(site: dict, slug: str, body: str, live: str | None) -> bytes:
    site = dict(site)
    site["live"] = live
    logo_uri = site_build.logo_data_uri(LOGO)
    has_form = any(p.get("slug") == slug and p.get("form") for p in site.get("pages", []))
    # Inert preview of the live form. It sits outside #canvas (Save only
    # posts the canvas), and page switches toggle it in renderAll.
    inert = ""
    if has_form:
        inert = ('<div id="formpreview" contenteditable="false">'
                 + site_build.CONTACT_FORM_HTML.replace("<form ", "<form onsubmit=\"return false;\" ", 1)
                 + "</div>")
    canvas_body, form_leftover = site_build.place_form(body, inert) if has_form else (body, "")
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Website builder — __TITLE__</title>
<style>
__CSS__
</style>
</head>
<body data-theme="__THEME__">
<div id="app">
<aside id="side">
  <div class="side-h">Pages</div>
  <ul id="pagelist"></ul>
  <div class="row"><button id="btn-addpage">+ Page</button></div>
  <div class="side-h">Site</div>
  <label>Title<input id="in-title"></label>
  <label>Tagline<input id="in-tagline"></label>
  <label>Footer line<input id="in-footer"></label>
  <label>Theme<select id="in-theme">
    <option value="classic">Classic</option>
    <option value="parchment">Parchment</option>
    <option value="ink">Ink</option>
  </select></label>
  <div class="row"><button id="btn-logo">Logo…</button></div>
  <div class="side-h">Check</div>
  <a id="link-built" href="#" target="_blank" rel="noopener">view built page</a>
  <a id="link-live" href="#" target="_blank" rel="noopener">view live site</a>
</aside>
<main id="main">
<div class="editbar">
  <span><b>Builder</b> — <span id="pagename"></span></span>
  <span class="status" id="status">viewing</span>
  <span class="spacer"></span>
  <button id="btn-edit">Edit</button>
  <button id="btn-save" disabled>Save</button>
  <button id="btn-quit" class="warn">Quit</button>
</div>
<div class="toolbar" id="toolbar">
  <button data-cmd="undo" id="btn-undo" title="Undo (Ctrl+Z)" disabled>Undo</button>
  <button data-cmd="redo" id="btn-redo" title="Redo (Ctrl+Y)" disabled>Redo</button>
  <span class="sep"></span>
  <button data-cmd="bold"><b>B</b></button>
  <button data-cmd="italic"><i>I</i></button>
  <button data-cmd="underline"><u>U</u></button>
  <button data-cmd="h2">Heading</button>
  <button data-cmd="h3">Subhead</button>
  <button data-cmd="p">Text</button>
  <button data-cmd="smaller" title="Smaller text">A-</button>
  <button data-cmd="bigger" title="Bigger text">A+</button>
  <input type="number" id="sizeNum" min="8" max="200" title="Font size in px">
  <select id="fontSel" title="Font">
    <option value="">Font: default</option>
    <option value='Georgia,Times New Roman,serif' data-first="georgia">Serif (Georgia)</option>
    <option value='Arial,Helvetica,sans-serif' data-first="arial">Sans (Arial)</option>
    <option value='Times New Roman,Times,serif' data-first="timesnewroman">Times</option>
    <option value='Courier New,Courier,monospace' data-first="couriernew">Mono (Courier)</option>
    <option value='Verdana,Geneva,sans-serif' data-first="verdana">Verdana</option>
    <option value='Trebuchet MS,Verdana,sans-serif' data-first="trebuchetms">Trebuchet</option>
    <option value='Palatino Linotype,Palatino,serif' data-first="palatinolinotype">Palatino</option>
  </select>
  <span class="sep"></span>
  <button data-cmd="ul">Bullets</button>
  <button data-cmd="ol">Numbers</button>
  <button data-cmd="quote">Quote</button>
  <span class="sep"></span>
  <button data-cmd="left">Left</button>
  <button data-cmd="center">Center</button>
  <button data-cmd="right">Right</button>
  <span class="sep"></span>
  <button data-cmd="color" data-color="" title="Default color">Auto</button>
  <button data-cmd="color" data-color="c-muted" class="swatch" title="Muted" style="background:#7a7a7a"></button>
  <button data-cmd="color" data-color="c-red" class="swatch" title="Red" style="background:#c0392b"></button>
  <button data-cmd="color" data-color="c-blue" class="swatch" title="Blue" style="background:#2f6fd0"></button>
  <button data-cmd="color" data-color="c-green" class="swatch" title="Green" style="background:#2e8b57"></button>
  <button data-cmd="color" data-color="c-gold" class="swatch" title="Gold" style="background:#c9a227"></button>
  <input type="color" id="colorwheel" title="Any color — full wheel" value="#c0392b">
  <span class="sep"></span>
  <button data-cmd="link">Link</button>
  <button data-cmd="button">Button</button>
  <button data-cmd="unlink">Unlink</button>
  <span id="linkStat" title="Put the caret in linked text to see where it goes">no link</span>
  <span class="sep"></span>
  <button data-cmd="image">Picture</button>
  <button data-cmd="gallery">Gallery</button>
  <button data-cmd="divider">Divider</button>
  <button data-cmd="section" title="Insert a nice section panel">Section</button>
  <button data-cmd="table" title="Insert a 2x2 table">Table</button>
  <button data-cmd="small" disabled>Small</button>
  <button data-cmd="medium" disabled>Medium</button>
  <button data-cmd="full" disabled>Full</button>
  <button data-cmd="delimg" disabled>Remove</button>
  <span class="sep"></span>
  <button data-cmd="clear">Clear style</button>
</div>
<div class="ld-back" id="linkdlg" hidden>
  <div class="ld-box" role="dialog" aria-label="Link editor">
    <h3 id="linkdlgTitle">Insert link</h3>
    <label for="linkText">Words on the page</label>
    <input type="text" id="linkText" maxlength="200" autocomplete="off" placeholder="What the reader clicks">
    <label for="linkUrl">Goes to</label>
    <input type="text" id="linkUrl" inputmode="url" maxlength="500" autocomplete="off" placeholder="Pick a page below, or paste https://…">
    <div class="ld-hint" id="linkHint"></div>
    <div class="ld-pages" id="linkPages"></div>
    <label class="ld-check"><input type="checkbox" id="linkBlank"> Open in a new tab</label>
    <label class="ld-check"><input type="checkbox" id="linkBtn"> Show as a button</label>
    <div class="ld-btns">
      <button id="linkApply" type="button">Apply</button>
      <button id="linkRemove" type="button">Remove link</button>
      <button id="linkCancel" type="button">Cancel</button>
    </div>
  </div>
</div>
<div class="page" id="page">
  <a class="star-badge" id="starBadge" href="#" title="Open the hidden page"><span class="star-title">Latest and greatest</span><span class="star-sub">Click me</span></a>
  <a class="gem-badge" id="gemBadge" href="#" title="Open the feedback page"><span class="gem-title">Feedback</span></a>
  <div class="masthead">
    <img id="logo" src="__LOGO__" alt="Flamingo and Cactus">
    <h1 data-ed="title">__TITLE_ESC__</h1>
    <p class="tagline" data-ed="tagline">__TAGLINE_ESC__</p>
  </div>
  <nav class="sitenav" id="nav"></nav>
  <div class="hr"></div>
  <div class="canvas" id="canvas">__BODY__</div>
  __FORMPREVIEW__
  <div id="hint">Blank page — press Edit and start typing, use the toolbar for blocks, or drop a picture in.</div>
  <footer id="foot"></footer>
</div>
</main>
</div>
<input type="file" id="filepick" accept="image/png,image/jpeg,image/gif" style="display:none">
<script>
let SITE = __SITE_JSON__;
let SLUG = "__SLUG__";
let editing = false, dirty = false, selImg = null, galleryMode = false;
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const canvas = $("#canvas"), status = $("#status"),
      btnEdit = $("#btn-edit"), btnSave = $("#btn-save"), filepick = $("#filepick");

async function api(path, payload){
  const o = {method: payload === undefined ? "GET" : "POST"};
  if (payload !== undefined) { o.headers = {"Content-Type": "application/json"}; o.body = JSON.stringify(payload); }
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function setEditing(on){
  editing = on;
  document.body.classList.toggle("editing", on);
  canvas.contentEditable = on ? "true" : "false";
  $$("[data-ed]").forEach(el => el.contentEditable = on ? "true" : "false");
  btnEdit.textContent = on ? "Viewing" : "Edit";
  updateSaveBtn();
  if (dirty) status.textContent = "edited — press Save";
  else status.textContent = on ? "editing — nothing saved yet" : "viewing";
  hint();
}
function updateSaveBtn(){
  btnSave.disabled = !(editing || dirty);
}
function markDirty(msg){
  dirty = true;
  updateSaveBtn();
  status.textContent = msg || "edited — press Save";
}
function hint(){
  $("#hint").style.display =
    (editing && !canvas.innerText.trim() && !canvas.querySelector("img")) ? "block" : "none";
}
function renderAll(){
  document.body.dataset.theme = SITE.theme;
  document.querySelector('[data-ed="title"]').innerText = SITE.title;
  document.querySelector('[data-ed="tagline"]').innerText = SITE.tagline;
  $("#in-title").value = SITE.title; $("#in-tagline").value = SITE.tagline;
  $("#in-footer").value = SITE.footer || ""; $("#in-theme").value = SITE.theme;
  const nav = $("#nav"); nav.innerHTML = "";
  const visPages = SITE.pages.filter(p => !p.hidden);
  if (visPages.length > 1) visPages.forEach(p => {
    if (p.slug === SLUG) {
      const s = document.createElement("span"); s.className = "cur"; s.textContent = p.nav;
      nav.appendChild(s);
    } else {
      const a = document.createElement("a"); a.href = "#"; a.textContent = p.nav;
      a.onclick = ev => { ev.preventDefault(); openPage(p.slug); };
      nav.appendChild(a);
    }
  });
  const ul = $("#pagelist"); ul.innerHTML = "";
  SITE.pages.forEach(p => {
    const li = document.createElement("li");
    if (p.slug === SLUG) li.className = "cur";
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = (p.hidden ? "\u2605 " : "") + p.nav;
    nm.title = p.slug + ".html" + (p.hidden ? " (hidden page, not in the menu)" : ""); nm.onclick = () => openPage(p.slug);
    li.appendChild(nm);
    const up = document.createElement("button"); up.textContent = "↑"; up.title = "move up";
    up.onclick = () => movePage(p.slug, -1); li.appendChild(up);
    const dn = document.createElement("button"); dn.textContent = "↓"; dn.title = "move down";
    dn.onclick = () => movePage(p.slug, 1); li.appendChild(dn);
    if (p.slug !== "index") {
      const rn = document.createElement("button"); rn.textContent = "✎"; rn.title = "rename";
      rn.onclick = () => renamePage(p.slug, p.nav); li.appendChild(rn);
      const del = document.createElement("button"); del.textContent = "×"; del.title = "delete page";
      del.onclick = () => deletePage(p.slug, p.nav); li.appendChild(del);
    }
    ul.appendChild(li);
  });
  const cur = SITE.pages.find(p => p.slug === SLUG);
  $("#pagename").textContent = cur ? cur.nav : SLUG;
  const fp = document.getElementById("formpreview");
  if (fp) fp.style.display = (cur && cur.form) ? "" : "none";
  $("#foot").textContent = SITE.footer || "";
  $("#foot").style.display = SITE.footer ? "" : "none";
  const starPage = SITE.pages.find(p => p.hidden && (p.badge || "star") === "star");
  const starBadge = $("#starBadge");
  if (starBadge) starBadge.style.display = starPage ? "" : "none";
  const gemPage = SITE.pages.find(p => p.hidden && p.badge === "gem");
  const gemBadge = $("#gemBadge");
  if (gemBadge) {
    gemBadge.style.display = gemPage ? "" : "none";
    if (gemPage) gemBadge.querySelector(".gem-title").textContent = gemPage.nav;
  }
  $("#link-built").href = "/built?slug=" + encodeURIComponent(SLUG);
  const liveA = $("#link-live");
  if (SITE.live) {
    liveA.style.display = "";
    liveA.href = SITE.live + (SLUG === "index" ? "" : encodeURIComponent(SLUG) + ".html");
  } else {
    liveA.style.display = "none";
  }
  hint();
  updateLinkStat();
  updateFontSel();
}
async function openPage(slug){
  if (slug === SLUG) return;
  if (dirty && !confirm("Switch page without saving?")) return;
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  let j;
  try {
    j = await api("/api/page?slug=" + encodeURIComponent(slug));
  } catch (err) { status.textContent = "could not open page: " + err.message; return; }
  SLUG = slug; canvas.innerHTML = j.body; selImg = null;
  ensureExitBlock();
  histReset();
  dirty = false; renderAll(); updateSaveBtn(); placeCaretStart();
  status.textContent = editing ? "editing — nothing saved yet" : "viewing";
}
function placeCaretStart(){
  canvas.focus();
  try {
    const r = document.createRange();
    r.selectNodeContents(canvas);
    r.collapse(true);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  } catch (e) {}
}
["in-title","in-tagline","in-footer","in-theme"].forEach(id => {
  $("#" + id).addEventListener("input", () => {
    readInputsToSite();
    // focus is in the sidebar, so rewriting the canvas copy is safe
    document.querySelector('[data-ed="title"]').innerText = SITE.title;
    document.querySelector('[data-ed="tagline"]').innerText = SITE.tagline;
    markDirty();
  });
});
// title/tagline can also be edited directly on the page — copy those
// edits back into the sidebar so Save picks them up. Never rewrite the
// element being typed in (that would jump the caret).
const ED_TO_INPUT = {title: "in-title", tagline: "in-tagline"};
function readInputsToSite(){
  SITE.title = $("#in-title").value;
  SITE.tagline = $("#in-tagline").value;
  SITE.footer = $("#in-footer").value;
  SITE.theme = $("#in-theme").value;
  document.body.dataset.theme = SITE.theme;
  $("#foot").textContent = SITE.footer;
  $("#foot").style.display = SITE.footer ? "" : "none";
}
document.addEventListener("input", e => {
  const ed = e.target.closest && e.target.closest("#canvas,[data-ed]");
  if (!ed) return;
  if (ed.dataset && ed.dataset.ed && ED_TO_INPUT[ed.dataset.ed]) {
    $("#" + ED_TO_INPUT[ed.dataset.ed]).value = ed.innerText.trim();
    readInputsToSite();
  }
  markDirty(); hint();
});
btnEdit.onclick = () => setEditing(!editing);

$("#btn-addpage").onclick = async () => {
  if (dirty && !confirm("Add a page without saving this one?")) return;
  const nav = prompt("Name for the new page:", "New page");
  if (!nav) return;
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  let j;
  try {
    j = await api("/api/pages/add", {nav});
  } catch (err) { status.textContent = "could not add page: " + err.message; return; }
  SITE = j.site; dirty = false;
  let b;
  try {
    b = await api("/api/page?slug=" + encodeURIComponent(j.slug));
  } catch (err) { status.textContent = "page added but it could not be loaded: " + err.message; renderAll(); updateSaveBtn(); return; }
  SLUG = j.slug; canvas.innerHTML = b.body; renderAll(); updateSaveBtn();
  histReset();
  status.textContent = "page added — it is already saved";
};
async function renamePage(slug, old){
  if (dirty && !confirm("Rename without saving this page?")) return;
  const nav = prompt("Menu name:", old);
  if (!nav || nav === old) return;
  try {
    SITE = (await api("/api/pages/rename", {slug, nav})).site;
  } catch (err) { status.textContent = "rename failed: " + err.message; return; }
  dirty = false; renderAll(); updateSaveBtn(); status.textContent = "page renamed — saved";
}
async function deletePage(slug, nav){
  if (dirty && !confirm("Delete without saving this page?")) return;
  if (!confirm('Delete the page "' + nav + '"? This cannot be undone.')) return;
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  try {
    SITE = (await api("/api/pages/delete", {slug})).site;
  } catch (err) { status.textContent = "delete failed: " + err.message; return; }
  if (SLUG === slug) {
    SLUG = "index";
    try {
      canvas.innerHTML = (await api("/api/page?slug=index")).body;
    } catch (err) { status.textContent = "page deleted but home could not load: " + err.message; dirty = false; renderAll(); updateSaveBtn(); return; }
  histReset();
  }
  dirty = false; renderAll(); updateSaveBtn(); status.textContent = "page deleted";
}
async function movePage(slug, dir){
  if (dirty && !confirm("Reorder without saving this page?")) return;
  try {
    SITE = (await api("/api/pages/move", {slug, dir})).site;
  } catch (err) { status.textContent = "move failed: " + err.message; return; }
  dirty = false; renderAll(); updateSaveBtn();
}
$("#btn-logo").onclick = async () => {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = "image/png";
  inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    const du = await new Promise((res, rej) => {
      const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(f);
    });
    const j = await api("/api/upload-logo", {dataUri: du});
    $("#logo").src = j.preview;
    status.textContent = "logo changed — saved";
  };
  inp.click();
};

/* ---------- formatting + blocks ---------- */
const SIZE_BTNS = ["small","medium","full","delimg"];
function imgButtons(){
  $$("#toolbar [data-cmd]").forEach(b => {
    if (SIZE_BTNS.includes(b.dataset.cmd)) b.disabled = !selImg;
  });
}
canvas.addEventListener("click", e => {
  // in edit mode, clicking a link must not navigate away from the builder
  if (editing && e.target.closest("a")) e.preventDefault();
  $$("#canvas img").forEach(i => i.classList.remove("sel"));
  selImg = null;
  if (e.target.tagName === "IMG" && editing) { selImg = e.target; selImg.classList.add("sel"); }
  imgButtons();
});
function placeCaretAtEnd(el){
  el.focus();
  const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
}
function nodePath(root, node){
  const path = [];
  let cur = node;
  while (cur && cur !== root && cur.parentNode) {
    const parent = cur.parentNode;
    const idx = Array.prototype.indexOf.call(parent.childNodes, cur);
    if (idx < 0) return null;
    path.unshift(idx);
    cur = parent;
  }
  if (cur !== root) return null;
  return path;
}
function nodeFromPath(root, path){
  let cur = root;
  for (const i of path) {
    if (!cur || !cur.childNodes || i < 0 || i >= cur.childNodes.length) return null;
    cur = cur.childNodes[i];
  }
  return cur;
}
/* ---------- text size + text color ----------
   Sizes are NUMERIC pixels: the toolbar writes inline style="font-size:NNpx"
   (integer, 8-200) and never a t-* class. Colors are the c-* classes or an
   inline style="color:...". save() whitelists both before sending and the
   server re-validates, so nothing hostile survives a save.
   The old t-* classes still render (the CSS keeps them) and a legacy element
   still reads back its true size through getComputedStyle, but the toolbar
   only ever writes numbers — and setting a size strips any legacy t-* class
   off the element it touches. */
const PX_SCALE = [8,9,10,11,12,14,16,18,20,24,28,32,36,48,64,72,96];
const SIZE_MIN = 8, SIZE_MAX = 200;    // the number box's range
// the seven stacks the Font dropdown offers — the only values save() keeps
const FONT_OK = new Set(Array.from(document.querySelectorAll("#fontSel option")).map(o => o.value).filter(Boolean));
const COLOR_CLASSES = ["c-muted", "c-red", "c-blue", "c-green", "c-gold"];
const BLOCK_SEL = "p,h2,h3,blockquote,li";
const SIZE_RE = /^t-(tiny|small|large|huge|giant)$/;
const COLOR_RE = /^c-/;
const WHEEL_RE = /^#[0-9a-f]{6}$/i;   // native picker always yields #rrggbb
// ---------- undo history: snapshots of the canvas ----------
let hist = [], hi = -1, typeTimer = null;
function histReset(){ if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; } hist = [canvas.innerHTML]; hi = 0; updateHistBtns(); }
function pushHist(){
  const cur = canvas.innerHTML;
  if (hist[hi] === cur) return;
  hist = hist.slice(0, hi + 1);
  hist.push(cur);
  if (hist.length > 60) hist.shift();
  hi = hist.length - 1;
  updateHistBtns();
}
function updateHistBtns(){
  const u = $("#btn-undo"), r = $("#btn-redo");
  if (u) u.disabled = hi <= 0;
  if (r) r.disabled = hi >= hist.length - 1;
}
function doUndo(){
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; pushHist(); }
  if (hi <= 0) return;
  // save a best-effort caret path before the DOM is replaced
  let savedPath = null, savedOffset = 0;
  const sel0 = getSelection();
  if (sel0.rangeCount) {
    const r0 = sel0.getRangeAt(0);
    if (canvas.contains(r0.commonAncestorContainer)) {
      savedPath = nodePath(canvas, r0.startContainer);
      savedOffset = r0.startOffset;
    }
  }
  hi--; canvas.innerHTML = hist[hi];
  canvas.focus();
  let restored = false;
  if (savedPath) {
    const node = nodeFromPath(canvas, savedPath);
    if (node) {
      try {
        const r = document.createRange();
        const maxOff = (node.nodeType === 3) ? node.length : node.childNodes.length;
        r.setStart(node, Math.min(savedOffset, maxOff));
        r.collapse(true);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        restored = true;
      } catch (e) { restored = false; }
    }
  }
  if (!restored) placeCaretAtEnd(canvas);
  markDirty(); hint(); updateHistBtns();
  updateSizeNum();
}
function doRedo(){
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; pushHist(); }
  if (hi >= hist.length - 1) return;
  let savedPath = null, savedOffset = 0;
  const sel0 = getSelection();
  if (sel0.rangeCount) {
    const r0 = sel0.getRangeAt(0);
    if (canvas.contains(r0.commonAncestorContainer)) {
      savedPath = nodePath(canvas, r0.startContainer);
      savedOffset = r0.startOffset;
    }
  }
  hi++; canvas.innerHTML = hist[hi];
  canvas.focus();
  let restored = false;
  if (savedPath) {
    const node = nodeFromPath(canvas, savedPath);
    if (node) {
      try {
        const r = document.createRange();
        const maxOff = (node.nodeType === 3) ? node.length : node.childNodes.length;
        r.setStart(node, Math.min(savedOffset, maxOff));
        r.collapse(true);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        restored = true;
      } catch (e) { restored = false; }
    }
  }
  if (!restored) placeCaretAtEnd(canvas);
  markDirty(); hint(); updateHistBtns();
  updateSizeNum();
}
// typing coalesces into one undo step per burst
canvas.addEventListener("input", () => {
  clearTimeout(typeTimer);
  typeTimer = setTimeout(pushHist, 800);
});
// paint an arbitrary wheel color (inline style the sanitizer keeps)
function paintWheel(el, hex){
  stripClasses(el, COLOR_RE);
  el.style.color = hex.toLowerCase();
}
function applyWheelColor(hex){
  if (!WHEEL_RE.test(hex || "")) return;
  let r = getRange();
  if (!r) { r = activeRange(); if (r) restoreRange(r); }
  if (!r) return;
  hex = hex.toLowerCase();
  if (r.collapsed) {
    const b = nodeBlock(r.startContainer);
    if (b) paintWheel(b, hex);
    return;
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) { blocksIntersecting(r).forEach(b => paintWheel(b, hex)); return; }
  const s = nearestSpan(r);
  if (s) { paintWheel(s, hex); if (!s.className && !s.style.color) unwrapSpan(s); }
  else { const sp = wrapSpan(r, ""); if (sp) paintWheel(sp, hex); }
}
function getRange(){
  const s = getSelection();
  if (!s.rangeCount) return null;
  const r = s.getRangeAt(0);
  return canvas.contains(r.commonAncestorContainer) ? r : null;
}
function restoreRange(r){
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
}
// nearest block (p/h2/h3/blockquote/li) holding n, stopping at the canvas
function nodeBlock(n){
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && n.matches(BLOCK_SEL)) return n;
    n = n.parentNode;
  }
  return null;
}
// nearest span around the selection — where an inline run of text goes
function nearestSpan(r){
  let n = r.commonAncestorContainer;
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && n.tagName === "SPAN") return n;
    n = n.parentNode;
  }
  return null;
}
function blocksIntersecting(r){
  return Array.from(canvas.querySelectorAll(BLOCK_SEL)).filter(el => r.intersectsNode(el));
}
function stripClasses(el, re){
  Array.from(el.classList).forEach(c => { if (re.test(c)) el.classList.remove(c); });
}
// the real size in px, read from the computed style — never inferred from a
// class, so legacy t-* content and default-size blocks both come out right
function effectivePx(el){
  if (!el) return null;
  const v = parseFloat(getComputedStyle(el).fontSize);
  return (isFinite(v) && v > 0) ? v : null;
}
// One step along PX_SCALE from a current effective size: down takes the
// nearest step at/below it, up the nearest step at/above it, and a size
// sitting exactly on a step moves on to the next one. The index is clamped to
// the ends of the scale, so A-/A+ stop at 8 and 96 — they never wrap round to
// the other end and never fall back to a default. A typed size outside the
// scale is pulled onto its nearest end.
function stepSize(px, dir){
  const n = PX_SCALE.length;
  if (!isFinite(px) || px <= 0) px = effectivePx(canvas) || 16;
  let i;
  if (dir > 0) {
    i = PX_SCALE.findIndex(v => v >= px);
    if (i < 0) i = n;                        // above the scale: clamp to the top
    else if (PX_SCALE[i] === px) i++;        // on a step: take the next one up
    i = Math.min(i, n - 1);
  } else {
    i = -1;
    for (let k = 0; k < n; k++) if (PX_SCALE[k] <= px) i = k;   // last at/below
    if (i >= 0 && PX_SCALE[i] === px) i--;   // on a step: take the one below
    i = Math.max(i, 0);                      // below the scale: clamp to the bottom
  }
  return PX_SCALE[i];
}
// The toolbar writes numbers only: an inline style.fontSize in whole pixels,
// never a t-* class and never a <font> tag. The legacy t-* class is stripped
// off whatever it lands on; c-* classes and an inline color are left alone.
function setSizePx(el, px){
  stripClasses(el, SIZE_RE);
  el.style.fontSize = Math.max(SIZE_MIN, Math.min(SIZE_MAX, Math.round(px))) + "px";
}
function setColor(el, cls){
  if (cls && el.classList.contains(cls)) { el.classList.remove(cls); return; }
  stripClasses(el, COLOR_RE); el.style.removeProperty("color");
  if (cls) el.classList.add(cls);
}
// The live range if it is still inside the canvas, else the last one: clicking
// into the size box moves focus off the canvas and some browsers then drop the
// selection, but Enter still has to know what it is sizing.
let lastRange = null;
document.addEventListener("selectionchange", () => {
  const r = getRange();
  if (r) lastRange = r.cloneRange();
  updateLinkStat();
});
function activeRange(){
  const r = getRange();
  if (r) return r;
  if (lastRange && canvas.contains(lastRange.commonAncestorContainer)) return lastRange;
  return null;
}
// the effective px the caret (or selection start) is sitting in
function sizeAtCaret(){
  const r = activeRange(); if (!r) return null;
  let n = r.startContainer;
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && (n.tagName === "SPAN" || n.matches(BLOCK_SEL))) return effectivePx(n);
    n = n.parentNode;
  }
  return effectivePx(canvas);
}
// the number box doubles as the readout and the input
function updateSizeNum(){
  const inp = $("#sizeNum"); if (!inp) return;
  if (document.activeElement === inp) return;   // don't stomp what is being typed
  const px = sizeAtCaret();
  inp.value = px === null ? "" : String(Math.round(px * 10) / 10);   // "16", not "16.0"
}
function unwrapSpan(el){
  const p = el.parentNode; if (!p) return;
  const r = document.createRange();
  r.setStartBefore(el);
  const last = el.lastChild;
  while (el.firstChild) p.insertBefore(el.firstChild, el);
  p.removeChild(el);
  if (last) { r.setEndAfter(last); restoreRange(r); }
}
function wrapSpan(r, cls){
  const s = document.createElement("span");
  s.className = cls;
  try { r.surroundContents(s); }
  catch (e) {
    // selection grazes an element boundary: move the contents in instead
    const frag = r.extractContents();
    s.appendChild(frag);
    if (!s.firstChild) return null;
    r.insertNode(s);
  }
  const nr = document.createRange();
  nr.selectNodeContents(s);
  restoreRange(nr);
  return s;
}
// Run fn over the size target(s) of the current selection: a collapsed caret
// sizes the span it sits in (or the block when there is none) so the element
// changed is the one the size box is reading out — sizing the block while an
// inner span holds a bigger size looks like nothing happens. A selection
// reaching across blocks sizes every block it touches, and a selection
// inside one block paints the nearest span (or wraps a bare one) so only
// that run of text changes. Each element is sized from its own effective
// size, so a mixed-size selection keeps its spread.
function eachSizeTarget(fn){
  let r = getRange();
  if (!r) {
    r = activeRange();
    if (r) restoreRange(r);          // focus left the canvas: put the range back
  }
  if (!r) return;
  if (r.collapsed) {                       // caret only: size the span, else the block
    const s = nearestSpan(r);
    if (s) { fn(s); if (!s.className && !s.style.color && !s.style.fontSize) unwrapSpan(s); return; }
    const b = nodeBlock(r.startContainer);
    if (b) fn(b);
    return;
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) {                           // selection reaches across blocks
    blocksIntersecting(r).forEach(b => fn(b));
    return;
  }
  const s = nearestSpan(r);
  if (s) { fn(s); if (!s.className && !s.style.color && !s.style.fontSize) unwrapSpan(s); }
  else { const sp = wrapSpan(r, ""); if (sp) fn(sp); }
}
/* ---------- fonts: same targeting as sizes, separate unwrap check ----------
   The toolbar writes an exact allowlisted stack (or removes the property);
   save() and the server re-validate, so only the seven stacks survive. */
function eachFontTarget(fn){
  let r = getRange();
  if (!r) {
    r = activeRange();
    if (r) restoreRange(r);
  }
  if (!r) return;
  if (r.collapsed) {
    const s = nearestSpan(r);
    if (s) { fn(s); if (!s.className && !s.style.color && !s.style.fontSize && !s.style.fontFamily) unwrapSpan(s); return; }
    const b = nodeBlock(r.startContainer);
    if (b) fn(b);
    return;
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) {
    blocksIntersecting(r).forEach(b => fn(b));
    return;
  }
  const s = nearestSpan(r);
  if (s) { fn(s); if (!s.className && !s.style.color && !s.style.fontSize && !s.style.fontFamily) unwrapSpan(s); }
  else { const sp = wrapSpan(r, ""); if (sp) fn(sp); }
}
function setFontStack(el, stack){
  if (stack) el.style.fontFamily = stack;
  else el.style.removeProperty("font-family");
}
function applyFont(stack){
  eachFontTarget(el => setFontStack(el, stack));
}
function fontFirstFamily(v){
  return (v || "").split(",")[0].replace(/["'\\s]/g, "").toLowerCase();
}
// the real stack at the caret, read from the computed style — never inferred
function updateFontSel(){
  const sel = document.getElementById("fontSel"); if (!sel) return;
  if (document.activeElement === sel) return;   // don't stomp what is being picked
  const r = activeRange(); if (!r) { sel.value = ""; return; }
  let n = r.startContainer;
  if (n.nodeType === 3) n = n.parentNode;
  let el = null;
  if (n && n !== canvas) {
    el = (n.nodeType === 1 && n.tagName === "SPAN") ? n : nodeBlock(n);
  }
  if (!el) { sel.value = ""; return; }
  const fam = fontFirstFamily(getComputedStyle(el).fontFamily);
  const opt = Array.from(sel.options).find(o => o.dataset.first === fam);
  sel.value = opt ? opt.value : "";
}
/* ---------- pictures: align the selected image ---------- */
function alignImage(how){
  if (!selImg) return;
  selImg.classList.remove("ileft", "iright", "icenter");
  if (how === "left") selImg.classList.add("ileft");
  else if (how === "right") selImg.classList.add("iright");
  else selImg.classList.add("icenter");
}
/* ---------- move the caret's top-level block up or down (Alt+Arrow) ---------- */
function topBlock(n){
  if (!n) return null;
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas && n.parentNode !== canvas) n = n.parentNode;
  return (n && n !== canvas) ? n : null;
}
function moveBlock(dir){
  const r = activeRange(); if (!r) return;
  const b = topBlock(r.startContainer); if (!b) return;
  const sib = dir < 0 ? b.previousElementSibling : b.nextElementSibling;
  if (!sib) return;
  if (dir < 0) b.parentNode.insertBefore(b, sib);
  else b.parentNode.insertBefore(sib, b);
  const nr = document.createRange();
  nr.selectNodeContents(b); nr.collapse(dir < 0);
  restoreRange(nr);
  pushHist(); markDirty(); hint(); updateSizeNum(); updateFontSel(); updateLinkStat();
}
// A-/A+: one step along PX_SCALE, clamped at the ends — no wrap-around and no
// falling back to a default size.
function applySize(dir){
  eachSizeTarget(el => setSizePx(el, stepSize(effectivePx(el), dir)));
}
// a typed size: exactly what the number box says, rounded and clamped
function applyExactSize(px){
  eachSizeTarget(el => setSizePx(el, px));
}
// Enter (or the change event) in the size box applies that size to the caret's
// block or the selection, same targeting rules as A-/A+.
function commitSizeNum(){
  const inp = $("#sizeNum"); if (!inp) return;
  const raw = (inp.value || "").trim();
  const n = Math.round(Number(raw));
  if (raw === "" || !isFinite(n)) { updateSizeNum(); return; }   // not a number: show the real size again
  applyExactSize(Math.max(SIZE_MIN, Math.min(SIZE_MAX, n)));
  canvas.focus();
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
}
$("#sizeNum").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); commitSizeNum(); }
});
$("#sizeNum").addEventListener("change", commitSizeNum);
$("#fontSel").addEventListener("change", () => {
  if (!editing) return;
  canvas.focus();
  applyFont($("#fontSel").value);
  pushHist(); markDirty(); hint(); updateSizeNum(); updateFontSel(); updateLinkStat();
});
function applyColor(cls){
  if (cls && COLOR_CLASSES.indexOf(cls) < 0) return;
  let r = getRange();
  if (!r) { r = activeRange(); if (r) restoreRange(r); }
  if (!r) return;
  if (r.collapsed) {                       // caret only: color the current block
    const b = nodeBlock(r.startContainer);
    if (b) setColor(b, cls);
    return;
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) {                           // selection reaches across blocks
    blocksIntersecting(r).forEach(b => setColor(b, cls));
    return;
  }
  const s = nearestSpan(r);
  if (s) { setColor(s, cls); if (!s.className && !s.style.color) unwrapSpan(s); }
  else if (cls) wrapSpan(r, cls);          // "Auto" on a bare selection: nothing to strip
}
// the Clear style button, beyond what removeFormat already does
function clearSizeColor(){
  const r = getRange(); if (!r) return;
  let range = r;
  if (r.collapsed) {                       // a caret clears its whole block
    const b = nodeBlock(r.startContainer);
    if (b) { range = document.createRange(); range.selectNodeContents(b); }
  }
  Array.from(canvas.querySelectorAll(BLOCK_SEL + ",span")).forEach(el => {
    if (!range.intersectsNode(el)) return;
    stripClasses(el, SIZE_RE);
    stripClasses(el, COLOR_RE);
    el.style.removeProperty("color");
    el.style.removeProperty("font-size");      // the numeric size the toolbar writes
    el.style.removeProperty("font-family");    // the font dropdown's stack
    if (el.tagName === "SPAN" && !el.className && !el.style.color && !el.style.fontSize && !el.style.fontFamily) unwrapSpan(el);   // no class left: unwrap
  });
  Array.from(canvas.querySelectorAll("img.secimg")).forEach(img => {
    if (!range.intersectsNode(img)) return;
    img.classList.remove("ileft", "iright", "icenter");
  });
}
/* ---------- links: a real dialog instead of prompt() ----------
   Page quick-pick, validation matching what save() keeps, editing the link
   under the caret, and a live readout of where it goes. The server-side
   sanitize() remains the enforcer: anything it would strip is blocked
   up front with an explanation. */
const linkDlg = $("#linkdlg"), linkTitle = $("#linkdlgTitle"),
      linkText = $("#linkText"), linkUrl = $("#linkUrl"),
      linkHint = $("#linkHint"), linkPages = $("#linkPages"),
      linkBlank = $("#linkBlank"), linkBtnBox = $("#linkBtn"),
      linkApply = $("#linkApply"), linkRemove = $("#linkRemove"),
      linkCancel = $("#linkCancel"), linkStat = $("#linkStat");
let linkEditTarget = null;
function escapeHtml(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
// nearest <a> holding node n, stopping at the canvas
function anchorAtPoint(n){
  if (n && n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && n.tagName === "A") return n;
    n = n.parentNode;
  }
  return null;
}
// the link the caret/selection is editing, or null for a fresh insert
function anchorForRange(r){
  if (!r) return null;
  const a = anchorAtPoint(r.startContainer);
  if (!a) return null;
  if (r.collapsed) return a;
  return a.contains(r.endContainer) ? a : null;
}
// client mirror of the server's _clean_href: block what save() would strip
function linkValidate(h){
  h = (h || "").trim();
  if (!h) return {ok:false, msg:"Type or pick a destination."};
  if (/["<>]/.test(h)) return {ok:false, msg:"No quotes or angle brackets."};
  if (h.includes("..")) return {ok:false, msg:"“..” is not allowed in a link."};
  if (/^[A-Za-z0-9_.-]+(\\/[A-Za-z0-9_.-]+)*\\.html$/.test(h)) return {ok:true, msg:"Page on this site."};
  if (h.startsWith("//")) return {ok:false, msg:"That would leave this site — paste the full address instead."};
  const m = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(h);
  if (m) {
    const s = m[1].toLowerCase();
    if (s === "http" || s === "https" || s === "mailto" || s === "tel") {
      if (/\\s/.test(h)) return {ok:true, warn:"Spaces usually break a link — check it in the preview."};
      return {ok:true, msg:"External link."};
    }
    return {ok:false, msg:"Only http, https, mailto, tel, #anchors, /paths and pages survive saving."};
  }
  if (h.startsWith("#")) return {ok:true, msg:"Anchor on this page."};
  if (h.startsWith("/")) return {ok:true, msg:"Path on this site."};
  if (/\\s/.test(h)) return {ok:false, msg:"That does not look like a link. Pick a page or paste a full address."};
  return {ok:false, msg:"Pick a page below, or paste a full http(s) address."};
}
function refreshLinkDialog(){
  const v = linkValidate(linkUrl.value);
  linkApply.disabled = !v.ok;
  linkHint.textContent = v.warn || v.msg || "";
  linkHint.style.color = v.ok ? "#555" : "#b3261e";
  const cur = linkUrl.value.trim();
  linkPages.querySelectorAll("button").forEach(b => {
    b.classList.toggle("on", b.dataset.href === cur);
  });
}
function openLinkDialog(mode){
  if (!editing) return;
  const r = activeRange();
  if (!r) { status.textContent = "click into the page text first, then Link"; return; }
  linkEditTarget = anchorForRange(r);
  const btnMode = mode === "btn";
  linkTitle.textContent = linkEditTarget ? (btnMode ? "Edit button" : "Edit link")
                                         : (btnMode ? "Insert button" : "Insert link");
  if (linkEditTarget) {
    linkText.value = (linkEditTarget.textContent || "").trim().slice(0, 200);
    linkUrl.value = linkEditTarget.getAttribute("href") || "";
    linkBlank.checked = linkEditTarget.getAttribute("target") === "_blank";
    linkBtnBox.checked = linkEditTarget.classList.contains("btn");
  } else {
    linkText.value = r.collapsed ? "" : r.toString().slice(0, 200);
    linkUrl.value = btnMode ? "https://" : "";
    linkBlank.checked = btnMode;
    linkBtnBox.checked = btnMode;
  }
  linkRemove.style.display = linkEditTarget ? "" : "none";
  // page quick-pick, rebuilt every open so renames show up
  linkPages.innerHTML = "";
  SITE.pages.forEach(p => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = (p.hidden ? "☆ " : "") + p.nav;
    b.title = p.slug + ".html" + (p.hidden ? " (hidden page)" : "");
    b.dataset.href = p.slug + ".html";
    b.onclick = () => { linkUrl.value = b.dataset.href; linkUrl.focus(); refreshLinkDialog(); };
    linkPages.appendChild(b);
  });
  refreshLinkDialog();
  linkDlg.hidden = false;
  if (linkEditTarget) linkUrl.focus(); else { linkText.focus(); linkText.select(); }
}
function closeLinkDialog(){
  linkDlg.hidden = true;
  linkEditTarget = null;
  if (editing) { canvas.focus(); const r = activeRange(); if (r) restoreRange(r); }
  updateLinkStat();
}
function applyLink(){
  const v = linkValidate(linkUrl.value);
  if (!v.ok) { refreshLinkDialog(); return; }
  const href = linkUrl.value.trim();
  const label = linkText.value.trim();
  const blank = linkBlank.checked, btn = linkBtnBox.checked;
  const r = activeRange();
  if (!r) return;
  restoreRange(r);
  canvas.focus();
  if (linkEditTarget && canvas.contains(linkEditTarget)) {
    if (label) linkEditTarget.textContent = label;
    linkEditTarget.setAttribute("href", href);
    if (blank) { linkEditTarget.setAttribute("target", "_blank"); linkEditTarget.setAttribute("rel", "noopener"); }
    else { linkEditTarget.removeAttribute("target"); linkEditTarget.removeAttribute("rel"); }
    linkEditTarget.classList.toggle("btn", btn);
    const cur = getRange();
    if (cur) { cur.selectNodeContents(linkEditTarget); cur.collapse(false); restoreRange(cur); }
  } else if (r.collapsed) {
    if (!label) { linkHint.textContent = "Give the link some words first."; linkHint.style.color = "#b3261e"; linkText.focus(); return; }
    let tag = '<a href="' + escapeHtml(href) + '"';
    if (blank) tag += ' target="_blank" rel="noopener"';
    if (btn) tag += ' class="btn"';
    let tagHtml = tag + ">" + escapeHtml(label) + "</a>";
    if (btn) tagHtml = "<p>" + tagHtml + "</p>";
    document.execCommand("insertHTML", false, tagHtml);
  } else {
    document.execCommand("createLink", false, "#fc-newlink");
    const made = Array.from(canvas.querySelectorAll('a[href="#fc-newlink"]'));
    if (!made.length) { linkHint.textContent = "Could not link that selection — try selecting plain text."; linkHint.style.color = "#b3261e"; return; }
    made.forEach(a => {
      a.setAttribute("href", href);
      if (blank) { a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener"); }
      if (btn) a.classList.add("btn");
    });
    if (made.length === 1 && label && label !== made[0].textContent) made[0].textContent = label;
  }
  closeLinkDialog();
  pushHist(); markDirty(); hint(); updateSizeNum(); updateLinkStat();
}
function removeLink(){
  const r = activeRange();
  if (!r) return;
  restoreRange(r);
  canvas.focus();
  const a = (linkEditTarget && canvas.contains(linkEditTarget)) ? linkEditTarget : anchorForRange(getRange());
  if (!a) { closeLinkDialog(); return; }
  const parent = a.parentNode, frag = document.createDocumentFragment();
  while (a.firstChild) frag.appendChild(a.firstChild);
  parent.replaceChild(frag, a);
  if (parent.normalize) parent.normalize();
  closeLinkDialog();
  pushHist(); markDirty(); hint(); updateSizeNum(); updateLinkStat();
}
function updateLinkStat(){
  if (!linkStat) return;
  const r = activeRange();
  const a = r ? anchorForRange(r) : null;
  if (a) {
    const h = a.getAttribute("href") || "";
    linkStat.textContent = "→ " + (h.length > 42 ? h.slice(0, 41) + "…" : h);
    linkStat.title = h || "(no destination)";
  } else {
    linkStat.textContent = "no link";
    linkStat.title = "Put the caret in linked text to see where it goes";
  }
}
linkApply.onclick = applyLink;
linkRemove.onclick = removeLink;
linkCancel.onclick = closeLinkDialog;
linkText.oninput = refreshLinkDialog;
linkUrl.oninput = refreshLinkDialog;
linkDlg.addEventListener("mousedown", e => { if (e.target === linkDlg) closeLinkDialog(); });
linkDlg.addEventListener("keydown", e => {
  if (e.key === "Enter" && !linkApply.disabled) { e.preventDefault(); applyLink(); }
  else if (e.key === "Escape") { e.preventDefault(); closeLinkDialog(); }
});
// full color wheel: live-paints any color as the selection changes
$("#colorwheel").addEventListener("input", e => {
  if (!editing) return;
  canvas.focus();
  applyWheelColor(e.target.value);
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
});
$("#toolbar").addEventListener("click", e => {
  const b = e.target.closest("[data-cmd]"); if (!b || !editing) return;
  const c = b.dataset.cmd;
  canvas.focus();
  if (c === "h2") document.execCommand("formatBlock", false, "<h2>");
  else if (c === "h3") document.execCommand("formatBlock", false, "<h3>");
  else if (c === "p") document.execCommand("formatBlock", false, "<p>");
  else if (c === "ul") document.execCommand("insertUnorderedList");
  else if (c === "ol") document.execCommand("insertOrderedList");
  else if (c === "quote") document.execCommand("formatBlock", false, "<blockquote>");
  else if (c === "left" || c === "center" || c === "right") {
    if (selImg && editing) alignImage(c);
    else if (c === "left") document.execCommand("justifyLeft");
    else if (c === "center") document.execCommand("justifyCenter");
    else document.execCommand("justifyRight");
  }
  else if (c === "smaller") applySize(-1);
  else if (c === "bigger") applySize(1);
  else if (c === "color") applyColor(b.dataset.color || "");
  else if (c === "bold" || c === "italic" || c === "underline") document.execCommand(c);
  else if (c === "undo") doUndo();
  else if (c === "redo") doRedo();
  else if (c === "clear") { document.execCommand("removeFormat"); clearSizeColor(); }
  else if (c === "unlink") document.execCommand("unlink");
  else if (c === "link") openLinkDialog("link");
  else if (c === "button") openLinkDialog("btn");
  else if (c === "divider") document.execCommand("insertHTML", false, "<hr><p><br></p>");
  else if (c === "section") document.execCommand("insertHTML", false,
    '<div class="card"><h2>Section title</h2><p>Write something great here.</p></div><p><br></p>');
  else if (c === "table") document.execCommand("insertHTML", false,
    '<table><tbody><tr><th>Header</th><th>Header</th></tr><tr><td><br></td><td><br></td></tr></tbody></table><p><br></p>');
  else if (c === "image") { galleryMode = false; filepick.multiple = false; filepick.click(); }
  else if (c === "gallery") { galleryMode = true; filepick.multiple = true; filepick.click(); }
  else if ((c === "small" || c === "medium" || c === "full") && selImg) {
    selImg.classList.remove("small","medium","full");
    selImg.classList.add("secimg", c);
  }
  else if (c === "delimg" && selImg) { selImg.remove(); selImg = null; }
  else return;
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
});
filepick.onchange = async () => {
  const files = Array.from(filepick.files); filepick.value = "";
  if (!files.length) return;
  const paths = [];
  for (const f of files) {
    const p = await uploadImage(f);
    if (p) paths.push(p);
  }
  if (!paths.length) return;
  canvas.focus();
  if (galleryMode && paths.length > 1) {
    const g = document.createElement("div"); g.className = "gallery";
    paths.forEach(p => {
      const img = document.createElement("img"); img.src = p; img.alt = "";
      g.appendChild(img);
    });
    appendNode(g);
  } else {
    paths.forEach(p => {
      const img = document.createElement("img");
      img.src = p; img.className = "secimg medium"; img.alt = "";
      appendNode(img);
    });
  }
  pushHist(); markDirty(); hint();
};
function appendNode(node){
  const sel = getSelection();
  if (sel.rangeCount && canvas.contains(sel.getRangeAt(0).commonAncestorContainer)) {
    const r = sel.getRangeAt(0);
    r.deleteContents(); r.insertNode(node);
    r.setStartAfter(node); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  } else {
    canvas.appendChild(node); placeCaretAtEnd(canvas);
  }
}
async function uploadImage(file){
  const du = await new Promise((res, rej) => {
    const r = new FileReader(); r.onload = () => res(r.result);
    r.onerror = rej; r.readAsDataURL(file);
  });
  status.textContent = "uploading picture…";
  try {
    const j = await api("/api/upload-image", {name: file.name, dataUri: du});
    status.textContent = "picture added — press Save";
    return j.path;
  } catch (err) { status.textContent = "picture upload failed: " + err.message; return null; }
}
canvas.addEventListener("dragover", e => { if (editing) e.preventDefault(); });
canvas.addEventListener("drop", async e => {
  if (!editing || !e.dataTransfer.files.length) return;
  e.preventDefault();
  let range = null;
  if (document.caretRangeFromPoint) range = document.caretRangeFromPoint(e.clientX, e.clientY);
  for (const f of e.dataTransfer.files) {
    if (!f.type.startsWith("image/")) continue;
    const path = await uploadImage(f);
    if (path) {
      const img = document.createElement("img");
      img.src = path; img.className = "secimg medium"; img.alt = "";
      canvas.focus();
      const sel = getSelection();
      if (range) { sel.removeAllRanges(); sel.addRange(range); }
      if (sel.rangeCount && canvas.contains(sel.getRangeAt(0).commonAncestorContainer)) {
        const r = sel.getRangeAt(0);
        r.deleteContents(); r.insertNode(img);
        r.setStartAfter(img); r.collapse(true);
        sel.removeAllRanges(); sel.addRange(r);
      } else canvas.appendChild(img);
      range = null;
    }
  }
  pushHist(); markDirty(); hint();
});
canvas.addEventListener("paste", async e => {
  if (!editing) return;
  const files = (e.clipboardData && e.clipboardData.files) || [];
  if (!files.length) return;
  e.preventDefault();
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    const path = await uploadImage(f);
    if (path) {
      const img = document.createElement("img");
      img.src = path; img.className = "secimg medium"; img.alt = "";
      appendNode(img);
    }
  }
  pushHist(); markDirty(); hint();
});
canvas.addEventListener("mouseup", ()=>{updateSizeNum();updateFontSel();updateLinkStat();});
canvas.addEventListener("keyup", ()=>{updateSizeNum();updateFontSel();updateLinkStat();});
async function save(){
  readInputsToSite();
  // The Center/Left/Right buttons write text-align; fold alignment into the
  // "ac"/"ar" classes, and keep a validated wheel color, a validated font
  // size and one of the seven toolbar fonts as an inline style, before
  // saving so all four survive the page cleaner. The canonical form is
  // style="color:X;font-size:Ypx;font-family:Z" — anything else is dropped.
  // The server re-validates every property, so nothing hostile survives there.
  canvas.querySelectorAll("[style]").forEach(el => {
    const wasCentered = el.classList.contains("ac");
    const wasRight = el.classList.contains("ar");
    const t = (el.style.textAlign || "").toLowerCase();
    const col = (el.style.color || "").trim();
    const fsz = (el.style.fontSize || "").trim();
    const ffm = (el.style.fontFamily || "").trim();
    el.removeAttribute("style");
    el.classList.remove("ac", "ar");
    if (t === "center" || (wasCentered && t === "")) el.classList.add("ac");
    else if (t === "right" || (wasRight && t === "")) el.classList.add("ar");
    const keep = [];
    const v = col.replace(/\\s+/g, "");
    if (/^(#[0-9a-f]{3,8}|[a-z]{3,30}|rgba?\\([0-9,.%/]+\\)|hsla?\\([0-9,.%/]+(?:deg|turn)?\\))$/i.test(v)
        && !/url|expression|javascript/i.test(v)) {
      keep.push("color:" + v);
    }
    const f = /^(\\d{1,3})px$/.exec(fsz.replace(/\\s+/g, ""));
    if (f && +f[1] >= SIZE_MIN && +f[1] <= SIZE_MAX) {
      keep.push("font-size:" + (+f[1]) + "px");
    }
    if (FONT_OK.has(ffm)) keep.push("font-family:" + ffm);
    if (keep.length) el.setAttribute("style", keep.join(";"));
  });
  ensureExitBlock();
  pushHist();
  const settings = {
    title: SITE.title.slice(0, 300) || "Flamingo and Cactus",
    tagline: SITE.tagline.slice(0, 300),
    footer: SITE.footer.slice(0, 500),
    theme: SITE.theme
  };
  status.textContent = "saving…";
  btnSave.disabled = true;
  try {
    const a = await api("/api/settings", settings);
    SITE = a.site;
    const b = await api("/api/page/save", {slug: SLUG, body: canvas.innerHTML});
    dirty = false; renderAll(); updateSaveBtn();
    status.textContent = "saved — " + (b.note || "built pages updated");
  } catch (err) { status.textContent = "save failed: " + err.message; updateSaveBtn(); }
}
btnSave.onclick = save;
$("#starBadge").addEventListener("click", e => {
  e.preventDefault();
  const h = SITE.pages.find(p => p.hidden && (p.badge || "star") === "star");
  if (h) openPage(h.slug);
});
$("#gemBadge").addEventListener("click", e => {
  e.preventDefault();
  const h = SITE.pages.find(p => p.hidden && p.badge === "gem");
  if (h) openPage(h.slug);
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && (editing || dirty)) { e.preventDefault(); save(); }
  const inEd = editing && document.activeElement && document.activeElement.closest
    && document.activeElement.closest("#canvas,[data-ed]");
  if (inEd && e.altKey && !e.ctrlKey && !e.metaKey && (e.key === "ArrowUp" || e.key === "ArrowDown"))
    { e.preventDefault(); moveBlock(e.key === "ArrowUp" ? -1 : 1); }
  if (inEd && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey)
    { e.preventDefault(); doUndo(); }
  if (inEd && (e.ctrlKey || e.metaKey)
      && (e.key.toLowerCase() === "y"
        || (e.shiftKey && e.key.toLowerCase() === "z")))
    { e.preventDefault(); doRedo(); }
});
/* ---------- exit block: the caret must always have somewhere to go ----------
   A page whose last block is a card, table, divider or gallery traps the
   caret: every click below it lands back inside the last block, so the
   mouse "can't leave the box". Guarantee a plain paragraph at the end of
   the canvas — on load, on page switch, and on every save. */
function ensureExitBlock(){
  const last = canvas.lastElementChild;
  if (!last || last.tagName !== "P") {
    const p = document.createElement("p");
    p.appendChild(document.createElement("br"));
    canvas.appendChild(p);
  }
}
$("#btn-quit").onclick = async () => {
  if (dirty && !confirm("You have unsaved edits. Quit anyway?")) return;
  await fetch("/api/shutdown", {method: "POST"});
  status.textContent = "builder stopped — close this tab";
};
renderAll();
setEditing(false);
ensureExitBlock();
histReset();
</script>
</body>
</html>
"""
    page = page.replace("__CSS__", EDITOR_CSS)
    page = page.replace("__LOGO__", logo_uri)
    page = page.replace("__TITLE__", html.escape(site["title"]))
    page = page.replace("__TITLE_ESC__", html.escape(site["title"]))
    page = page.replace("__TAGLINE_ESC__", html.escape(site["tagline"]))
    page = page.replace("__THEME__", site["theme"])
    page = page.replace("__BODY__", canvas_body)
    page = page.replace("__FORMPREVIEW__", form_leftover)
    page = page.replace("__SITE_JSON__", json.dumps(site, ensure_ascii=False).replace("<", "\\u003c"))
    page = page.replace("__SLUG__", slug)
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "FlamingoSiteBuilder/3.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                site = read_site()
                slug = "index"
                self._send(200, editor_page(site, slug, read_body(slug), live_base()))
            elif url.path == "/built":
                rebuild()
                slug = qs.get("slug", ["index"])[0]
                if not site_build.SLUG_RE.match(slug):
                    slug = "index"
                p = SITE / f"{slug}.html"
                if not p.is_file():
                    p = SITE / "index.html"
                self._send(200, p.read_bytes())
            elif url.path == "/api/site":
                site = read_site()
                site["live"] = live_base()
                self._json(200, site)
            elif url.path == "/api/page":
                slug = qs.get("slug", [""])[0]
                if not site_build.SLUG_RE.match(slug):
                    return self._json(400, {"error": "bad slug"})
                self._json(200, {"slug": slug, "body": read_body(slug)})
            elif url.path.endswith(".html"):
                # Serve a built page directly, e.g. /financials.html, so the
                # local preview matches the live URL structure.
                site = read_site()
                known = {p["slug"] for p in site["pages"]} | {"404"}
                name = url.path.lstrip("/").rsplit(".", 1)[0]
                if site_build.SLUG_RE.match(name) and name in known:
                    try:
                        rebuild()
                    except Exception as e:
                        return self._json(500, {"error": str(e)[:300]})
                    p = SITE / f"{name}.html"
                    if p.is_file():
                        return self._send(200, p.read_bytes())
                return self._json(404, {"error": "not found"})
            elif url.path.startswith("/uploads/"):
                name = safe_upload_name(url.path[len("/uploads/"):])
                for base in (SITE / "uploads", UPLOADS):
                    p = base / name
                    if p.is_file():
                        ctype = "image/png"
                        if p.suffix.lower() in (".jpg", ".jpeg"):
                            ctype = "image/jpeg"
                        elif p.suffix.lower() == ".gif":
                            ctype = "image/gif"
                        self._send(200, p.read_bytes(), ctype)
                        return
                self._json(404, {"error": "no such image"})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(500, {"error": str(e)[:300]})

    def _read_json(self, limit=12_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            raise ValueError("bad request size")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _save_site(self, site: dict) -> str:
        PAGES.mkdir(parents=True, exist_ok=True)
        write_text_atomic(DATA, json.dumps(site, ensure_ascii=False, indent=2) + "\n")
        rebuild()
        paths = [DATA] + [PAGES / f"{p['slug']}.html" for p in site["pages"]] \
            + [SITE, LOGO, UPLOADS]
        return git_add([p for p in paths if p.exists()])

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/settings":
                doc = self._read_json()
                site = read_site()
                site["title"] = str(doc.get("title", ""))[:300] or "Flamingo and Cactus"
                site["tagline"] = str(doc.get("tagline", ""))[:300]
                site["footer"] = str(doc.get("footer", ""))[:500]
                if str(doc.get("theme", "")) in site_build.THEMES:
                    site["theme"] = str(doc.get("theme"))
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/page/save":
                doc = self._read_json()
                slug = str(doc.get("slug", ""))
                if not site_build.SLUG_RE.match(slug):
                    return self._json(400, {"error": "bad slug"})
                site = read_site()
                if slug not in [p["slug"] for p in site["pages"]]:
                    return self._json(400, {"error": "unknown page"})
                clean = site_build.sanitize(str(doc.get("body", ""))[:200000])
                PAGES.mkdir(parents=True, exist_ok=True)
                write_text_atomic(PAGES / f"{slug}.html", clean)
                rebuild()
                note = git_add([PAGES / f"{slug}.html", SITE])
                return self._json(200, {"ok": True, "note": note})
            if url.path == "/api/pages/add":
                doc = self._read_json()
                nav = str(doc.get("nav", ""))[:60].strip() or "New page"
                site = read_site()
                slug = slugify(nav)
                i, base = 1, slug
                taken = {p["slug"] for p in site["pages"]}
                while slug in taken:
                    i += 1
                    slug = f"{base}-{i}"[:40]
                site["pages"].append({"slug": slug, "nav": nav})
                write_text_atomic(PAGES / f"{slug}.html", "")
                note = self._save_site(site)
                return self._json(200, {"ok": True, "slug": slug, "site": site, "note": note})
            if url.path == "/api/pages/rename":
                doc = self._read_json()
                slug = str(doc.get("slug", ""))
                nav = str(doc.get("nav", ""))[:60].strip()
                if not nav:
                    return self._json(400, {"error": "empty name"})
                site = read_site()
                for p in site["pages"]:
                    if p["slug"] == slug:
                        p["nav"] = nav
                        break
                else:
                    return self._json(400, {"error": "unknown page"})
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/pages/delete":
                doc = self._read_json()
                slug = str(doc.get("slug", ""))
                if slug == "index":
                    return self._json(400, {"error": "cannot delete the home page"})
                site = read_site()
                site["pages"] = [p for p in site["pages"] if p["slug"] != slug]
                if len(site["pages"]) == len(read_site()["pages"]):
                    return self._json(400, {"error": "unknown page"})
                (PAGES / f"{slug}.html").unlink(missing_ok=True)
                (SITE / f"{slug}.html").unlink(missing_ok=True)
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/pages/move":
                doc = self._read_json()
                slug = str(doc.get("slug", ""))
                try:
                    direction = 1 if int(doc.get("dir", 0)) > 0 else -1
                except (TypeError, ValueError):
                    return self._json(400, {"error": "bad direction"})
                site = read_site()
                idx = next((i for i, p in enumerate(site["pages"]) if p["slug"] == slug), None)
                if idx is None:
                    return self._json(400, {"error": "unknown page"})
                j = min(len(site["pages"]) - 1, max(0, idx + direction))
                site["pages"][idx], site["pages"][j] = site["pages"][j], site["pages"][idx]
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/upload-image":
                body = self._read_json()
                du = str(body.get("dataUri", ""))
                m = re.fullmatch(r"data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)", du)
                if not m:
                    return self._json(400, {"error": "need a PNG/JPEG/GIF data URI"})
                name = safe_upload_name(str(body.get("name", "image.png")), m.group(1))
                raw = base64.b64decode(m.group(2))
                if len(raw) > 5_000_000:
                    return self._json(400, {"error": "image too big (5 MB max)"})
                UPLOADS.mkdir(parents=True, exist_ok=True)
                dest = UPLOADS / name
                i = 1
                while dest.exists():
                    dest = UPLOADS / f"{dest.stem}-{i}{dest.suffix}"
                    i += 1
                dest.write_bytes(raw)
                rebuild()
                staged = git_add([dest, SITE])
                return self._json(200, {"ok": True, "path": f"uploads/{dest.name}",
                                       "note": staged})
            if url.path == "/api/upload-logo":
                body = self._read_json()
                du = str(body.get("dataUri", ""))
                m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", du)
                if not m:
                    return self._json(400, {"error": "need a PNG data URI"})
                raw = base64.b64decode(m.group(1))
                if len(raw) > 5_000_000:
                    return self._json(400, {"error": "image too big (5 MB max)"})
                if raw[:8] != b"\x89PNG\r\n\x1a\n":
                    return self._json(400, {"error": "not a PNG"})
                LOGO.write_bytes(raw)
                rebuild()
                staged = git_add([LOGO, SITE])
                return self._json(200, {"ok": True, "preview": site_build.logo_data_uri(LOGO),
                                       "note": staged})
            if url.path == "/api/shutdown":
                self._json(200, {"ok": True})
                self.server.shutdown()
                return
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)[:300]})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flamingo & Cactus website builder (loopback only).")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("refusing to bind off loopback")
    rebuild()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"serving {ROOT} at {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

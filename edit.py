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


def safe_upload_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "image.png")).strip("-.") or "image.png"
    if "." not in base:
        base += ".png"
    return base[:80]


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
  .toolbar .sep{width:1px;height:20px;background:#bbb;margin:0 4px}
  .editing [data-ed]{outline:1px dashed #1c4fd8;outline-offset:4px;min-height:1em}
  .editing #canvas{outline-offset:8px;min-height:45vh}
  .editing [data-ed]:focus,.editing #canvas:focus{outline:2px solid #1c4fd8}
  .canvas img.sel{outline:3px solid #1c4fd8}
  #hint{
    display:none;color:var(--faint);font-style:italic;
    padding:8px 0 40px;pointer-events:none;
  }
'''


def editor_page(site: dict, slug: str, body: str) -> bytes:
    logo_uri = site_build.logo_data_uri(LOGO)
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
  <button data-cmd="bold"><b>B</b></button>
  <button data-cmd="italic"><i>I</i></button>
  <button data-cmd="underline"><u>U</u></button>
  <button data-cmd="h2">Heading</button>
  <button data-cmd="p">Text</button>
  <span class="sep"></span>
  <button data-cmd="ul">Bullets</button>
  <button data-cmd="ol">Numbers</button>
  <button data-cmd="quote">Quote</button>
  <span class="sep"></span>
  <button data-cmd="left">Left</button>
  <button data-cmd="center">Center</button>
  <button data-cmd="link">Link</button>
  <button data-cmd="button">Button</button>
  <button data-cmd="unlink">Unlink</button>
  <span class="sep"></span>
  <button data-cmd="image">Picture</button>
  <button data-cmd="gallery">Gallery</button>
  <button data-cmd="divider">Divider</button>
  <button data-cmd="small" disabled>Small</button>
  <button data-cmd="medium" disabled>Medium</button>
  <button data-cmd="full" disabled>Full</button>
  <button data-cmd="delimg" disabled>Remove</button>
  <span class="sep"></span>
  <button data-cmd="clear">Clear style</button>
</div>
<div class="page" id="page">
  <div class="masthead">
    <img id="logo" src="__LOGO__" alt="Flamingo and Cactus">
    <h1 data-ed="title">__TITLE_ESC__</h1>
    <p class="tagline" data-ed="tagline">__TAGLINE_ESC__</p>
  </div>
  <nav class="sitenav" id="nav"></nav>
  <div class="hr"></div>
  <div class="canvas" id="canvas">__BODY__</div>
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
  btnSave.disabled = !on;
  status.textContent = on ? "editing — nothing saved yet" : "viewing";
  hint();
}
function markDirty(msg){
  dirty = true;
  if (editing) status.textContent = msg || "edited — press Save";
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
  if (SITE.pages.length > 1) SITE.pages.forEach(p => {
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
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = p.nav;
    nm.title = p.slug + ".html"; nm.onclick = () => openPage(p.slug);
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
  $("#foot").textContent = SITE.footer || "";
  $("#foot").style.display = SITE.footer ? "" : "none";
  $("#link-built").href = "/built?slug=" + encodeURIComponent(SLUG);
  hint();
}
async function openPage(slug){
  if (slug === SLUG) return;
  if (dirty && !confirm("Switch page without saving?")) return;
  const j = await api("/api/page?slug=" + encodeURIComponent(slug));
  SLUG = slug; canvas.innerHTML = j.body; selImg = null;
  dirty = false; renderAll(); placeCaretStart();
  status.textContent = editing ? "editing — nothing saved yet" : "viewing";
}
function placeCaretStart(){ canvas.focus(); }
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
  const nav = prompt("Name for the new page:", "New page");
  if (!nav) return;
  const j = await api("/api/pages/add", {nav});
  SITE = j.site; dirty = false;
  const b = await api("/api/page?slug=" + encodeURIComponent(j.slug));
  SLUG = j.slug; canvas.innerHTML = b.body; renderAll();
  status.textContent = "page added — it is already saved";
};
async function renamePage(slug, old){
  const nav = prompt("Menu name:", old);
  if (!nav || nav === old) return;
  SITE = (await api("/api/pages/rename", {slug, nav})).site;
  dirty = false; renderAll(); status.textContent = "page renamed — saved";
}
async function deletePage(slug, nav){
  if (!confirm('Delete the page "' + nav + '"? This cannot be undone.')) return;
  SITE = (await api("/api/pages/delete", {slug})).site;
  if (SLUG === slug) {
    SLUG = "index";
    canvas.innerHTML = (await api("/api/page?slug=index")).body;
  }
  dirty = false; renderAll(); status.textContent = "page deleted";
}
async function movePage(slug, dir){
  SITE = (await api("/api/pages/move", {slug, dir})).site;
  dirty = false; renderAll();
}
$("#btn-logo").onclick = async () => {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = "image/png,image/jpeg";
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
$("#toolbar").addEventListener("click", e => {
  const b = e.target.closest("[data-cmd]"); if (!b || !editing) return;
  const c = b.dataset.cmd;
  canvas.focus();
  if (c === "h2") document.execCommand("formatBlock", false, "<h2>");
  else if (c === "p") document.execCommand("formatBlock", false, "<p>");
  else if (c === "ul") document.execCommand("insertUnorderedList");
  else if (c === "ol") document.execCommand("insertOrderedList");
  else if (c === "quote") document.execCommand("formatBlock", false, "<blockquote>");
  else if (c === "left") document.execCommand("justifyLeft");
  else if (c === "center") document.execCommand("justifyCenter");
  else if (c === "bold" || c === "italic" || c === "underline") document.execCommand(c);
  else if (c === "clear") document.execCommand("removeFormat");
  else if (c === "unlink") document.execCommand("unlink");
  else if (c === "link") {
    const u = prompt("Link to (a page or http://, https://, mailto:):", "https://");
    if (u) document.execCommand("createLink", false, u);
  }
  else if (c === "button") {
    const label = prompt("Button words:", "Click me");
    if (!label) return;
    const u = prompt("Button goes to:", "https://");
    if (!u) return;
    const nt = confirm("Open in a new tab when pressed?");
    const extra = nt ? ' target="_blank" rel="noopener"' : "";
    document.execCommand("insertHTML", false,
      '<p><a class="btn" href="' + u.replace(/"/g, "") + '"' + extra + '></a></p>');
    const a = canvas.querySelector("a.btn:empty");
    if (a) { a.textContent = label; placeCaretAtEnd(a.parentElement); }
  }
  else if (c === "divider") document.execCommand("insertHTML", false, "<hr><p><br></p>");
  else if (c === "image") { galleryMode = false; filepick.multiple = false; filepick.click(); }
  else if (c === "gallery") { galleryMode = true; filepick.multiple = true; filepick.click(); }
  else if ((c === "small" || c === "medium" || c === "full") && selImg) {
    selImg.classList.remove("small","medium","full");
    selImg.classList.add("secimg", c);
  }
  else if (c === "delimg" && selImg) { selImg.remove(); selImg = null; }
  else return;
  imgButtons(); markDirty(); hint();
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
  markDirty(); hint();
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
  markDirty(); hint();
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
  markDirty(); hint();
});
async function save(){
  readInputsToSite();
  // The Center/Left buttons write inline styles; fold centering into the
  // "ac" class before saving so it survives the page cleaner and still
  // looks identical here and on the built page.
  canvas.querySelectorAll("[style]").forEach(el => {
    const t = (el.style.textAlign || "").toLowerCase();
    el.removeAttribute("style");
    el.classList.remove("ac");
    if (t === "center") el.classList.add("ac");
  });
  const settings = {
    title: SITE.title.slice(0, 300) || "Flamingo and Cactus",
    tagline: SITE.tagline.slice(0, 300),
    footer: SITE.footer.slice(0, 500),
    theme: SITE.theme
  };
  status.textContent = "saving…";
  try {
    const a = await api("/api/settings", settings);
    SITE = a.site;
    const b = await api("/api/page/save", {slug: SLUG, body: canvas.innerHTML});
    dirty = false; renderAll();
    status.textContent = "saved — " + (b.note || "built pages updated");
  } catch (err) { status.textContent = "save failed: " + err.message; }
}
btnSave.onclick = save;
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && editing) { e.preventDefault(); save(); }
});
$("#btn-quit").onclick = async () => {
  if (dirty && !confirm("You have unsaved edits. Quit anyway?")) return;
  await fetch("/api/shutdown", {method: "POST"});
  status.textContent = "builder stopped — close this tab";
};
renderAll();
setEditing(false);
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
    page = page.replace("__BODY__", body)
    page = page.replace("__SITE_JSON__", json.dumps(site, ensure_ascii=False))
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
            if url.path in ("/", "/index.html"):
                site = read_site()
                slug = "index"
                self._send(200, editor_page(site, slug, read_body(slug)))
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
                self._json(200, read_site())
            elif url.path == "/api/page":
                slug = qs.get("slug", [""])[0]
                if not site_build.SLUG_RE.match(slug):
                    return self._json(400, {"error": "bad slug"})
                self._json(200, {"slug": slug, "body": read_body(slug)})
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
                name = safe_upload_name(str(body.get("name", "image.png")))
                du = str(body.get("dataUri", ""))
                m = re.fullmatch(r"data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)", du)
                if not m:
                    return self._json(400, {"error": "need a PNG/JPEG/GIF data URI"})
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
                m = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", du)
                if not m:
                    return self._json(400, {"error": "need a PNG or JPEG data URI"})
                raw = base64.b64decode(m.group(2))
                if len(raw) > 5_000_000:
                    return self._json(400, {"error": "image too big (5 MB max)"})
                if m.group(1) == "png" and raw[:8] != b"\x89PNG\r\n\x1a\n":
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

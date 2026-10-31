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
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILDER_PORT = 8767   # set from args in main() before the server starts
DATA = ROOT / "data" / "site.json"
PAGES = ROOT / "data" / "pages"
FORM_PATH = ROOT / "data" / "form.json"
CAL_PATH = ROOT / "data" / "calendar.json"
LOGO = ROOT / "assets" / "logo.png"
UPLOADS = ROOT / "assets" / "uploads"
SITE = ROOT / "site"

sys.path.insert(0, str(ROOT / "scripts"))
import build as site_build  # noqa: E402


# Serializes the read-modify-write mutating endpoints. The server is
# threaded, and two overlapping saves (e.g. Ctrl+S during a still-running
# page-settings POST) would otherwise silently lose one of the updates.
# Reentrant so a locked handler can safely call another locked helper.
_MUTEX = threading.RLock()


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


def unique_slug(nav: str, taken: set[str]) -> str:
    base = slugify(nav)
    if base not in taken:
        return base
    stem = base[:30].rstrip("-") or "page"
    i = 2
    while True:
        suffix = f"-{i}"
        candidate = stem[:40 - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
        i += 1


EDITOR_CSS = site_build.CSS + r'''
  #app{display:flex;align-items:stretch;min-height:100vh}
  #side{
    width:280px;flex:0 0 280px;background:#141414;color:#eee;
    font:13px "Helvetica Neue",Arial,sans-serif;
    padding:14px 12px;display:flex;flex-direction:column;gap:8px;
    position:sticky;top:0;height:100vh;overflow:auto;
  }
  #side .side-h{
    display:flex;align-items:center;justify-content:space-between;
    font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:#999;margin:8px 2px 2px;
  }
  #side .side-h button{background:transparent;border:1px solid #555;border-radius:3px;color:#ccc;padding:3px 8px;cursor:pointer;font:11px "Helvetica Neue",Arial,sans-serif;text-transform:none;letter-spacing:0}
  #side .side-h button:hover{background:#333;color:#fff}
  #side ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
  #side li{
    display:flex;align-items:center;gap:3px;border-radius:3px;padding:4px 5px;min-height:34px;
  }
  #side li.cur{background:#2c2c2c}
  #side li.cur.unsaved .nm::after{content:"  ●";color:#e2b84b;font-size:10px}
  #side li .nm{flex:1;min-width:0;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #side li.cur .nm{font-weight:700}
  #side li button{
    background:none;border:1px solid transparent;border-radius:3px;color:#aaa;cursor:pointer;
    font:12px "Helvetica Neue",Arial,sans-serif;padding:3px 5px;min-width:25px;min-height:25px;
  }
  #side li button:hover{color:#fff;border-color:#555;background:#292929}
  #side li .dirtydot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#e2b84b;margin-right:5px;vertical-align:middle}
  #side li .page-kind{font-size:10px;color:#888;letter-spacing:.3px;margin-left:4px}
  #side label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:#bbb;margin:0 2px}
  #side input,#side select,#side textarea{
    background:#222;border:1px solid #444;color:#eee;border-radius:2px;
    font-size:13px;padding:6px 7px;width:100%;
  }
  #side textarea{min-height:58px;resize:vertical;font:12px/1.4 "Helvetica Neue",Arial,sans-serif}
  #side .side-help{font-size:11px;color:#888;line-height:1.4;margin:0 2px}
  #side details.advanced-site{margin:4px 2px}
  #side details.advanced-site summary{cursor:pointer;color:#bbb;font-size:11px;letter-spacing:.5px;padding:5px 0}
  #side details.advanced-site[open] summary{color:#fff}
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
  .editbar a.barlink{display:inline-block;padding:6px 10px;border:1px solid #666;border-radius:2px;color:#fff;text-decoration:none;font:12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;text-transform:uppercase}
  .editbar a.barlink:hover{background:#333}
  .editbar .status{color:#bbb;font-size:12.5px}
  .toolbar{
    display:none;gap:6px;align-items:center;flex-wrap:wrap;
    background:#f2f0eb;border-bottom:1px solid var(--rule);
    padding:8px 14px;position:sticky;top:39px;z-index:40;
    font:12px "Helvetica Neue",Arial,sans-serif;
  }
  .toolgroup{display:flex;align-items:center;gap:5px;border-right:1px solid #d3d0c8;padding:0 8px 0 0;margin-right:2px}
  .toolgroup:last-child{border-right:0}
  .toolgroup .groupname{font:10px "Helvetica Neue",Arial,sans-serif;color:#666;text-transform:uppercase;letter-spacing:.8px;margin-right:2px;white-space:nowrap}
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
  .toolbar #leadNum,.toolbar #trackNum{
    width:52px;text-align:center;font-weight:700;font-size:12px;color:#141414;
    background:#fff;border:1px solid #999;border-radius:2px;padding:3px 4px;
    -moz-appearance:textfield;
  }
  .toolbar #leadNum::-webkit-outer-spin-button,
  .toolbar #leadNum::-webkit-inner-spin-button,
  .toolbar #trackNum::-webkit-outer-spin-button,
  .toolbar #trackNum::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
  .toolbar #leadNum:focus,.toolbar #trackNum:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  .toolbar #fontSel{
    max-width:118px;font-size:12px;color:#141414;
    background:#fff;border:1px solid #999;border-radius:2px;padding:4px 2px;
  }
  .toolbar #fontSel:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  /* the live-site star would sit under the dark page list: shift it right */
  /* the badges belong to the page, not to the toolbar: keep them clear
     of the sticky toolbar and under its z-index so its buttons work */
  .star-badge{left:300px;top:360px;z-index:5;}
  .gem-badge{z-index:5;}
  .gem-badge{top:120px;}
  .toolbar input[type=color]{
    width:26px;height:22px;padding:1px;border:1px solid #999;border-radius:2px;
    background:#fff;cursor:pointer;vertical-align:middle;
  }
  .toolbar .cwheel{display:inline-flex;flex-direction:column;align-items:center;gap:1px;vertical-align:middle}
  .toolbar .cwheel em{font-style:normal;font-size:9px;line-height:10px;color:#333;letter-spacing:.2px}
  .toolbar .sep{width:1px;height:20px;background:#bbb;margin:0 4px}
  #linkStat{font:12px "Helvetica Neue",Arial,sans-serif;color:#bbb;max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ld-back{position:fixed;inset:0;background:rgba(20,20,20,.5);z-index:300;display:flex;align-items:flex-start;justify-content:center;padding:12vh 12px 12px}
  .ld-back[hidden]{display:none}
  .ld-box{background:#fff;color:#141414;border-radius:6px;padding:20px 22px;width:520px;max-width:94vw;max-height:80vh;overflow:auto;box-shadow:0 12px 40px rgba(0,0,0,.4);font:13px "Helvetica Neue",Arial,sans-serif}
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
  .ld-box label.dlg-field input[type=color]{width:46px;height:28px;padding:2px;border:1px solid #999;border-radius:4px;background:#fff;cursor:pointer}
  .ld-box label.dlg-field[hidden]{display:none}
  #linkFillLabel,#linkFgLabel{flex-direction:row;align-items:center;gap:10px;justify-content:space-between;margin:10px 0 3px}
  #builderupdated{background:#263238;color:#fff;padding:10px 16px;font:13px "Helvetica Neue",Arial,sans-serif}
  #builderupdated button{margin:0 8px;padding:4px 12px;background:#1c4fd8;border:none;border-radius:3px;color:#fff;cursor:pointer;font-size:12px}
  #builderupdated span{color:#b0bec5;font-size:12px}
  .ld-btns{display:flex;gap:8px;margin-top:14px}
  .ld-btns button{flex:1;padding:8px;font-size:12px;letter-spacing:.6px;text-transform:uppercase;border-radius:2px;cursor:pointer}
  #linkApply{background:#141414;color:#fff;border:1px solid #141414}
  #linkApply:disabled{opacity:.4;cursor:default}
  #linkCancel,#linkRemove{background:#fff;color:#141414;border:1px solid #999}
  .ld-box .dlg-field{display:flex;flex-direction:column;gap:4px;margin:12px 0}
  .ld-box .dlg-field input,.ld-box .dlg-field select,.ld-box .dlg-field textarea{box-sizing:border-box;width:100%;border:1px solid #aaa;border-radius:3px;padding:8px 9px;font:14px "Helvetica Neue",Arial,sans-serif;color:#141414;background:#fff}
  .ld-box .dlg-field textarea{min-height:68px;resize:vertical}
  .ld-box textarea.html-source{min-height:360px;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre;tab-size:2}
  .ld-box .dlg-row{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
  .ld-box .dlg-row label{display:flex;align-items:center;gap:7px;margin:0;color:#333}
  .dlg-actions{display:flex;gap:8px;margin-top:18px;flex-wrap:wrap}
  .dlg-actions button{border:1px solid #888;border-radius:3px;background:#fff;color:#141414;padding:8px 12px;font:12px "Helvetica Neue",Arial,sans-serif;cursor:pointer}
  .dlg-actions button.primary{background:#141414;color:#fff;border-color:#141414}
  .dlg-actions button.danger{color:#a1261e;border-color:#a1261e;margin-left:auto}
  .dlg-note{font-size:12px;color:#666;line-height:1.45;margin:8px 0}
  .media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:10px;margin-top:12px}
  .media-item{border:1px solid #ccc;border-radius:4px;background:#fff;padding:6px;cursor:pointer;text-align:left;color:#222;min-width:0}
  .media-item:hover,.media-item:focus{border-color:#1c4fd8;outline:1px solid #1c4fd8}
  .media-item img{display:block;width:100%;height:78px;object-fit:contain;background:#f3f3f3}
  .media-item span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;margin-top:5px}
  #blockbar{position:fixed;z-index:90;display:flex;gap:4px;padding:4px;background:#141414;border:1px solid #555;border-radius:4px;box-shadow:0 4px 18px rgba(0,0,0,.25)}
  #blockbar[hidden]{display:none}
  #blockbar button{border:0;border-radius:3px;background:#333;color:#fff;padding:5px 8px;font:12px "Helvetica Neue",Arial,sans-serif;cursor:pointer}
  #blockbar button:hover{background:#1c4fd8}
  #blockbar button.danger:hover{background:#a1261e}
  #imgInspector{display:none;align-items:center;gap:4px}
  #imgInspector input{width:150px;font-size:11px;padding:4px 6px;border:1px solid #999;border-radius:2px}
  .editing #imgInspector.on{display:flex}
  .editing a.bigbtn{cursor:move}
  .editing a.btn{cursor:move}
  .canvas a.bigbtn.sel{outline:3px solid #1c4fd8;outline-offset:3px}
  .canvas a.bigbtn.dragging{opacity:.85;box-shadow:0 18px 40px rgba(0,0,0,.3)}
  .editing a.bigbtn{user-select:none}
  .editing [data-ed]{outline:1px dashed #1c4fd8;outline-offset:4px;min-height:1em}
  .editing #canvas{outline-offset:8px;min-height:45vh}
  .editing [data-ed]:focus,.editing #canvas:focus{outline:2px solid #1c4fd8}
  .canvas img.sel{outline:3px solid #1c4fd8}
  .editing .form-placeholder{padding:12px 14px;border:1px dashed #1c4fd8;background:#f4f7ff;color:#29447a;border-radius:4px;font:13px "Helvetica Neue",Arial,sans-serif}
  #formpreview{max-width:560px;margin:14px auto 0;opacity:.82}
  #formpreview::before{content:"CONTACT FORM PREVIEW";display:block;margin:0 0 4px;color:#777;font:10px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-align:center}
  #hint{
    display:none;color:var(--faint);font-style:italic;
    padding:8px 0 40px;pointer-events:none;
  }
  /* the two generated blocks the owner edits through their own dialogs:
     the contact form and the events calendar. They are drawn here exactly
     as the published site draws them, with a builder-only label so it is
     clear what they are. */
  #canvas [data-form-block],#canvas [data-cal-wrap]{border-radius:4px;margin:14px 0}
  .editing #canvas [data-form-block]{outline:2px dashed #1c4fd8;outline-offset:4px}
  .editing #canvas [data-cal-wrap]{outline:2px dashed #1c4fd8;outline-offset:4px}
  .editing #canvas [data-form-block]::before{
    content:"CONTACT FORM — press Form to change its wording";
    display:block;margin:6px 0 2px;
    font:600 10px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.4px;
    text-transform:uppercase;color:#29447a}
  .editing #canvas [data-cal-wrap]::before{
    content:"CALENDAR — press Events to add or change events; click a day to add one";
    display:block;margin:6px 0 2px;
    font:600 10px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.4px;
    text-transform:uppercase;color:#29447a}
  /* the event list stays out of the editor's way: the calendar is the one
     place events are made, and the list is what it is drawn from */
  #canvas .evsrc{display:none}
  .evcal-label{font:600 15px "Helvetica Neue",Arial,sans-serif;color:var(--ink)}
  .editing .evcal .ev-day{cursor:pointer}
  .editing .evcal .ev-day:hover{transform:scale(1.06);z-index:2}
  #eventsList .evrow{display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid #e6e6e6}
  #eventsList .evrow .evwhen{font:700 12px "Helvetica Neue",Arial,sans-serif;color:#1c4fd8;white-space:nowrap}
  #eventsList .evrow .evwhat{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:13px Georgia,serif;color:#222}
  #eventsList .evrow button{border:1px solid #999;background:#fff;border-radius:3px;padding:3px 9px;font-size:12px;cursor:pointer}
  #eventsList .evrow button:hover{border-color:#1c4fd8;color:#1c4fd8}
  #eventsList .evrow button.del:hover{border-color:#a1261e;color:#a1261e}
  .dlg-field textarea{min-height:64px}
  .linkbtn{border:1px solid #1c4fd8;background:#fff;color:#1c4fd8;border-radius:3px;
    padding:2px 8px;font-size:12px;cursor:pointer}
  .linkbtn:hover{background:#1c4fd8;color:#fff}
  .cal-size{display:inline-flex;align-items:center;gap:6px}
  .cal-size button{border:1px solid #999;background:#fff;border-radius:3px;width:30px;height:28px;cursor:pointer;font:16px "Helvetica Neue",Arial,sans-serif}
  .cal-size button:hover{border-color:#1c4fd8;color:#1c4fd8}
  .cal-size input{padding:4px 6px}
  .cal-preview{margin-left:10px;line-height:1}
  .dlg-field textarea.short{min-height:38px}
'''


def inert_form_html(cfg: dict) -> str:
    """The contact form as it is shown in the editor: it never sends.

    The behaviour script is dropped, the submit is blocked and the send
    button becomes a plain button, so the owner can read (and change) the
    form without a note ever leaving the machine."""
    text = site_build.contact_form_html(cfg)
    text = re.sub(r"<script>[\s\S]*?</script>", "", text, count=1)
    text = text.replace("<form ", '<form onsubmit="return false;" ', 1)
    text = text.replace('<button type="submit">', '<button type="button">', 1)
    text = text.replace('type="file"', 'type="file" disabled', 1)
    return text


def editor_page(site: dict, slug: str, body: str, live: str | None,
                apply_custom_css: bool = True) -> bytes:
    site = dict(site)
    site["live"] = live
    logo_uri = site_build.logo_data_uri(LOGO)
    form_here = any(p.get("slug") == slug and p.get("form") for p in site.get("pages", []))
    # The contact form and the events calendar are real pieces of the site,
    # so the editor shows the real thing where it will sit. Both are
    # generated blocks: they are drawn here and swapped back for their
    # marker paragraph when the page is saved.
    form_cfg = site_build.load_form_config(FORM_PATH.parent)
    if form_here:
        block = ('<div data-form-block contenteditable="false">'
                 + inert_form_html(form_cfg) + "</div>")
        canvas_body, leftover = site_build.place_form(body, block)
        if leftover:
            canvas_body = canvas_body + leftover
    else:
        canvas_body = body
    canvas_body = site_build.place_calendar_static(canvas_body)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Website builder — __TITLE__</title>
<style>
__CSS__
</style>
<style id="site-custom-css">__CUSTOM_CSS__</style>
</head>
<body data-theme="__THEME__" data-star-animation="comic">
<div id="app">
<aside id="side">
  <div class="side-h"><span>Pages</span><button id="btn-help" title="Show editor help">Help</button></div>
  <ul id="pagelist"></ul>
  <div class="row"><button id="btn-addpage">+ Page</button></div>
  <div class="side-h">Site</div>
  <label>Title<input id="in-title"></label>
  <label>Tagline<input id="in-tagline"></label>
  <label>Search description<textarea id="in-description" maxlength="300" placeholder="A short description shown in search and link previews"></textarea></label>
  <label>Footer line<input id="in-footer"></label>
  <label>Theme<select id="in-theme">
    <option value="classic">Classic</option>
    <option value="parchment">Parchment</option>
    <option value="ink">Ink</option>
    <option value="midnight">Midnight</option>
  </select></label>
  <p class="side-help">Star burst: the comic ink-and-paper look is hardcoded. There is no toggle, by design — it is the house style.</p>
  <div class="row"><button id="btn-logo">Logo…</button></div>
  <details class="advanced-site">
    <summary>Advanced design</summary>
    <label>Custom CSS<textarea id="in-css" maxlength="20000" rows="8" placeholder="Example: @media (max-width: 700px) { .star-badge { width: 120px; } }"></textarea></label>
    <p class="side-help">CSS changes the live design. Use Preview before publishing. Scripts and remote imports are blocked.</p>
    <p class="side-help">If your CSS hides the editor, open <code>http://127.0.0.1:8767/?no-css=1</code> to recover it.</p>
    <p class="side-help" id="customCssError" hidden>Remote imports and off-site CSS URLs are blocked. Use an image from uploads/ instead.</p>
    <p class="side-help" id="customCssDisabled" hidden>Custom CSS preview is disabled for recovery. Remove <code>?no-css=1</code> from the address to re-enable it.</p>
  </details>
  <div class="side-h">Check</div>
  <a id="link-built" href="#" target="_blank" rel="noopener">view built page</a>
  <a id="link-live" href="#" target="_blank" rel="noopener">view live site</a>
</aside>
<main id="main">
<div class="editbar">
  <span><b>Builder</b> — <span id="pagename"></span></span>
  <span class="status" id="status">viewing</span>
  <span class="spacer"></span>
  <button id="btn-page-settings" title="Page title, visibility, badge and contact form">Page settings</button>
  <button id="btn-calendar-top" title="Change the shape, size and colour of the calendar marker">Calendar marker</button>
  <button id="btn-edit">Edit</button>
  <button id="btn-save" disabled>Save</button>
  <a id="btn-preview" href="#" target="_blank" rel="noopener" class="barlink">Preview</a>
  <button id="btn-quit" class="warn">Quit</button>
</div>
<div id="builderupdated" hidden>
  New builder code is ready — Restart &amp; load it now.
  <button type="button" id="builderreload">Restart now</button>
  <span>(saving first keeps your changes)</span>
</div>
<div class="toolbar" id="toolbar">
  <div class="toolgroup"><span class="groupname">Edit</span>
  <button data-cmd="undo" id="btn-undo" title="Undo (Ctrl+Z)" disabled>Undo</button>
  <button data-cmd="redo" id="btn-redo" title="Redo (Ctrl+Y)" disabled>Redo</button>
  <button data-cmd="bold"><b>B</b></button>
  <button data-cmd="italic"><i>I</i></button>
  <button data-cmd="underline"><u>U</u></button>
  </div>
  <div class="toolgroup"><span class="groupname">Text</span>
  <button data-cmd="h2">Heading</button>
  <button data-cmd="h3">Subhead</button>
  <button data-cmd="p">Text</button>
  <button data-cmd="smaller" title="Smaller text">A-</button>
  <button data-cmd="bigger" title="Bigger text">A+</button>
  <input type="number" id="sizeNum" min="8" max="200" title="Font size in px">
  <input type="number" id="leadNum" min="1" max="3" step="0.1" title="Line height (1.0–3.0)">
  <input type="number" id="trackNum" min="-5" max="30" step="0.5" title="Letter spacing in px">
  <select id="fontSel" title="Font">
    <option value="">Font: default</option>
    <option value='Georgia,Times New Roman,serif' data-first="georgia">Serif (Georgia)</option>
    <option value='Arial,Helvetica,sans-serif' data-first="arial">Sans (Arial)</option>
    <option value='Times New Roman,Times,serif' data-first="timesnewroman">Times</option>
    <option value='Courier New,Courier,monospace' data-first="couriernew">Mono (Courier)</option>
    <option value='Verdana,Geneva,sans-serif' data-first="verdana">Verdana</option>
  <option value='Trebuchet MS,Verdana,sans-serif' data-first="trebuchetms">Trebuchet</option>
    <option value='Palatino Linotype,Palatino,serif' data-first="palatinolinotype">Palatino</option>
    <option value='Helvetica Neue,Helvetica,Arial,sans-serif' data-first="helveticaneue">Clean (Helvetica)</option>
    <option value='Tahoma,Geneva,sans-serif' data-first="tahoma">Sans (Tahoma)</option>
    <option value='Garamond,Georgia,serif' data-first="garamond">Book (Garamond)</option>
  </select>
  </div>
  <div class="toolgroup"><span class="groupname">Layout</span>
  <button data-cmd="ul">Bullets</button>
  <button data-cmd="ol">Numbers</button>
  <button data-cmd="quote">Quote</button>
  <button data-cmd="left">Left</button>
  <button data-cmd="center">Center</button>
  <button data-cmd="right">Right</button>
  </div>
  <div class="toolgroup"><span class="groupname">Color</span>
  <button data-cmd="color" data-color="" title="Default color">Auto</button>
  <button data-cmd="color" data-color="c-muted" class="swatch" title="Muted" style="background:#7a7a7a"></button>
  <button data-cmd="color" data-color="c-red" class="swatch" title="Red" style="background:#c0392b"></button>
  <button data-cmd="color" data-color="c-blue" class="swatch" title="Blue" style="background:#2f6fd0"></button>
  <button data-cmd="color" data-color="c-green" class="swatch" title="Green" style="background:#2e8b57"></button>
  <button data-cmd="color" data-color="c-gold" class="swatch" title="Gold" style="background:#c9a227"></button>
  <span class="cwheel"><input type="color" id="colorwheel" title="Text color — full wheel" value="#c0392b"><em>Text</em></span>
  <span class="cwheel"><input type="color" id="hlwheel" title="Marker highlight — paints the background behind the text you select" value="#f3d97b"><em>Highlight</em></span>
  </div>
  <div class="toolgroup"><span class="groupname">Links</span>
  <button data-cmd="link">Link</button>
  <button data-cmd="button">Button</button>
  <button data-cmd="bigbtn" title="Huge clickable button box — drag it anywhere">Big button</button>
  <button data-cmd="unlink">Unlink</button>
  <span id="linkStat" title="Put the caret in linked text to see where it goes">no link</span>
  </div>
  <div class="toolgroup"><span class="groupname">Insert</span>
  <button data-cmd="image">Picture</button>
  <button data-cmd="gallery">Gallery</button>
  <button data-cmd="media">Media library</button>
  <button data-cmd="divider">Divider</button>
  <button data-cmd="section" title="Insert a nice section panel">Section</button>
  <button data-cmd="table" title="Insert a 2x2 table">Table</button>
  <button data-cmd="form" id="btn-form-marker" title="Place the contact form at the cursor">Form location</button>
  <button data-cmd="formedit" id="btn-form-edit" title="Change the wording of the contact form on this page">Form</button>
  <button data-cmd="events" id="btn-events" title="Add or change the events and the calendar">Events</button>
  <button data-cmd="calendar" id="btn-calendar" title="Change the shape, size and colour of the calendar marker">Calendar</button>
  <button data-cmd="news" id="btn-news" title="Add a dated news article to the top of the Latest page">News</button>
  </div>
  <div class="toolgroup"><span class="groupname">Image</span>
  <button data-cmd="small" disabled>Small</button>
  <button data-cmd="medium" disabled>Medium</button>
  <button data-cmd="full" disabled>Full</button>
  <button data-cmd="delimg" disabled>Remove</button>
  <div id="imgInspector"><label for="imgAlt">Alt</label><input id="imgAlt" type="text" maxlength="200" placeholder="Describe the image"><button type="button" id="btn-replace-img">Replace</button></div>
  </div>
  <div class="toolgroup"><span class="groupname">Style</span>
  <button data-cmd="clear">Clear style</button>
  <button type="button" id="btn-html-source">HTML source</button>
  </div>
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
    <label class="dlg-field" id="linkFillLabel" hidden>Box color<input type="color" id="linkFill" value="#1c4fd8"></label>
    <label class="dlg-field" id="linkFgLabel" hidden>Text color<input type="color" id="linkFg" value="#ffffff"></label>
    <div class="ld-btns">
      <button id="linkApply" type="button">Apply</button>
      <button id="linkRemove" type="button">Remove link</button>
      <button id="linkCancel" type="button">Cancel</button>
    </div>
  </div>
</div>
<div class="ld-back" id="pageDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="pageDlgTitle">
    <h3 id="pageDlgTitle">Page settings</h3>
    <p class="dlg-note">Control how this page appears in the menu and browser. The URL stays <code id="pageSlug"></code>.</p>
    <label class="dlg-field">Menu name<input id="pageNav" maxlength="60"></label>
    <label class="dlg-field">Browser tab title<input id="pageTitle" maxlength="300" placeholder="Use site title"></label>
    <label class="dlg-field">Search description<textarea id="pageDescription" maxlength="300" placeholder="Use the site description"></textarea></label>
    <div class="dlg-row">
      <label><input type="checkbox" id="pageHidden"> Hide from the main menu</label>
      <label><input type="checkbox" id="pageForm"> Show the contact form on this page</label>
    </div>
    <label class="dlg-field">Badge on this page<select id="pageBadge"><option value="">None (visible page)</option><option value="none">No badge (hidden page)</option><option value="star">Latest and greatest star</option><option value="gem">Feedback badge</option></select></label>
    <p class="dlg-note">Badge pages are hidden from the menu automatically. The contact form appears on a page only when enabled here.</p>
    <div class="dlg-actions">
      <button type="button" class="primary" id="pageApply">Save page settings</button>
      <button type="button" id="pageDuplicate">Duplicate page…</button>
      <button type="button" id="pageCancel">Cancel</button>
      <button type="button" class="danger" id="pageDelete">Delete page</button>
    </div>
  </div>
</div>
<div class="ld-back" id="sourceDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="sourceTitle">
    <h3 id="sourceTitle">Page HTML</h3>
    <p class="dlg-note">Advanced editing for this page only. The cleaner allows safe text, links, images and layout tags; scripts, forms and unsupported markup are removed before preview/save.</p>
    <label class="dlg-field">HTML<textarea class="html-source" id="sourceHtml" spellcheck="false"></textarea></label>
    <p class="dlg-note" id="sourceHint"></p>
    <div class="dlg-actions"><button type="button" class="primary" id="sourceApply">Clean and preview</button><button type="button" id="sourceCancel">Cancel</button></div>
  </div>
</div>
<div class="ld-back" id="addPageDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="addPageTitle">
    <h3 id="addPageTitle">New page</h3>
    <label class="dlg-field">Menu name<input id="newPageName" maxlength="60" placeholder="e.g. About"></label>
    <label class="dlg-field">Start with<select id="newPageStarter"><option value="blank">Blank page</option><option value="copy">Copy the current page</option></select></label>
    <p class="dlg-note">The page is saved immediately. Add your words in the preview, then Save.</p>
    <div class="dlg-actions"><button type="button" class="primary" id="newPageCreate">Create page</button><button type="button" id="newPageCancel">Cancel</button></div>
  </div>
</div>
<div class="ld-back" id="unsavedDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="unsavedTitle">
    <h3 id="unsavedTitle">Unsaved changes</h3>
    <p class="dlg-note">Save your edits before leaving this page? A local draft is kept in this browser until you save or discard it.</p>
    <div class="dlg-actions">
      <button type="button" class="primary" id="unsavedSave">Save and continue</button>
      <button type="button" class="danger" id="unsavedDiscard">Discard edits</button>
      <button type="button" id="unsavedCancel">Stay here</button>
    </div>
  </div>
</div>
<div class="ld-back" id="draftDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="draftTitle">
    <h3 id="draftTitle">Unsaved browser draft</h3>
    <p class="dlg-note">A draft from <span id="draftWhen"></span> is available for this page.</p>
    <div class="dlg-actions">
      <button type="button" class="primary" id="draftRestore">Restore draft</button>
      <button type="button" class="danger" id="draftDiscard">Discard draft</button>
      <button type="button" id="draftLater">Not now</button>
    </div>
  </div>
</div>
<div class="ld-back" id="mediaDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="mediaDlgTitle">
    <h3 id="mediaDlgTitle">Media library</h3>
    <p class="dlg-note">Choose an image already uploaded to this site, or upload a new one.</p>
    <div class="ld-btns"><button id="mediaUpload" type="button">Upload image</button><button id="mediaClose" type="button">Close</button></div>
    <div id="mediaGrid" class="media-grid"></div>
    <p id="mediaEmpty" class="dlg-note" hidden>No uploaded images yet.</p>
  </div>
</div>
<div class="ld-back" id="formDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="formDlgTitle">
    <h3 id="formDlgTitle">Contact form wording</h3>
    <p class="dlg-note">These are the words on the contact form. The form itself keeps working the same way — only the wording changes.</p>
    <label class="dlg-field">Email subject<input id="formSubject" maxlength="200" autocomplete="off"></label>
    <label class="dlg-field">Name field label<input id="formNameLabel" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Name field hint<textarea class="short" id="formNameSub" maxlength="300"></textarea></label>
    <label class="dlg-field">Name field placeholder<input id="formNamePh" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Email or phone field label<input id="formContactLabel" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Email or phone field hint<textarea class="short" id="formContactSub" maxlength="300"></textarea></label>
    <label class="dlg-field">Email or phone field placeholder<input id="formContactPh" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Note field label<input id="formNoteLabel" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Note field placeholder<input id="formNotePh" maxlength="200" autocomplete="off"></label>
    <label class="dlg-field">Files field label<input id="formFilesLabel" maxlength="120" autocomplete="off"></label>
    <label class="dlg-field">Files field hint<textarea class="short" id="formFilesSub" maxlength="300"></textarea></label>
    <label class="dlg-field">Send button text<input id="formButton" maxlength="60" autocomplete="off"></label>
    <details class="dlg-section" id="formFailoverSection">
      <summary>Third-party services + failover</summary>
      <p class="dlg-note">Every note is sent once, through FormInit only — there is no backup chain, because a backup that fires after an unconfirmed send is how one note became two emails. The failover name and Web3Forms fields below are no longer used by the page. For FormInit, create a free form at <a href="https://forminit.com" target="_blank" rel="noopener">forminit.com</a>, set it to Public, and paste its form ID — never an API key.</p>
      <label class="dlg-field">Primary service name<input id="formPrimaryService" maxlength="120" autocomplete="off"></label>
      <label class="dlg-field">Failover service name<input id="formFailoverService" maxlength="120" autocomplete="off"></label>
      <label class="dlg-field">Web3Forms access key
        <input id="formWeb3Key" maxlength="256" autocomplete="off" spellcheck="false" placeholder="Paste your web3forms.com access key here">
        <span class="dlg-hint">Stored in <code>data/form.json</code> only. Built into the page once, never echoed back to this dialog.</span>
      </label>
      <label class="dlg-field">Second failover service name<input id="formSecondFailover" maxlength="120" autocomplete="off"></label>
      <label class="dlg-field">FormInit form ID
        <input id="formInitId" maxlength="128" autocomplete="off" spellcheck="false" placeholder="Paste your forminit.com public form ID here">
        <span class="dlg-hint">Public form ID only (dashboard, form Settings, Public mode). It ships in the page by design — API keys must never go here.</span>
      </label>
      <label class="dlg-field">Disclaimer title<input id="formDisclaimerTitle" maxlength="120" autocomplete="off"></label>
      <label class="dlg-field">Disclaimer body<textarea id="formDisclaimerBody" maxlength="1200"></textarea></label>
    </details>
    <div class="dlg-row">
      <label><input type="checkbox" id="formShowContact"> Show the email-or-phone field</label>
      <label><input type="checkbox" id="formShowFiles"> Show the file picker</label>
    </div>
    <div class="dlg-actions">
      <button type="button" class="primary" id="formApply">Apply to the form</button>
      <button type="button" id="formReset">Original wording</button>
      <button type="button" id="formCancel">Cancel</button>
    </div>
  </div>
</div>
<div class="ld-back" id="eventsDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="eventsDlgTitle">
    <h3 id="eventsDlgTitle">Events</h3>
    <p class="dlg-note">Every event here becomes a mark on the calendar. Click a day on the calendar to add or change an event on it.</p>
    <div id="eventsList"></div>
    <div class="dlg-actions">
      <button type="button" class="primary" id="eventAdd">Add an event</button>
      <button type="button" id="eventsClose">Done</button>
    </div>
  </div>
</div>
<div class="ld-back" id="calDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="calDlgTitle">
    <h3 id="calDlgTitle">Calendar marker</h3>
    <p class="dlg-note">How the days that have events are marked on the calendar — on the published site too.</p>
    <div class="dlg-row">
      <label class="dlg-field">Shape<select id="calSymbol"></select></label>
      <label class="dlg-field">Colour<input id="calColor" type="color" value="#1a7f37"></label>
    </div>
    <label class="dlg-field">Size
      <span class="cal-size">
        <button type="button" id="calSmaller" title="Smaller">&minus;</button>
        <input id="calSize" type="number" min="8" max="48" step="1" style="width:76px">
        <button type="button" id="calBigger" title="Bigger">+</button>
        <span id="calPreview" class="cal-preview">&bull;</span>
      </span>
    </label>
    <div class="dlg-actions">
      <button type="button" class="primary" id="calApply">Done</button>
      <button type="button" id="calCancel">Cancel</button>
    </div>
  </div>
</div>
<div class="ld-back" id="eventDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="eventDlgTitle">
    <h3 id="eventDlgTitle">Add an event</h3>
    <p class="dlg-note" id="evWhen">Saturday, November 14, 2026</p>
    <div id="evDayList"></div>
    <label class="dlg-field">Year<input id="evYear" type="number" min="2000" max="2100"></label>
    <label class="dlg-field">Time (optional)<input id="evTime" maxlength="60" autocomplete="off" placeholder="e.g. 9 am to 3 pm"></label>
    <label class="dlg-field">What is it<textarea id="evTitle" maxlength="300"></textarea></label>
    <p class="dlg-note">A link goes right in the text, in square brackets: check the Rotary website <b>[here](https://owegorotary.org/)</b></p>
    <p class="dlg-note">The shape, size and colour of the mark on the calendar: <button type="button" id="evMarkerBtn" class="linkbtn">change the marker</button></p>
    <div class="dlg-actions">
      <button type="button" class="primary" id="evSave">Add event</button>
      <button type="button" class="danger" id="evDelete" hidden>Remove event</button>
      <button type="button" id="evCancel">Cancel</button>
    </div>
  </div>
</div>
<div class="ld-back" id="newsDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="newsDlgTitle">
    <h3 id="newsDlgTitle">Add a news article</h3>
    <p class="dlg-note">Goes to the top of the page, above older articles — newest first.</p>
    <label class="dlg-field">Headline<input id="newsHeadline" maxlength="200" autocomplete="off" placeholder="What happened"></label>
    <label class="dlg-field">Date<input id="newsDate" type="date"></label>
    <label class="dlg-field">Article<textarea id="newsBody" maxlength="5000" rows="6" placeholder="Tell it like a news story"></textarea></label>
    <p class="dlg-note">A blank line starts a new paragraph.</p>
    <div class="dlg-actions">
      <button type="button" class="primary" id="newsAdd">Add article</button>
      <button type="button" id="newsCancel">Cancel</button>
    </div>
  </div>
</div>
<div class="ld-back" id="helpDlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="helpTitle">
    <h3 id="helpTitle">Using the site editor</h3>
    <p><b>1. Pick a page.</b> Use the Pages list to switch pages. Use Page settings to rename, hide, add a badge or enable the contact form.</p>
    <p><b>2. Edit the preview.</b> Press Edit, then click text and type. Use the labeled toolbar groups to format text and add pictures, links, sections, tables and news articles.</p>
    <p><b>Colors are for text.</b> The "Text" wheel and the color swatches color the letters themselves — a click inside a paragraph colors just the run of text the caret sits in. The "Highlight" wheel is a marker pen: it paints the background behind the word you clicked into or the text you selected, never a whole block.</p>
    <p><b>3. Manage content.</b> Hover a page block for move, duplicate and delete controls. Select an image to change its size, alignment or alt text.</p>
    <p><b>4. Save and publish.</b> Save writes the editable source and stages the built site. Commit and push in GitKraken to publish.</p>
    <p class="dlg-note">Shortcuts: Ctrl+S save · Ctrl+Z undo · Ctrl+Y redo · Alt+↑/↓ move a block. A draft is saved in this browser while you edit. If custom CSS hides the editor, open /?no-css=1.</p>
    <div class="dlg-actions"><button type="button" class="primary" id="helpClose">Got it</button></div>
  </div>
</div>
<div class="ld-back" id="bigbtndlg" hidden>
  <div class="ld-box" role="dialog" aria-modal="true" aria-labelledby="bigbtnTitle">
    <h3 id="bigbtnTitle">Big button</h3>
    <p class="dlg-note">A huge rounded box the visitor can click anywhere on. Drag it anywhere on the page while editing.</p>
    <label class="dlg-field">Words on the box<input id="bigbtnText" maxlength="120" autocomplete="off" placeholder="e.g. Contact us"></label>
    <label class="dlg-field">Goes to<input id="bigbtnUrl" inputmode="url" maxlength="500" autocomplete="off" placeholder="Pick a page below, or paste https://…"></label>
    <div class="ld-hint" id="bigbtnHint"></div>
    <div class="ld-pages" id="bigbtnPages"></div>
    <div class="dlg-row">
      <label>Box color <input type="color" id="bigbtnBg" value="#1c4fd8"></label>
      <label>Text color <input type="color" id="bigbtnFg" value="#ffffff"></label>
    </div>
    <label class="ld-check"><input type="checkbox" id="bigbtnBlank"> Open in a new tab</label>
    <div class="dlg-actions">
      <button type="button" class="primary" id="bigbtnApply">Apply</button>
      <button type="button" class="danger" id="bigbtnRemove">Remove box</button>
      <button type="button" id="bigbtnCancel">Cancel</button>
    </div>
  </div>
</div>
<div id="blockbar" hidden aria-label="Selected block actions">
  <button type="button" data-block-action="up" title="Move block up">↑</button>
  <button type="button" data-block-action="down" title="Move block down">↓</button>
  <button type="button" data-block-action="duplicate">Duplicate</button>
  <button type="button" class="danger" data-block-action="delete">Delete</button>
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
  <div id="hint">Blank page — press Edit and start typing, use the toolbar for blocks, or drop a picture in.</div>
  <footer id="foot"></footer>
</div>
</main>
</div>
<input type="file" id="filepick" accept="image/png,image/jpeg,image/gif" style="display:none">
<script>
let SITE = __SITE_JSON__;
let SLUG = "__SLUG__";
/* the contact form's wording: what the server has on disk (FORM) and the
   original wording it falls back to (FORM_BASE) */
let FORM = __FORM_JSON__;
let FORM_BASE = __FORM_DEFAULTS__;
/* ---------- builder self-update: never need Quit to pick up new code.
   The page and /api/pping carry (pid, stamp-of-edit.py). New pid = the
   server restarted itself -> reload and get the fresh code. Same pid but
   a new stamp = edit.py changed on disk -> offer in-page Restart; the
   server swaps itself (os.execv), then the poll reloads. Unsaved work is
   saved first; if a dialog is open the banner only ever offers. */
let BPING0 = "__BPING__";
let __fcRestarted = false;
function flushDialogGuard(){ return !!document.querySelector(".ld-back:not([hidden])"); }
function restartBuilderNow(){
  const go = async () => {
    if (dirty) {
      const okSave = await save();
      if (!okSave) { status.textContent = "Fix the save first, then press Restart again."; return; }
    }
    __fcRestarted = true;
    let ok = false;
    try {
      const r = await fetch("/api/restart", { method: "POST" });
      ok = r.ok;
    } catch (_) { ok = false; }
    if (!ok) {
      __fcRestarted = false;
      status.textContent = "The builder refused to restart — Quit and relaunch.";
      return;
    }
    setTimeout(checkBuilderStamp, 1200);   // prompt reload at next poll
  };
  go();
}
function checkBuilderStamp(){
  api("/api/ping").then(j => {
    if (!j || !j.pid) return;
    const busy = flushDialogGuard();
    // execv keeps the process id, so a deliberate restart is seen as a
    // stamp change with __fcRestarted set: that is fresh code, reload now
    if (j.pid !== BPING0.pid) {
      if (busy) { showUpdatedBanner(); return; }
      location.reload(); return;
    }
    if (j.stamp === BPING0.stamp) return;
    if (__fcRestarted) {
      if (busy) { showUpdatedBanner(); return; }
      location.reload(); return;
    }
    showUpdatedBanner();   // edit.py changed on disk — offer, never force
  }).catch(() => {});    // server gone (Quit pressed) — leave the page alone
}
function showUpdatedBanner(){
  const bar = document.getElementById("builderupdated");
  if (!bar) return;
  const openDialog = flushDialogGuard();
  bar.hidden = false;
  bar.querySelector("span").textContent = openDialog
    ? "(close the open dialog first — a click here doesn't touch it)"
    : "(saving first keeps your changes)";
}
setInterval(checkBuilderStamp, 20000);
setTimeout(checkBuilderStamp, 2500);
const CUSTOM_CSS_PREVIEW_ENABLED = __CUSTOM_CSS_ENABLED__;
let editing = false, dirty = false, selImg = null, galleryMode = false, replaceTarget = null, mediaTarget = null, mediaRange = null;
let editRevision = 0;
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const canvas = $("#canvas"), status = $("#status"),
      btnEdit = $("#btn-edit"), btnSave = $("#btn-save"), filepick = $("#filepick");
let saving = false, draftTimer = null, unsavedResolve = null, pageSettingsSlug = null;
let draftResolve = null;
let newPageSource = null;
const draftKey = slug => "fc-site-draft:" + slug;
const LAST_PAGE_KEY = "fc-site-last-page";
function globalSettings(site){
  return {title:site.title || "", tagline:site.tagline || "", description:site.description || "",
          footer:site.footer || "", theme:site.theme || "classic", custom_css:site.custom_css || ""};
}
let serverSiteSnapshot = JSON.stringify(globalSettings(SITE));

async function api(path, payload){
  const o = {method: payload === undefined ? "GET" : "POST", cache: "no-store"};
  if (payload !== undefined) { o.headers = {"Content-Type": "application/json"}; o.body = JSON.stringify(payload); }
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function replaceSite(next){ SITE = Object.assign({}, next, {live:SITE.live}); serverSiteSnapshot = JSON.stringify(globalSettings(SITE)); }
async function previewCurrent(e){
  if (e) e.preventDefault();
  const tab = window.open("about:blank", "_blank");
  if (!tab) { status.textContent = "Allow popups to open the page preview"; return; }
  if (dirty && !(await confirmLeave())) { tab.close(); return; }
  tab.opener = null;
  tab.location.replace("/built?slug=" + encodeURIComponent(SLUG));
}
function setEditing(on){
  editing = on;
  if (!on && typeof blockbar !== "undefined") blockbar.hidden = true;
  document.body.classList.toggle("editing", on);
  canvas.contentEditable = on ? "true" : "false";
  $$("[data-ed]").forEach(el => el.contentEditable = on ? "true" : "false");
  btnEdit.textContent = on ? "Viewing" : "Edit";
  updateSaveBtn();
  if (dirty) status.textContent = "Unsaved changes — Save to stage";
  else status.textContent = on ? "editing — nothing saved yet" : "viewing";
  hint();
}
function updateSaveBtn(){
  btnSave.disabled = saving || !(editing || dirty);
}
function writeDraft(){
  clearTimeout(draftTimer);
  if (!dirty) return;
  draftTimer = setTimeout(persistDraft, 350);
}
function persistDraft(){
  if (!dirty) return;
  try {
    localStorage.setItem(draftKey(SLUG), JSON.stringify({
      body: canvas.innerHTML,
        site: globalSettings(SITE), siteBase:serverSiteSnapshot,
      form: FORM,
      savedAt: Date.now()
    }));
  } catch (e) { status.textContent = "Unsaved changes — browser draft storage is full"; }
}
function clearDraft(slug=SLUG){
  clearTimeout(draftTimer);
  try { localStorage.removeItem(draftKey(slug)); } catch (e) {}
}
function markDirty(msg){
  dirty = true;
  editRevision++;
  updateSaveBtn();
  const currentRow = $("#pagelist li.cur"); if (currentRow) currentRow.classList.add("unsaved");
  status.textContent = msg || "Unsaved changes — Save to stage";
  writeDraft();
}
function askUnsaved(){
  return new Promise(resolve => { unsavedResolve = resolve; $("#unsavedDlg").hidden = false; });
}
function settleUnsaved(choice){
  $("#unsavedDlg").hidden = true;
  const resolve = unsavedResolve; unsavedResolve = null;
  if (resolve) resolve(choice);
}
$("#unsavedSave").onclick = () => settleUnsaved("save");
$("#unsavedDiscard").onclick = () => settleUnsaved("discard");
$("#unsavedCancel").onclick = () => settleUnsaved("stay");
async function discardCurrentDraft(){
  clearDraft(SLUG);
  const [site, page] = await Promise.all([
    api("/api/site"), api("/api/page?slug=" + encodeURIComponent(SLUG))
  ]);
  editRevision++; replaceSite(site); canvas.innerHTML = page.body; dirty = false; selImg = null; syncFormMarker();
  syncFormMarker(); histReset(); renderAll(); updateSaveBtn(); imgButtons();
  status.textContent = "Edits discarded — showing the saved page";
}
async function confirmLeave(){
  if (saving) { status.textContent = "Save is still running — wait before switching pages"; return false; }
  if (!dirty) return true;
  writeDraft();
  const choice = await askUnsaved();
  if (choice === "stay") return false;
  if (choice === "save") { await save(); return !dirty; }
  try { await discardCurrentDraft(); return true; }
  catch (err) { status.textContent = "could not discard draft: " + err.message; return false; }
}
function askDraftChoice(when){
  return new Promise(resolve => {
    draftResolve = resolve;
    $("#draftWhen").textContent = when;
    $("#draftDlg").hidden = false;
  });
}
function settleDraft(choice){
  $("#draftDlg").hidden = true;
  const resolve = draftResolve; draftResolve = null;
  if (resolve) resolve(choice);
}
$("#draftRestore").onclick = () => settleDraft("restore");
$("#draftDiscard").onclick = () => settleDraft("discard");
$("#draftLater").onclick = () => settleDraft("later");
async function restoreDraft(){
  let draft;
  try { draft = JSON.parse(localStorage.getItem(draftKey(SLUG)) || "null"); }
  catch (e) { clearDraft(SLUG); return false; }
  if (!draft || typeof draft.body !== "string") return false;
  const when = draft.savedAt ? new Date(draft.savedAt).toLocaleString() : "earlier";
  const choice = await askDraftChoice(when);
  if (choice === "later") return false;
  if (choice === "discard") { clearDraft(SLUG); return false; }
  canvas.innerHTML = draft.body; selImg = null; replaceTarget = null;
  if (draft.form && typeof draft.form === "object") FORM = Object.assign({}, FORM, draft.form);
  const siteSettingsCurrent = !!draft.siteBase && draft.siteBase === serverSiteSnapshot;
  if (draft.site && siteSettingsCurrent) SITE = Object.assign({}, SITE, draft.site);
  dirty = true; ensureExitBlock(); renderAll(); histReset(); updateSaveBtn();
  status.textContent = siteSettingsCurrent ? "Draft restored — review it, then Save to stage"
                                          : "Page draft restored; newer saved site settings kept";
  return true;
}
window.addEventListener("beforeunload", e => {
  if (!dirty) return;
  clearTimeout(draftTimer); persistDraft(); e.preventDefault(); e.returnValue = "";
});
function hint(){
  $("#hint").style.display =
    (editing && !canvas.innerText.trim() && !canvas.querySelector("img")) ? "block" : "none";
}
function renderAll(){
  document.body.dataset.theme = SITE.theme;
  document.body.dataset.starAnimation = "comic";
  applyCustomCssPreview();
  document.querySelector('[data-ed="title"]').innerText = SITE.title;
  document.querySelector('[data-ed="tagline"]').innerText = SITE.tagline;
  $("#in-title").value = SITE.title; $("#in-tagline").value = SITE.tagline;
  $("#in-description").value = SITE.description || "";
  $("#in-footer").value = SITE.footer || ""; $("#in-theme").value = SITE.theme;
  $("#in-css").value = SITE.custom_css || "";
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
    if (p.slug === SLUG) li.className = "cur" + (dirty ? " unsaved" : "");
    const nm = document.createElement("span"); nm.className = "nm";
    const badge = p.badge === "gem" ? "◇ " : (p.hidden && p.badge !== "none" ? "★ " : "");
    nm.textContent = badge + p.nav;
    nm.title = p.slug + ".html" + (p.hidden ? " (hidden page, not in the menu)" : ""); nm.onclick = () => openPage(p.slug);
    li.appendChild(nm);
    if (p.form || (p.hidden && p.badge === "none")) {
      const kind = document.createElement("span"); kind.className = "page-kind";
      kind.textContent = p.form ? "form" : "hidden"; li.appendChild(kind);
    }
    const up = document.createElement("button"); up.textContent = "↑"; up.title = "Move page up"; up.setAttribute("aria-label", "Move " + p.nav + " up");
    up.onclick = () => movePage(p.slug, -1); li.appendChild(up);
    const dn = document.createElement("button"); dn.textContent = "↓"; dn.title = "Move page down"; dn.setAttribute("aria-label", "Move " + p.nav + " down");
    dn.onclick = () => movePage(p.slug, 1); li.appendChild(dn);
    const settings = document.createElement("button"); settings.textContent = "⚙"; settings.title = "Page settings";
    settings.setAttribute("aria-label", "Settings for " + p.nav); settings.onclick = () => openPageSettings(p.slug); li.appendChild(settings);
    ul.appendChild(li);
  });
  const cur = SITE.pages.find(p => p.slug === SLUG);
  $("#pagename").textContent = cur ? cur.nav : SLUG;
  $("#btn-page-settings").disabled = !cur;
  const fp = document.getElementById("formpreview");
  if (fp) fp.style.display = (cur && cur.form) ? "" : "none";
  const fm = $("#btn-form-marker"); if (fm) fm.disabled = !(cur && cur.form);
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
  $("#btn-preview").href = "/built?slug=" + encodeURIComponent(SLUG);
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
  imgButtons();
}
$("#link-built").addEventListener("click", previewCurrent);
$("#btn-preview").addEventListener("click", previewCurrent);
/* ---------- the contact form and the events calendar ------------------
   Both are generated blocks: the editor draws the real thing where it
   will sit, and swaps it back for its marker paragraph when the page is
   saved. The form's wording lives in data/form.json; the events live in
   the event list on the page and the calendar is drawn from them. */
const MONTH_NAMES = ["January","February","March","April","May","June","July",
                     "August","September","October","November","December"];
let CAL_Y = 0, CAL_M = 0, EVENTS = [], EVENT_EDIT = null, EVENT_DAY = null, evTimer = null;
let CAL_CFG = {symbol: "●", color: "#1a7f37", size: 17};
const FORM_FIELDS = [
  ["subject", "#formSubject"], ["name_label", "#formNameLabel"],
  ["name_sub", "#formNameSub"], ["name_placeholder", "#formNamePh"],
  ["contact_label", "#formContactLabel"], ["contact_sub", "#formContactSub"],
  ["contact_placeholder", "#formContactPh"], ["note_label", "#formNoteLabel"],
  ["note_placeholder", "#formNotePh"], ["files_label", "#formFilesLabel"],
  ["files_sub", "#formFilesSub"], ["button", "#formButton"],
  ["primary_service", "#formPrimaryService"],
  ["failover_service", "#formFailoverService"],
  ["web3forms_access_key", "#formWeb3Key"],
  ["second_failover_service", "#formSecondFailover"],
  ["forminit_form_id", "#formInitId"],
  ["disclaimer_title", "#formDisclaimerTitle"],
  ["disclaimer_body", "#formDisclaimerBody"]
];
function formBlockEl(){ return canvas.querySelector("[data-form-block]"); }
function calWrapEl(){ return canvas.querySelector("[data-cal-wrap]"); }
function evSrcEl(){ return canvas.querySelector(".evsrc"); }
function escText(s){ return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function escAttr(s){ return escText(s).replace(/"/g,"&quot;"); }
function markerPara(text){ const p = document.createElement("p"); p.textContent = text; return p; }

async function renderFormBlock(){
  const page = SITE.pages.find(p => p.slug === SLUG);
  let block = formBlockEl();
  if (!page || !page.form) { if (block) block.remove(); return; }
  let j;
  try { j = await api("/api/form", {form: FORM}); } catch(e){ return; }
  if (j && j.form) FORM = j.form;
  if (!block) {
    const marker = Array.from(canvas.querySelectorAll("p")).find(el => el.textContent.trim() === "[contact form]");
    block = document.createElement("div");
    block.setAttribute("data-form-block", "");
    block.contentEditable = "false";
    if (marker) marker.replaceWith(block); else canvas.appendChild(block);
  }
  block.innerHTML = j.html || "";
}

let calReq = 0;
async function renderCalBlock(){
  const src = evSrcEl() ? evSrcEl().innerHTML : "";
  const mine = ++calReq;
  let j;
  try { j = await api("/api/events", {block: src, y: CAL_Y, m: CAL_M, symbol: CAL_CFG.symbol, color: CAL_CFG.color, size: CAL_CFG.size}); }
  catch(e){ return; }
  if (mine !== calReq) return;   // a newer request already drew the calendar
  EVENTS = j.events || [];
  CAL_Y = j.y || CAL_Y; CAL_M = j.m || CAL_M;
  let wrap = calWrapEl();
  if (!wrap) {
    const marker = Array.from(canvas.querySelectorAll("p")).find(el => el.textContent.trim() === "[event calendar]");
    if (!marker) return;
    wrap = document.createElement("div");
    wrap.setAttribute("data-cal-wrap", "");
    wrap.contentEditable = "false";
    marker.replaceWith(wrap);
  }
  wrap.innerHTML = j.html || "";
}
function scheduleEventsRefresh(){
  clearTimeout(evTimer);
  evTimer = setTimeout(() => { renderCalBlock(); }, 450);
}

/* ---- the event list on the page is what the calendar is drawn from ---- */
function safeUrl(url){
  const u = String(url || "").trim();
  if (!u) return "";
  if (/^(https?:|mailto:|tel:)/i.test(u)) return u;
  if (/^[/#?]/.test(u)) return u;           // a page of this site
  return "";
}
/* the text of an event may carry links written as [words](address) */
function textToEventHtml(text){
  let out = escText(text);
  out = out.replace(/\\[([^\\]]*)\\]\\(([^)\\s]+)\\)/g, function(m, words, url){
    const ok = safeUrl(url);
    if (!ok) return words;
    return '<a href="' + escAttr(ok) + '">' + words + "</a>";
  });
  return out;
}
/* an event that came from the page: show its text with the link inline */
function eventInlineText(ev){
  if (ev && ev.html) {
    let h = String(ev.html).replace(/<a\\b[^>]*href="([^"]*)"[^>]*>([\\s\\S]*?)<\\/a>/gi,
      function(m, url, words){ return "[" + words.replace(/<[^>]*>/g, "") + "](" + url + ")"; });
    h = h.replace(/<[^>]*>/g, "");
    const m = /^[A-Z][a-z]+ \\d{1,2}(?:st|nd|rd|th), \\d{4}(?:,\\s*[^—]*)?\\s*[—-]\\s*([\\s\\S]*)$/.exec(h.trim());
    if (m) return m[1].trim();
    if (h.trim()) return h.trim();
  }
  return (ev && ev.label) || "";
}
function eventOrdinal(n){
  const s = ["th","st","nd","rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
function eventPara(ev){
  let text = MONTH_NAMES[ev.month - 1] + " " + eventOrdinal(ev.day) + ", " + ev.year;
  if (ev.time) text += ", " + ev.time;
  text += " — " + ev.title;
  return textToEventHtml(text);
}
function ensureEvSrc(){
  let block = evSrcEl();
  if (block) return block;
  block = document.createElement("div");
  block.className = "evsrc";
  const wrap = calWrapEl();
  if (wrap && wrap.parentNode) wrap.parentNode.insertBefore(block, wrap.nextSibling);
  else canvas.appendChild(block);
  return block;
}
function writeEvent(ev, editIdx){
  const block = ensureEvSrc();
  const paras = Array.from(block.querySelectorAll("p, li"));
  const p = document.createElement("p");
  p.innerHTML = eventPara(ev);
  if (editIdx != null && paras[editIdx]) paras[editIdx].replaceWith(p);
  else block.appendChild(p);
  // show the month the event now sits in
  CAL_Y = ev.year; CAL_M = ev.month;
  markDirty();
  renderCalBlock();
}
async function deleteEvent(ev){
  const block = evSrcEl();
  if (!block) return;
  const paras = Array.from(block.querySelectorAll("p, li"));
  if (paras[ev.idx]) paras[ev.idx].remove();
  markDirty();
  await renderCalBlock();
}
function fillEventForm(ev, dateStr){
  let y, m, d;
  if (ev) { y = ev.y; m = ev.m; d = ev.d; }
  else if (dateStr) { const bits = String(dateStr).split("-").map(Number); y = bits[0]; m = bits[1]; d = bits[2]; }
  else { const now = new Date(); y = now.getFullYear(); m = now.getMonth() + 1; d = now.getDate(); }
  EVENT_DAY = {y: y, m: m, d: d};
  EVENT_EDIT = ev || null;
  let when = "";
  try { when = new Date(y, m - 1, d).toLocaleDateString("en-US",
        {weekday:"long", month:"long", day:"numeric", year:"numeric"}); } catch(e) {}
  $("#evWhen").textContent = when;
  $("#evYear").value = String(y);
  $("#evTime").value = ev ? (ev.times || "") : "";
  $("#evTitle").value = ev ? eventInlineText(ev) : "";
  $("#evSave").textContent = ev ? "Save changes" : "Add event";
  $("#evDelete").hidden = !ev;
  eventDayList(EVENT_DAY);
  $("#eventsDlg").hidden = true;
  $("#eventDlg").hidden = false;
  $("#evTitle").focus();
}
function eventDayList(day){
  const list = $("#evDayList");
  list.innerHTML = "";
  if (!day) return;
  const mine = EVENTS.filter(e => e.y === day.y && e.m === day.m && e.d === day.d);
  if (!mine.length) return;
  const head = document.createElement("p");
  head.className = "dlg-note";
  head.textContent = "Already on this day:";
  list.appendChild(head);
  mine.forEach(ev => {
    const row = document.createElement("div"); row.className = "evrow";
    const what = document.createElement("span"); what.className = "evwhat";
    what.textContent = (ev.times ? ev.times + " · " : "") + (ev.label || "");
    const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
    edit.onclick = () => fillEventForm(ev, null);
    const del = document.createElement("button"); del.type = "button"; del.className = "del"; del.textContent = "Remove";
    del.onclick = async () => { await deleteEvent(ev); fillEventForm(null, day.y + "-" + day.m + "-" + day.d); };
    row.appendChild(what); row.appendChild(edit); row.appendChild(del);
    list.appendChild(row);
  });
}
async function openEventsDialog(){
  await renderCalBlock();
  const list = $("#eventsList");
  list.innerHTML = "";
  if (!EVENTS.length) {
    const p = document.createElement("p");
    p.className = "dlg-note";
    p.textContent = "No events yet. Click a day on the calendar to add one.";
    list.appendChild(p);
  }
  EVENTS.forEach(ev => {
    const row = document.createElement("div"); row.className = "evrow";
    const when = document.createElement("span"); when.className = "evwhen";
    when.textContent = ev.day + (ev.times ? " · " + ev.times : "");
    const what = document.createElement("span"); what.className = "evwhat";
    what.textContent = ev.label || "";
    const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
    edit.onclick = () => { $("#eventsDlg").hidden = true; fillEventForm(ev, null); };
    const del = document.createElement("button"); del.type = "button"; del.className = "del"; del.textContent = "Delete";
    del.onclick = async () => { await deleteEvent(ev); openEventsDialog(); };
    row.appendChild(when); row.appendChild(what); row.appendChild(edit); row.appendChild(del);
    list.appendChild(row);
  });
  $("#eventsDlg").hidden = false;
}
function applyCalMarker(){
  const sym = $("#calSymbol").value, col = $("#calColor").value;
  const size = parseInt($("#calSize").value, 10);
  CAL_CFG = {symbol: sym || CAL_CFG.symbol,
             color: col || CAL_CFG.color,
             size: (size >= 8 && size <= 48) ? size : (CAL_CFG.size || 17)};
  const pv = $("#calPreview");
  if (pv) { pv.textContent = CAL_CFG.symbol; pv.style.color = CAL_CFG.color; pv.style.fontSize = CAL_CFG.size + "px"; }
  renderCalBlock();
  markDirty();
}
function openCalDialog(){
  $("#calSymbol").value = CAL_CFG.symbol;
  $("#calColor").value = CAL_CFG.color;
  $("#calSize").value = CAL_CFG.size || 17;
  const pv = $("#calPreview");
  if (pv) { pv.textContent = CAL_CFG.symbol; pv.style.color = CAL_CFG.color; pv.style.fontSize = (CAL_CFG.size || 17) + "px"; }
  $("#calDlg").hidden = false;
}
$("#evMarkerBtn").onclick = () => { openCalDialog(); };
$("#calSmaller").onclick = () => { $("#calSize").value = Math.max(8, (parseInt($("#calSize").value, 10) || 17) - 2); applyCalMarker(); };
$("#calBigger").onclick = () => { $("#calSize").value = Math.min(48, (parseInt($("#calSize").value, 10) || 17) + 2); applyCalMarker(); };
$("#calSize").addEventListener("change", applyCalMarker);
$("#calApply").onclick = () => { $("#calDlg").hidden = true; };
$("#calCancel").onclick = () => { $("#calDlg").hidden = true; };
/* ---- the contact form's wording ---- */
function formToFields(cfg){
  FORM_FIELDS.forEach(([key, sel]) => { const el = $(sel); if (el) el.value = cfg[key] == null ? "" : cfg[key]; });
  const c = $("#formShowContact"), f = $("#formShowFiles");
  if (c) c.checked = cfg.show_contact !== false;
  if (f) f.checked = cfg.show_files !== false;
}
function fieldsToForm(cfg){
  const out = Object.assign({}, cfg);
  FORM_FIELDS.forEach(([key, sel]) => { const el = $(sel); if (el) out[key] = el.value; });
  const c = $("#formShowContact"), f = $("#formShowFiles");
  if (c) out.show_contact = c.checked;
  if (f) out.show_files = f.checked;
  return out;
}
function openFormDialog(){
  formToFields(FORM || {});
  $("#formDlg").hidden = false;
}
$("#formApply").onclick = async () => {
  FORM = fieldsToForm(FORM || {});
  $("#formDlg").hidden = true;
  await renderFormBlock();
  markDirty();
  status.textContent = "Form wording changed — press Save to stage it";
};
$("#formReset").onclick = () => {
  FORM = Object.assign({}, FORM_BASE);
  formToFields(FORM);
};
$("#formCancel").onclick = () => { $("#formDlg").hidden = true; };
["●","•","■","▲","▼","★","◆","✦","✚","♦"].forEach(sym => {
  const o = document.createElement("option");
  o.value = sym; o.textContent = sym;
  $("#calSymbol").appendChild(o);
});
$("#calSymbol").addEventListener("change", applyCalMarker);
$("#calColor").addEventListener("change", applyCalMarker);
$("#eventAdd").onclick = () => { fillEventForm(null, null); };
$("#eventsClose").onclick = () => { $("#eventsDlg").hidden = true; };
$("#evCancel").onclick = () => { $("#eventDlg").hidden = true; };
/* ---- dated news articles for the Latest page ---- */
function openNewsDialog(){
  const now = new Date();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  $("#newsHeadline").value = "";
  $("#newsDate").value = now.getFullYear() + "-" + mm + "-" + dd;
  $("#newsBody").value = "";
  $("#newsDlg").hidden = false;
  $("#newsHeadline").focus();
}
$("#newsAdd").onclick = () => {
  const headline = $("#newsHeadline").value.trim();
  const body = $("#newsBody").value.trim();
  if (!headline) { status.textContent = "Give the article a headline first"; $("#newsHeadline").focus(); return; }
  if (!body) { status.textContent = "Write the article first"; $("#newsBody").focus(); return; }
  const bits = String($("#newsDate").value || "").split("-").map(Number);
  let datestr = "";
  try {
    datestr = new Date(bits[0], bits[1] - 1, bits[2], 12).toLocaleDateString("en-US",
      {month: "long", day: "numeric", year: "numeric"});
  } catch(e) { datestr = ""; }
  if (!datestr || datestr === "Invalid Date") { status.textContent = "Pick a date for the article first"; return; }
  const card = document.createElement("div");
  card.className = "card";
  const h = document.createElement("h2");
  h.textContent = headline;
  card.appendChild(h);
  const dp = document.createElement("p");
  dp.className = "c-muted";
  dp.textContent = datestr;
  card.appendChild(dp);
  const chunks = body.split(/\\n\\s*\\n/);
  chunks.forEach(chunk => {
    const p = document.createElement("p");
    const lines = chunk.split("\\n");
    lines.forEach((ln, i) => {
      if (i) p.appendChild(document.createElement("br"));
      p.appendChild(document.createTextNode(ln));
    });
    card.appendChild(p);
  });
  let anchor = null;
  for (const el of canvas.children) { if (el.tagName === "H2") { anchor = el; break; } }
  if (anchor && anchor.nextSibling) canvas.insertBefore(card, anchor.nextSibling);
  else if (anchor) canvas.appendChild(card);
  else if (canvas.firstChild) canvas.insertBefore(card, canvas.firstChild);
  else canvas.appendChild(card);
  $("#newsDlg").hidden = true;
  card.scrollIntoView({block: "center"});
  pushHist(); markDirty();
  status.textContent = "News article added — Save to stage it";
};
$("#newsCancel").onclick = () => { $("#newsDlg").hidden = true; };
$("#evSave").onclick = async () => {
  const day = EVENT_DAY;
  if (!day) return;
  const y = parseInt($("#evYear").value, 10) || day.y;
  const title = $("#evTitle").value.trim();
  if (!title) { $("#evTitle").focus(); status.textContent = "Give the event a name"; return; }
  const ev = {month: day.m, day: day.d, year: y, time: $("#evTime").value.trim(),
              title: title};
  const editIdx = EVENT_EDIT ? EVENT_EDIT.idx : null;
  $("#eventDlg").hidden = true;
  writeEvent(ev, editIdx);
};
$("#evDelete").onclick = async () => {
  const ev = EVENT_EDIT;
  $("#eventDlg").hidden = true;
  if (ev) await deleteEvent(ev);
};
/* clicking a day in the builder's calendar adds an event on that day */
canvas.addEventListener("click", e => {
  const prev = e.target.closest("[data-cal-prev]");
  const next = e.target.closest("[data-cal-next]");
  if (prev || next) {
    e.preventDefault();
    if (!CAL_Y || !CAL_M) return;
    let y = CAL_Y, m = CAL_M + (next ? 1 : -1);
    while (m < 1) { m = 12; y--; }
    while (m > 12) { m = 1; y++; }
    CAL_Y = y; CAL_M = m;
    renderCalBlock();
    return;
  }
  const day = e.target.closest("[data-cal-day]");
  if (day) {
    e.preventDefault();
    fillEventForm(null, day.getAttribute("data-cal-day"));
  }
});
function decorateFormMarker(){
  const current = SITE.pages.find(p => p.slug === SLUG);
  if (!current || !current.form) return;
  renderFormBlock();
}
function syncFormMarker(){
  const current = SITE.pages.find(p => p.slug === SLUG);
  if (current && current.form) { decorateFormMarker(); return; }
  const existing = canvas.querySelector('[data-form-marker="contact"]');
  const marker = existing || Array.from(canvas.querySelectorAll("p")).find(el => el.textContent.trim() === "[contact form]");
  if (marker) {
    marker.dataset.formMarker = "contact"; marker.classList.add("form-placeholder");
    marker.contentEditable = "false"; marker.textContent = "Contact form is off — enable it in Page settings to publish it";
  }
}
function bodyForSave(){
  const copy = canvas.cloneNode(true);
  const page = SITE.pages.find(p => p.slug === SLUG);
  let kept = false;
  // the generated blocks go back to the marker paragraphs the site reads
  Array.from(copy.querySelectorAll("[data-cal-wrap]")).forEach(node => {
    node.replaceWith(markerPara("[event calendar]"));
  });
  Array.from(copy.querySelectorAll("[data-form-block]")).forEach(node => {
    if (page && page.form) node.replaceWith(markerPara("[contact form]"));
    else node.remove();
  });
  // any marker paragraph that is still lying around
  Array.from(copy.querySelectorAll("p")).forEach(node => {
    const isMarker = node.dataset.formMarker === "contact"
      || node.textContent.trim() === "[contact form]";
    if (!isMarker) return;
    if (page && page.form && !kept) {
      node.textContent = "[contact form]";
      node.removeAttribute("data-form-marker");
      node.removeAttribute("class");
      node.removeAttribute("contenteditable");
      kept = true;
    } else node.remove();
  });
  return copy.innerHTML;
}
async function openPage(slug){
  if (slug === SLUG) return;
  if (!(await confirmLeave())) return;
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  let j;
  try {
    j = await api("/api/page?slug=" + encodeURIComponent(slug));
  } catch (err) { status.textContent = "could not open page: " + err.message; return; }
  SLUG = slug; canvas.innerHTML = j.body; selImg = null; syncFormMarker();
  CAL_Y = 0; CAL_M = 0;
  renderCalBlock();
  try { localStorage.setItem(LAST_PAGE_KEY, SLUG); } catch (e) {}
  ensureExitBlock();
  histReset();
  dirty = false; renderAll(); updateSaveBtn(); placeCaretStart();
  const restored = await restoreDraft();
  if (!restored) status.textContent = editing ? "editing — nothing saved yet" : "viewing";
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
["in-title","in-tagline","in-description","in-footer","in-theme","in-css"].forEach(id => {
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
  SITE.description = $("#in-description").value;
  SITE.footer = $("#in-footer").value;
  SITE.theme = $("#in-theme").value;
  SITE.custom_css = $("#in-css").value;
  document.body.dataset.theme = SITE.theme;
  document.body.dataset.starAnimation = "comic";
  applyCustomCssPreview();
  $("#foot").textContent = SITE.footer;
  $("#foot").style.display = SITE.footer ? "" : "none";
}
function customCssAllowed(css){
  let plain = String(css || "").replace(/\\/\\*[\\s\\S]*?\\*\\//g, "");
  plain = plain.replace(/\\\\([0-9a-fA-F]{1,6})\\\\s?/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16)));
  plain = plain.replace(/\\\\(.)/g, "$1");
  if (/<\\/style|@\\s*import\\b|image-set\\s*\\(/i.test(plain)) return false;
  const re = /url\\(\\s*(?:(["])(.*?)\\1|([^)]*))\\s*\\)/gi;
  let match, count = 0;
  while ((match = re.exec(plain)) !== null) {
    count++;
    const target = (match[2] || match[3] || "").trim();
    if (!/^\\/?uploads\\/[A-Za-z0-9._/-]+$/i.test(target) || target.includes("..")) return false;
  }
  return count === (plain.match(/url\\s*\\(/gi) || []).length;
}
function applyCustomCssPreview(){
  const style = $("#site-custom-css");
  const warning = $("#customCssError");
  const disabled = $("#customCssDisabled");
  const allowed = customCssAllowed(SITE.custom_css || "");
  if (style) style.textContent = CUSTOM_CSS_PREVIEW_ENABLED && allowed ? (SITE.custom_css || "") : "";
  if (warning) warning.hidden = allowed || !CUSTOM_CSS_PREVIEW_ENABLED;
  if (disabled) disabled.hidden = CUSTOM_CSS_PREVIEW_ENABLED;
}
document.addEventListener("input", e => {
  const ed = e.target.closest && e.target.closest("#canvas,[data-ed]");
  if (!ed) return;
  if (ed.dataset && ed.dataset.ed && ED_TO_INPUT[ed.dataset.ed]) {
    $("#" + ED_TO_INPUT[ed.dataset.ed]).value = ed.innerText.trim();
    readInputsToSite();
  }
  // typing in the event list redraws the calendar
  if (e.target.closest && e.target.closest(".evsrc")) scheduleEventsRefresh();
  markDirty(); hint();
});
btnEdit.onclick = () => setEditing(!editing);

function openNewPageDialog(source=SLUG, mode="blank", name=""){
  newPageSource = source;
  $("#newPageName").value = name;
  $("#newPageStarter").value = mode;
  $("#addPageDlg").hidden = false;
  $("#newPageName").focus();
}
$("#btn-addpage").onclick = async () => {
  if (!(await confirmLeave())) return;
  openNewPageDialog(SLUG, "blank", "");
};
$("#newPageCancel").onclick = () => {
  $("#addPageDlg").hidden = true; newPageSource = null;
  if (pageSettingsSlug) $("#pageDlg").hidden = false;
};
$("#newPageCreate").onclick = async () => {
  const nav = $("#newPageName").value.trim();
  if (!nav) { $("#newPageName").focus(); status.textContent = "Enter a menu name for the page"; return; }
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  try {
    const copy = $("#newPageStarter").value === "copy";
    const j = copy ? await api("/api/pages/duplicate", {slug:newPageSource || SLUG, nav})
                   : await api("/api/pages/add", {nav});
    let body = j.body;
    if (body === undefined) {
      try { body = (await api("/api/page?slug=" + encodeURIComponent(j.slug))).body; }
      catch (err) { replaceSite(j.site); renderAll(); status.textContent = "Page created, but could not load it: " + err.message; return; }
    }
    replaceSite(j.site); SLUG = j.slug;
    try { localStorage.setItem(LAST_PAGE_KEY, SLUG); } catch (e) {}
    canvas.innerHTML = body || ""; dirty = false; selImg = null; replaceTarget = null; clearDraft(SLUG); syncFormMarker(); ensureExitBlock();
    histReset(); renderAll(); updateSaveBtn(); placeCaretStart();
    $("#addPageDlg").hidden = true; $("#pageDlg").hidden = true;
    newPageSource = null; pageSettingsSlug = null;
    status.textContent = (copy ? "Page copied" : "Blank page created") + " and staged — add your content, then Save";
  } catch (err) { status.textContent = "could not create page: " + err.message; }
};
async function deletePage(slug, nav){
  if (dirty && !(await confirmLeave())) return false;
  if (!confirm('Delete the page "' + nav + '"? This cannot be undone.')) return false;
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  try {
    replaceSite((await api("/api/pages/delete", {slug})).site);
  } catch (err) { status.textContent = "delete failed: " + err.message; return false; }
  if (SLUG === slug) {
    SLUG = "index";
    try { localStorage.setItem(LAST_PAGE_KEY, SLUG); } catch (e) {}
    try {
      canvas.innerHTML = (await api("/api/page?slug=index")).body; selImg = null; replaceTarget = null; syncFormMarker();
    } catch (err) { status.textContent = "page deleted but home could not load: " + err.message; dirty = false; renderAll(); updateSaveBtn(); return false; }
  histReset();
  }
  clearDraft(slug);
  dirty = false; renderAll(); updateSaveBtn(); status.textContent = "page deleted and staged";
  return true;
}
async function movePage(slug, dir){
  if (dirty && !(await confirmLeave())) return;
  try {
    replaceSite((await api("/api/pages/move", {slug, dir})).site);
  } catch (err) { status.textContent = "move failed: " + err.message; return; }
  dirty = false; renderAll(); updateSaveBtn();
}
async function openPageSettings(slug){
  if (dirty && !(await confirmLeave())) return;
  const p = SITE.pages.find(x => x.slug === slug);
  if (!p) return;
  pageSettingsSlug = slug;
  $("#pageSlug").textContent = slug + ".html";
  $("#pageNav").value = p.nav || "";
  $("#pageTitle").value = p.title || "";
  $("#pageDescription").value = p.description || "";
  $("#pageHidden").checked = !!p.hidden;
  $("#pageForm").checked = !!p.form;
  $("#pageBadge").value = p.badge || (p.hidden ? "none" : "");
  $("#pageHidden").disabled = slug === "index";
  $("#pageBadge").disabled = slug === "index";
  $("#pageDlgTitle").textContent = "Page settings — " + p.nav;
  $("#pageDelete").hidden = slug === "index";
  $("#pageDlg").hidden = false;
  $("#pageNav").focus();
}
$("#btn-page-settings").onclick = () => openPageSettings(SLUG);
$("#btn-calendar-top").onclick = () => openCalDialog();
$("#pageCancel").onclick = () => { $("#pageDlg").hidden = true; pageSettingsSlug = null; };
$("#pageBadge").addEventListener("change", () => {
  if ($("#pageBadge").value) $("#pageHidden").checked = true;
});
$("#pageHidden").addEventListener("change", () => {
  if (!$("#pageHidden").checked) $("#pageBadge").value = "";
});
$("#pageApply").onclick = async () => {
  if (!pageSettingsSlug) return;
  const payload = {
    slug: pageSettingsSlug,
    nav: $("#pageNav").value,
    title: $("#pageTitle").value,
    description: $("#pageDescription").value,
    hidden: $("#pageHidden").checked,
    form: $("#pageForm").checked,
    badge: $("#pageBadge").value
  };
  try {
    const j = await api("/api/pages/settings", payload);
    replaceSite(j.site); dirty = false; clearDraft(SLUG); renderAll(); updateSaveBtn();
    syncFormMarker();
    $("#pageDlg").hidden = true; pageSettingsSlug = null;
    status.textContent = "Page settings saved and staged — commit/push to publish";
  } catch (err) { status.textContent = "page settings failed: " + err.message; }
};
$("#pageDuplicate").onclick = async () => {
  if (!pageSettingsSlug) return;
  const source = pageSettingsSlug, name = $("#pageNav").value || "Page";
  try {
    const updated = await api("/api/pages/settings", {
      slug:source, nav:name, title:$("#pageTitle").value, description:$("#pageDescription").value,
      hidden:$("#pageHidden").checked, form:$("#pageForm").checked, badge:$("#pageBadge").value
    });
    replaceSite(updated.site);
  } catch (err) { status.textContent = "save page settings first: " + err.message; return; }
  $("#pageDlg").hidden = true;
  openNewPageDialog(source, "copy", name + " copy");
};
$("#pageDelete").onclick = async () => {
  if (!pageSettingsSlug) return;
  const p = SITE.pages.find(x => x.slug === pageSettingsSlug);
  if (p && await deletePage(p.slug, p.nav)) { $("#pageDlg").hidden = true; pageSettingsSlug = null; }
};
$("#btn-help").onclick = () => { $("#helpDlg").hidden = false; };
$("#builderreload").onclick = restartBuilderNow;
$("#helpClose").onclick = () => { $("#helpDlg").hidden = true; };
$("#btn-html-source").onclick = () => {
  if (!editing) { status.textContent = "Press Edit before opening page HTML"; return; }
  $("#sourceHtml").value = bodyForSave();
  $("#sourceHint").textContent = "Changes are cleaned and previewed here; they are not saved until you press Save.";
  $("#sourceDlg").hidden = false; $("#sourceHtml").focus();
};
$("#sourceCancel").onclick = () => { $("#sourceDlg").hidden = true; };
$("#sourceApply").onclick = async () => {
  try {
    const j = await api("/api/clean", {body:$("#sourceHtml").value});
    canvas.innerHTML = j.body; selImg = null; replaceTarget = null; syncFormMarker(); ensureExitBlock();
    $("#sourceDlg").hidden = true; pushHist(); markDirty(); hint(); imgButtons();
    status.textContent = "HTML cleaned and previewed — Save to stage";
  } catch (err) { $("#sourceHint").textContent = "Could not clean HTML: " + err.message; }
};
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("#helpDlg").hidden) $("#helpDlg").hidden = true;
  else if (!$("#mediaDlg").hidden) { $("#mediaDlg").hidden = true; mediaTarget = null; mediaRange = null; }
  else if (!$("#pageDlg").hidden) { $("#pageDlg").hidden = true; pageSettingsSlug = null; }
  else if (!$("#sourceDlg").hidden) $("#sourceDlg").hidden = true;
  else if (!$("#bigbtndlg").hidden) closeBigDialog();
  else if (!$("#draftDlg").hidden) settleDraft("later");
  else if (!$("#unsavedDlg").hidden) settleUnsaved("stay");
});
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
  const inspector = $("#imgInspector");
  if (inspector) inspector.classList.toggle("on", !!selImg && editing);
  const alt = $("#imgAlt");
  if (alt) { alt.disabled = !selImg || !editing; if (document.activeElement !== alt) alt.value = selImg ? (selImg.alt || "") : ""; }
}
canvas.addEventListener("click", e => {
  // in edit mode, clicking a link must not navigate away from the builder
  if (editing && e.target.closest("a")) e.preventDefault();
  $$("#canvas img").forEach(i => i.classList.remove("sel"));
  selImg = null;
  if (e.target.tagName === "IMG" && editing) { selImg = e.target; selImg.classList.add("sel"); }
  imgButtons();
});
$("#imgAlt").addEventListener("input", e => {
  if (!selImg || !editing) return;
  selImg.alt = e.target.value.slice(0, 200); markDirty();
});
$("#imgAlt").addEventListener("change", () => { if (selImg) pushHist(); });
$("#btn-replace-img").onclick = () => {
  if (!selImg || !editing) return;
  replaceTarget = selImg; galleryMode = false; filepick.multiple = false; filepick.click();
};
filepick.addEventListener("cancel", () => { replaceTarget = null; mediaTarget = null; mediaRange = null; });
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
/* ---------- text size + text color + leading + tracking + highlight ----------
   Sizes are NUMERIC pixels: the toolbar writes inline style="font-size:NNpx"
   (integer, 8-200) and never a t-* class. Colors are the c-* classes or an
   inline style="color:..."; highlights are always inline
   style="background-color:...". Leading is a unitless multiplier (1.0–3.0),
   tracking is px (-5..30). save() whitelists all six before sending and the
   server re-validates, so nothing hostile survives a save.
   The old t-* classes still render (the CSS keeps them) and a legacy element
   still reads back its true size through getComputedStyle, but the toolbar
   only ever writes numbers — and setting a size strips any legacy t-* class
   off the element it touches. */
const PX_SCALE = [8,9,10,11,12,14,16,18,20,24,28,32,36,48,64,72,96];
const SIZE_MIN = 8, SIZE_MAX = 200;    // the number box's range
const LEAD_MIN = 1.0, LEAD_MAX = 3.0;  // line-height multiplier range
const TRACK_MIN = -5, TRACK_MAX = 30;  // letter-spacing px range
// the ten stacks the Font dropdown offers — the only values save() keeps.
// Compared whitespace-insensitively: the DOM serializes font-family with
// spaces after commas ("Tahoma, Geneva, sans-serif") while the options
// carry the canonical spaceless form.
const FONT_CANON = {};
Array.from(document.querySelectorAll("#fontSel option")).forEach(o => {
  if (o.value) FONT_CANON[o.value.toLowerCase().replace(/\\s+/g, "")] = o.value;
});
const COLOR_CLASSES = ["c-muted", "c-red", "c-blue", "c-green", "c-gold"];
const BLOCK_SEL = "p,h2,h3,blockquote,li";
const SIZE_RE = /^t-(tiny|small|large|huge|giant)$/;
const COLOR_RE = /^c-/;
const WHEEL_RE = /^#[0-9a-f]{6}$/i;   // native picker always yields #rrggbb
// a span that carries any toolbar-kept inline style must survive unwrapping
function hasKeepStyle(el){
  return !!(el.style.color || el.style.backgroundColor || el.style.fontSize
    || el.style.fontFamily || el.style.lineHeight || el.style.letterSpacing);
}
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
  hi--; canvas.innerHTML = hist[hi]; selImg = null; imgButtons();
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
  hi++; canvas.innerHTML = hist[hi]; selImg = null; imgButtons();
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
  // on a button the wheel paints the box fill, with readable text after
  const host = r.startContainer.nodeType === 3
    ? r.startContainer.parentNode : r.startContainer;
  if (!host || !host.closest) return;
  const anc = host.closest("a.bigbtn, a.btn");
  if (anc && canvas.contains(anc)) {
    anc.style.color = autoFg(hex);
    anc.style.backgroundColor = hex;
    return;
  }
  if (r.collapsed) {                 // caret: the run of text it sits in
    const s = nearestSpan(r);
    if (s) { paintWheel(s, hex); return; }
    const b = nodeBlock(r.startContainer);
    if (b) paintWheel(b, hex);
    return;
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) { blocksIntersecting(r).forEach(b => paintWheel(b, hex)); return; }
  const s = nearestSpan(r);
  if (spanCovers(r, s)) { paintWheel(s, hex); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); }
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
  updateLeadNum(); updateTrackNum();   // keep the sibling readouts in lockstep
}
// ---------- line height + letter spacing + highlight ----------
// Same targeting as sizes (eachSizeTarget), same save/server validation.
// Leading is a unitless multiplier (1.0–3.0); tracking is px (-5..30);
// highlight is an inline background-color like the wheel color.
function setLeading(el, mult){
  el.style.lineHeight = String(Math.round(mult * 100) / 100);
}
function setTracking(el, px){
  el.style.letterSpacing = String(Math.round(px * 100) / 100) + "px";
}
// the effective leading ratio the caret is sitting in (computed px ÷ font px)
function leadAtCaret(){
  const r = activeRange(); if (!r) return null;
  let n = r.startContainer;
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && (n.tagName === "SPAN" || n.matches(BLOCK_SEL))) return effectiveLead(n);
    n = n.parentNode;
  }
  return effectiveLead(canvas);
}
function effectiveLead(el){
  if (!el) return null;
  const cs = getComputedStyle(el), fs = parseFloat(cs.fontSize);
  if (!isFinite(fs) || fs <= 0 || cs.lineHeight === "normal") return null;
  const px = parseFloat(cs.lineHeight);
  return (isFinite(px) && px > 0) ? Math.round(px / fs * 100) / 100 : null;
}
// the effective tracking px the caret is sitting in
function trackAtCaret(){
  const r = activeRange(); if (!r) return null;
  let n = r.startContainer;
  if (n.nodeType === 3) n = n.parentNode;
  while (n && n !== canvas) {
    if (n.nodeType === 1 && (n.tagName === "SPAN" || n.matches(BLOCK_SEL))) return effectiveTrack(n);
    n = n.parentNode;
  }
  return effectiveTrack(canvas);
}
function effectiveTrack(el){
  if (!el) return null;
  const v = getComputedStyle(el).letterSpacing;
  if (!v || v === "normal") return null;
  const px = parseFloat(v);
  return isFinite(px) ? Math.round(px * 100) / 100 : null;
}
// the number boxes double as readouts and inputs
function updateLeadNum(){
  const inp = $("#leadNum"); if (!inp) return;
  if (document.activeElement === inp) return;   // don't stomp what is being typed
  const m = leadAtCaret();
  inp.value = m === null ? "" : String(m);
}
function updateTrackNum(){
  const inp = $("#trackNum"); if (!inp) return;
  if (document.activeElement === inp) return;
  const px = trackAtCaret();
  inp.value = px === null ? "" : String(px);
}
function applyExactLeading(mult){
  eachSizeTarget(el => setLeading(el, mult));
}
function applyExactTracking(px){
  eachSizeTarget(el => setTracking(el, px));
}
// Enter (or the change event) in the boxes applies the value, same targeting as A-/A+.
function commitLeadNum(){
  const inp = $("#leadNum"); if (!inp) return;
  const raw = (inp.value || "").trim();
  const n = Number(raw);
  if (raw === "" || !isFinite(n)) { updateLeadNum(); return; }   // not a number: show the real value again
  applyExactLeading(Math.max(LEAD_MIN, Math.min(LEAD_MAX, n)));
  canvas.focus();
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
}
function commitTrackNum(){
  const inp = $("#trackNum"); if (!inp) return;
  const raw = (inp.value || "").trim();
  const n = Number(raw);
  if (raw === "" || !isFinite(n)) { updateTrackNum(); return; }
  applyExactTracking(Math.max(TRACK_MIN, Math.min(TRACK_MAX, n)));
  canvas.focus();
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
}
$("#leadNum").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); commitLeadNum(); }
});
$("#leadNum").addEventListener("change", commitLeadNum);
$("#trackNum").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); commitTrackNum(); }
});
$("#trackNum").addEventListener("change", commitTrackNum);
// paint a highlight (inline background-color the sanitizer keeps)
function paintHighlight(el, hex){
  el.style.backgroundColor = hex.toLowerCase();
}
function applyHlColor(hex){
  if (!WHEEL_RE.test(hex || "")) return;
  let r = getRange();
  if (!r) { r = activeRange(); if (r) restoreRange(r); }
  if (!r) return;
  hex = hex.toLowerCase();
  if (r.collapsed) {             // caret: the word it sits in — never the block
    const wq = wordRangeAt(r);
    if (!wq) {
      status.textContent = "Highlight paints a word or a text selection — it never colors a whole block";
      return;
    }
    r = wq; restoreRange(r);
  }
  const a = nodeBlock(r.startContainer), z = nodeBlock(r.endContainer);
  if (a !== z) { blocksIntersecting(r).forEach(b => paintHighlight(b, hex)); return; }
  const s = nearestSpan(r);
  if (spanCovers(r, s)) { paintHighlight(s, hex); }
  else { const sp = wrapSpan(r, ""); if (sp) paintHighlight(sp, hex); }
}
$("#hlwheel").addEventListener("input", e => {
  if (!editing) return;
  canvas.focus();
  applyHlColor(e.target.value);
  pushHist(); imgButtons(); markDirty(); hint(); updateSizeNum();
});
function unwrapSpan(el){
  const p = el.parentNode; if (!p) return;
  const r = document.createRange();
  r.setStartBefore(el);
  const last = el.lastChild;
  while (el.firstChild) p.insertBefore(el.firstChild, el);
  p.removeChild(el);
  if (last) { r.setEndAfter(last); restoreRange(r); }
}
// the text runs a range covers, in document order — [node, start, end)
function textRuns(r){
  let root = r.commonAncestorContainer;
  if (root.nodeType === 3) root = root.parentNode;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const runs = [];
  let n;
  while ((n = walker.nextNode())) {
    if (!r.intersectsNode(n)) continue;
    let a = 0, b = n.length;
    if (n === r.startContainer) a = r.startOffset;
    if (n === r.endContainer) b = r.endOffset;
    if (a >= b) continue;
    runs.push([n, a, b]);
  }
  return runs;
}
function wrapSpan(r, cls){
  const s = document.createElement("span");
  if (cls) s.className = cls;
  try { r.surroundContents(s); }
  catch (e) {
    // selection grazes an element boundary: split the covered text runs out
    // of their spans and gather them — extractContents would clone the
    // partially contained span (duplicated ids, strays of nested wrappers)
    const runs = textRuns(r);
    if (!runs.length) return null;
    const slices = runs.map(([node, a, b]) => {
      let t = node;
      if (b < t.length) t.splitText(b);
      if (a > 0) t = t.splitText(a);
      return t;
    });
    const anchor = slices[0].parentNode;
    anchor.insertBefore(s, slices[0]);
    for (let i = 0; i < slices.length; i++) s.appendChild(slices[i]);
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
    if (s) { fn(s); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); return; }
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
  if (s) { fn(s); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); }
  else { const sp = wrapSpan(r, ""); if (sp) fn(sp); }
}
/* ---------- fonts: same targeting as sizes, separate unwrap check ----------
   The toolbar writes an exact allowlisted stack (or removes the property);
   save() and the server re-validate, so only the ten stacks survive. */
function eachFontTarget(fn){
  let r = getRange();
  if (!r) {
    r = activeRange();
    if (r) restoreRange(r);
  }
  if (!r) return;
  if (r.collapsed) {
    const s = nearestSpan(r);
    if (s) { fn(s); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); return; }
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
  if (s) { fn(s); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); }
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
let blockTarget = null, blockHideTimer = null;
const blockbar = $("#blockbar");
function showBlockbar(el){
  if (!editing || !el || !canvas.contains(el)) return;
  blockTarget = el;
  blockbar.hidden = false;
  const r = el.getBoundingClientRect();
  blockbar.style.left = Math.max(8, Math.min(window.innerWidth - blockbar.offsetWidth - 8, r.left)) + "px";
  // never sit on top of the toolbar: the bar belongs to the block, and the
  // toolbar's own buttons must stay clickable
  const tb = $("#toolbar");
  const floor = (tb ? tb.getBoundingClientRect().bottom : 44) + 8;
  let top = r.top - blockbar.offsetHeight - 5;
  if (top < floor) top = r.top + 5;
  blockbar.style.top = Math.max(floor, top) + "px";
}
function hideBlockbar(){ clearTimeout(blockHideTimer); blockbar.hidden = true; blockTarget = null; }
canvas.addEventListener("mousemove", e => {
  if (!editing || blockbar.contains(e.target)) return;
  const b = topBlock(e.target);
  if (b) showBlockbar(b);
});
canvas.addEventListener("mouseleave", () => {
  blockHideTimer = setTimeout(() => { if (!blockbar.matches(":hover")) hideBlockbar(); }, 180);
});
blockbar.addEventListener("mouseenter", () => clearTimeout(blockHideTimer));
blockbar.addEventListener("mouseleave", hideBlockbar);
blockbar.addEventListener("click", e => {
  const action = e.target.closest("[data-block-action]")?.dataset.blockAction;
  const el = blockTarget;
  if (!action || !el || !canvas.contains(el)) return;
  if (action === "up" || action === "down") {
    const sibling = action === "up" ? el.previousElementSibling : el.nextElementSibling;
    if (sibling) {
      if (action === "up") el.parentNode.insertBefore(el, sibling);
      else el.parentNode.insertBefore(sibling, el);
      pushHist(); markDirty();
    }
  } else if (action === "duplicate") {
    const copy = el.cloneNode(true); el.parentNode.insertBefore(copy, el.nextSibling);
    pushHist(); markDirty();
  } else if (action === "delete") {
    el.remove(); selImg = null; ensureExitBlock(); pushHist(); markDirty(); hint(); imgButtons();
  }
  hideBlockbar();
});
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
// the word the caret sits in, or null on whitespace / between elements
function wordRangeAt(r){
  const n = r.startContainer;
  if (n.nodeType !== 3) return null;
  const txt = n.data, off = r.startOffset;
  const w = c => /[A-Za-z0-9&_'’-]/.test(c);
  let a = off, z = off;
  while (a > 0 && w(txt[a-1])) a--;
  while (z < txt.length && w(txt[z])) z++;
  if (a === z) return null;
  const q = document.createRange();
  q.setStart(n, a); q.setEnd(n, z);
  return q;
}
// true when el's exact contents equal range r (styling el then touches
// nothing beyond the selection); a null element covers nothing
function spanCovers(r, el){
  if (!el) return false;
  const probe = document.createRange();
  probe.selectNodeContents(el);
  return probe.compareBoundaryPoints(Range.START_TO_START, r) === 0 &&
         probe.compareBoundaryPoints(Range.END_TO_END, r) === 0;
}
function applyColor(cls){
  if (cls && COLOR_CLASSES.indexOf(cls) < 0) return;
  let r = getRange();
  if (!r) { r = activeRange(); if (r) restoreRange(r); }
  if (!r) return;
  if (r.collapsed) {                       // caret: the run of text it sits in
    const s = nearestSpan(r);
    if (s) { setColor(s, cls); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); return; }
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
  if (spanCovers(r, s)) { setColor(s, cls); if (!s.className && !hasKeepStyle(s)) unwrapSpan(s); }
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
    el.style.removeProperty("background-color");   // highlight swatch/wheel
    el.style.removeProperty("font-size");      // the numeric size the toolbar writes
    el.style.removeProperty("font-family");    // the font dropdown's stack
    el.style.removeProperty("line-height");    // the line-height number box
    el.style.removeProperty("letter-spacing"); // the tracking number box
    if (el.tagName === "SPAN" && !el.className && !hasKeepStyle(el)) unwrapSpan(el);   // no class left: unwrap
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
      linkFill = $("#linkFill"), linkFg = $("#linkFg"),
      linkFillLabel = $("#linkFillLabel"), linkFgLabel = $("#linkFgLabel"),
      linkApply = $("#linkApply"), linkRemove = $("#linkRemove"),
      linkCancel = $("#linkCancel"), linkStat = $("#linkStat");
let linkEditTarget = null;
let linkRange0 = null;      // range captured when the link dialog opens
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
/* the box/text pickers only matter when the small-button look is on.
   forceFg is true only while the box colour is being changed by hand —
   opening a saved button must keep its own text colour untouched. */
function syncLinkFill(forceFg){
  const on = linkBtnBox.checked;
  linkFillLabel.hidden = !on;
  linkFgLabel.hidden = !on;
  if (on && forceFg && linkFg.value) { linkFg.value = autoFg(linkFill.value); }
}
function autoFg(bg){
  try {
    const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec((bg || "").trim());
    if (!m) return "#ffffff";
    const lin = c => {
      const v = parseInt(c, 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    const lum = 0.2126 * lin(m[1]) + 0.7152 * lin(m[2]) + 0.0722 * lin(m[3]);
    return lum > 0.179 ? "#141414" : "#ffffff";
  } catch (_) { return "#ffffff"; }
}
function openLinkDialog(mode){
  if (!editing) return;
  const r = activeRange();
  if (!r) { status.textContent = "click into the page text first, then Link"; return; }
  linkRange0 = r.cloneRange();
  linkEditTarget = anchorForRange(r);
  if (linkEditTarget && linkEditTarget.classList.contains("bigbtn")) {
    openBigbtnDialog(linkEditTarget);
    return;   // a big box belongs to its own dialog
  }
  const btnMode = mode === "btn" || (linkEditTarget && linkEditTarget.classList.contains("btn"));
  linkTitle.textContent = linkEditTarget ? (btnMode ? "Edit button" : "Edit link")
                                         : (btnMode ? "Insert button" : "Insert link");
  if (linkEditTarget) {
    linkText.value = (linkEditTarget.textContent || "").trim().slice(0, 200);
    linkUrl.value = linkEditTarget.getAttribute("href") || "";
    linkBlank.checked = linkEditTarget.getAttribute("target") === "_blank";
    linkBtnBox.checked = linkEditTarget.classList.contains("btn");
    const st = linkEditTarget.style;
    linkFill.value = cssColorToHex(st.backgroundColor) || "#1c4fd8";
    linkFg.value = cssColorToHex(st.color) || autoFg(linkFill.value);
  } else {
    linkText.value = r.collapsed ? "" : r.toString().slice(0, 200);
    linkUrl.value = btnMode ? "https://" : "";
    linkBlank.checked = btnMode;
    linkBtnBox.checked = btnMode;
    linkFill.value = "#1c4fd8"; linkFg.value = "#ffffff";
  }
  syncLinkFill(false);
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
  const bg = /^#[0-9a-f]{6}$/i.test(linkFill.value) ? linkFill.value.toLowerCase() : "#1c4fd8";
  const fg = /^#[0-9a-f]{6}$/i.test(linkFg.value) ? linkFg.value.toLowerCase() : "#ffffff";
  // the selection often moves into the dialog fields by the time Apply is
  // pressed; the range captured at dialog-open is the intended anchor
  const live = activeRange();
  const fallback = linkEditTarget ? null : (linkRange0 || null);
  const r = live || fallback;
  if (!r && !linkEditTarget) {
    linkHint.textContent = "Lost the insertion point — click where it should go, then try again.";
    linkHint.style.color = "#b3261e";
    return;   // leave the dialog open so the message is actually read
  }
  if (r) { restoreRange(r); }
  canvas.focus();
  if (linkEditTarget && canvas.contains(linkEditTarget)) {
    if (label) linkEditTarget.textContent = label;
    linkEditTarget.setAttribute("href", href);
    if (blank) { linkEditTarget.setAttribute("target", "_blank"); linkEditTarget.setAttribute("rel", "noopener"); }
    else { linkEditTarget.removeAttribute("target"); linkEditTarget.removeAttribute("rel"); }
    linkEditTarget.classList.toggle("btn", btn);
    if (btn) {
      // colors go straight onto the box; dragged geometry already present
      // on the style object is preserved untouched
      linkEditTarget.style.color = fg;
      linkEditTarget.style.backgroundColor = bg;
    } else {
      linkEditTarget.style.removeProperty("background-color");
    }
    const cur = getRange();
    if (cur) { cur.selectNodeContents(linkEditTarget); cur.collapse(false); restoreRange(cur); }
  } else if (r.collapsed) {
    if (btn) {
      // a button is its own box after the caret's block, like the big button
      if (!label) { linkHint.textContent = "Give the button some words first."; linkHint.style.color = "#b3261e"; linkText.focus(); return; }
      const a = document.createElement("a");
      a.className = "btn"; a.href = href;
      a.setAttribute("style", "color:" + fg + ";background-color:" + bg);
      a.textContent = label;
      if (blank) { a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener"); }
      const block = nodeBlock(r.startContainer);
      const top = block ? topBlock(block) : null;
      if (top && top.parentNode === canvas) top.parentNode.insertBefore(a, top.nextSibling);
      else canvas.appendChild(a);
    } else {
      if (!label) { linkHint.textContent = "Give the link some words first."; linkHint.style.color = "#b3261e"; linkText.focus(); return; }
      let tag = '<a href="' + escapeHtml(href) + '"';
      if (blank) tag += ' target="_blank" rel="noopener"';
      document.execCommand("insertHTML", false, tag + ">" + escapeHtml(label) + "</a>");
    }
  } else {
    document.execCommand("createLink", false, "#fc-newlink");
    const made = Array.from(canvas.querySelectorAll('a[href="#fc-newlink"]'));
    if (!made.length) { linkHint.textContent = "Could not link that selection — try selecting plain text."; linkHint.style.color = "#b3261e"; return; }
    made.forEach(a => {
      a.setAttribute("href", href);
      if (blank) { a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener"); }
      if (btn) {
        a.classList.add("btn");
        a.style.color = fg;
        a.style.backgroundColor = bg;
      }
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
linkBtnBox.onchange = () => syncLinkFill(true);
linkFill.oninput = () => syncLinkFill(true);
linkDlg.addEventListener("mousedown", e => { if (e.target === linkDlg) closeLinkDialog(); });
linkDlg.addEventListener("keydown", e => {
  if (e.key === "Enter" && !linkApply.disabled) { e.preventDefault(); applyLink(); }
  else if (e.key === "Escape") { e.preventDefault(); closeLinkDialog(); }
});
/* ---------- big button box: huge clickable block, dragged in edit mode ----------
   One element: <a class="bigbtn" href="…" style="…">WORDS</a>. In edit mode
   the whole box drags (mousedown+move) and a plain click opens its dialog;
   in view mode it is an ordinary link the visitor can click anywhere on. */
const bigDlg = $("#bigbtndlg"), bigTitle = $("#bigbtnTitle"),
      bigText = $("#bigbtnText"), bigUrl = $("#bigbtnUrl"),
      bigHint = $("#bigbtnHint"), bigPages = $("#bigbtnPages"),
      bigBg = $("#bigbtnBg"), bigFg = $("#bigbtnFg"), bigBlank = $("#bigbtnBlank"),
      bigApply = $("#bigbtnApply"), bigRemove = $("#bigbtnRemove"),
      bigCancel = $("#bigbtnCancel");
let bigTarget = null;   // the box being edited, or null for a fresh insert
let bigRange0 = null;   // range captured when the big dialog opens
function bigbtnValidate(h){ const v = linkValidate(h); return v; }
function refreshBigDialog(){
  const v = bigbtnValidate(bigUrl.value);
  bigApply.disabled = !v.ok;
  bigHint.textContent = v.warn || v.msg || "";
  bigHint.style.color = v.ok ? "#555" : "#b3261e";
  const cur = bigUrl.value.trim();
  bigPages.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.href === cur));
}
function cssColorToHex(v){
  const m = /^rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)$/.exec((v || "").trim());
  if (!m) return /^#[0-9a-f]{6}$/i.test((v || "").trim()) ? v.trim().toLowerCase() : null;
  return "#" + [m[1], m[2], m[3]].map(n => (+n).toString(16).padStart(2, "0")).join("");
}
function openBigbtnDialog(target){
  if (!editing) { status.textContent = "Press Edit before working with button boxes"; return; }
  bigTarget = target || null;
  bigTitle.textContent = bigTarget ? "Edit button box" : "Insert button box";
  if (bigTarget) {
    bigText.value = (bigTarget.textContent || "").trim().slice(0, 120);
    bigUrl.value = bigTarget.getAttribute("href") || "";
    const st = bigTarget.style;
    bigBg.value = cssColorToHex(st.backgroundColor) || "#1c4fd8";
    bigFg.value = cssColorToHex(st.color) || "#ffffff";
    bigBlank.checked = bigTarget.getAttribute("target") === "_blank";
  } else {
    bigText.value = "";
    bigUrl.value = "";
    bigBg.value = "#1c4fd8"; bigFg.value = "#ffffff";
    bigBlank.checked = false;
  }
  bigRemove.style.display = bigTarget ? "" : "none";
  bigRange0 = activeRange() ? activeRange().cloneRange() : null;
  bigPages.innerHTML = "";
  SITE.pages.forEach(p => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = (p.hidden ? "☆ " : "") + p.nav;
    b.dataset.href = p.slug + ".html";
    b.onclick = () => { bigUrl.value = b.dataset.href; bigUrl.focus(); refreshBigDialog(); };
    bigPages.appendChild(b);
  });
  refreshBigDialog();
  bigDlg.hidden = false;
  bigText.focus();
}
function closeBigDialog(){
  bigDlg.hidden = true; bigTarget = null;
  if (editing) { canvas.focus(); const r = activeRange(); if (r) restoreRange(r); }
}
function applyBig(){
  const v = bigbtnValidate(bigUrl.value);
  if (!v.ok) { refreshBigDialog(); return; }
  const href = bigUrl.value.trim();
  const words = bigText.value.trim() || "Button";
  const bg = /^#[0-9a-f]{6}$/i.test(bigBg.value) ? bigBg.value.toLowerCase() : "#1c4fd8";
  const fg = /^#[0-9a-f]{6}$/i.test(bigFg.value) ? bigFg.value.toLowerCase() : "#ffffff";
  const blank = bigBlank.checked;
  if (bigTarget && canvas.contains(bigTarget)) {
    bigTarget.textContent = words;
    bigTarget.setAttribute("href", href);
    // rebuild the style but PRESERVE the dragged geometry
    const st = bigTarget.style;
    const geo = [];
    if (st.position === "absolute") geo.push("position:absolute");
    const mg = (st.margin || "").replace(/\\s+/g, "");
    if (st.position === "absolute" && (mg === "0px" || mg === "0")) geo.push("margin:0px");
    ["left", "top", "width", "height"].forEach(prop => {
      const m = /^(-?\\d+(?:\\.\\d{1,2})?)px$/.exec((st[prop] || "").replace(/\\s+/g, ""));
      if (m) geo.push(prop + ":" + String(Math.round(+m[1] * 100) / 100) + "px");
    });
    bigTarget.setAttribute("style", geo.join(";")
      + (geo.length ? ";" : "") + "background-color:" + bg + ";color:" + fg);
    if (blank) { bigTarget.setAttribute("target", "_blank"); bigTarget.setAttribute("rel", "noopener"); }
    else { bigTarget.removeAttribute("target"); bigTarget.removeAttribute("rel"); }
  } else {
    // fresh insert: a block-level box after the caret's block (or at the end)
    const a = document.createElement("a");
    a.className = "bigbtn"; a.href = href;
    a.setAttribute("style", "background-color:" + bg + ";color:" + fg);
    a.textContent = words;
    if (blank) { a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener"); }
    const rr = activeRange();
    const r = (rr && canvas.contains(rr.commonAncestorContainer)) ? rr
      : ((bigRange0 && canvas.contains(bigRange0.commonAncestorContainer)) ? bigRange0 : null);
    const block = r ? nodeBlock(r.startContainer) : null;
    const top = block ? topBlock(block) : null;
    if (top && top.parentNode === canvas) top.parentNode.insertBefore(a, top.nextSibling);
    else canvas.appendChild(a);
  }
  closeBigDialog();
  pushHist(); markDirty(); hint(); imgButtons();
}
function removeBig(){
  if (bigTarget && canvas.contains(bigTarget)) { bigTarget.remove(); pushHist(); markDirty(); hint(); }
  closeBigDialog();
}
bigApply.onclick = applyBig;
bigRemove.onclick = removeBig;
bigCancel.onclick = closeBigDialog;
bigText.oninput = refreshBigDialog;
bigUrl.oninput = refreshBigDialog;
bigBg.oninput = refreshBigDialog;   // colors apply on Apply, dialog just validates
bigDlg.addEventListener("mousedown", e => { if (e.target === bigDlg) closeBigDialog(); });
bigDlg.addEventListener("keydown", e => {
  if (e.key === "Enter" && !bigApply.disabled) { e.preventDefault(); applyBig(); }
  else if (e.key === "Escape") { e.preventDefault(); closeBigDialog(); }
});
/* dragging a button box (big or small) */
let bigDrag = null;
canvas.addEventListener("mousedown", e => {
  if (!editing) return;
  const el = e.target.closest && e.target.closest("a.bigbtn, a.btn");
  if (!el) return;
  e.preventDefault();   // no text selection, no native link drag
  const cr = canvas.getBoundingClientRect(), er = el.getBoundingClientRect();
  bigDrag = {el, isBig: el.classList.contains("bigbtn"), moved:false, x0:e.clientX, y0:e.clientY,
             offX:e.clientX - er.left, offY:e.clientY - er.top,
             cw:cr.width, ch:cr.height, cl:cr.left, ct:cr.top,
             w:er.width, h:er.height};
});
document.addEventListener("mousemove", e => {
  if (!bigDrag) return;
  const dx = e.clientX - bigDrag.x0, dy = e.clientY - bigDrag.y0;
  if (!bigDrag.moved && Math.abs(dx) + Math.abs(dy) > 4) {
    bigDrag.moved = true;
    bigDrag.el.classList.add("dragging");
    bigDrag.el.style.position = "absolute";
    bigDrag.el.style.margin = "0px";
  }
  if (!bigDrag.moved) return;
  const x = Math.max(0, Math.min(bigDrag.cw - bigDrag.w, e.clientX - bigDrag.cl - bigDrag.offX));
  const y = Math.max(0, Math.min(bigDrag.ch - bigDrag.h, e.clientY - bigDrag.ct - bigDrag.offY));
  bigDrag.el.style.left = Math.round(x) + "px";
  bigDrag.el.style.top = Math.round(y) + "px";
});
document.addEventListener("mouseup", e => {
  if (!bigDrag) return;
  const el = bigDrag.el, moved = bigDrag.moved, isBig = bigDrag.isBig;
  el.classList.remove("dragging");
  bigDrag = null;
  if (moved) { pushHist(); markDirty(); }
  else if (isBig) { openBigbtnDialog(el); }   // a plain click edits the big box
  else {
    // a plain click on a small button opens the link dialog on it
    const r = document.createRange();
    r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    openLinkDialog("btn");
  }
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
  else if (c === "bigbtn") { openBigbtnDialog(null); return; }
  else if (c === "link") openLinkDialog("link");
  else if (c === "button") openLinkDialog("btn");
  else if (c === "media") { openMediaLibrary(); return; }
  else if (c === "divider") document.execCommand("insertHTML", false, "<hr><p><br></p>");
  else if (c === "section") document.execCommand("insertHTML", false,
    '<div class="card"><h2>Section title</h2><p>Write something great here.</p></div><p><br></p>');
  else if (c === "table") document.execCommand("insertHTML", false,
    '<table><tbody><tr><th>Header</th><th>Header</th></tr><tr><td><br></td><td><br></td></tr></tbody></table><p><br></p>');
  else if (c === "form") {
    const existing = formBlockEl();
    if (existing) {
      existing.scrollIntoView({block: "center"});
      status.textContent = "The contact form is already on this page";
      return;
    }
    const marker = document.createElement("p"); marker.dataset.formMarker = "contact";
    marker.className = "form-placeholder"; marker.contentEditable = "false";
    marker.textContent = "[contact form]"; appendNode(marker);
    renderFormBlock();
  }
  else if (c === "formedit") { openFormDialog(); return; }
  else if (c === "events") { openEventsDialog(); return; }
  else if (c === "calendar") { openCalDialog(); return; }
  else if (c === "news") { openNewsDialog(); return; }
  else if (c === "image") { galleryMode = false; replaceTarget = null; filepick.multiple = false; filepick.click(); }
  else if (c === "gallery") { replaceTarget = null; galleryMode = true; filepick.multiple = true; filepick.click(); }
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
  if (!files.length) { replaceTarget = null; mediaTarget = null; mediaRange = null; return; }
  const uploads = [];
  for (const f of files) {
    const p = await uploadImage(f);
    if (p) uploads.push({path:p, name:f.name});
  }
  if (!uploads.length) { replaceTarget = null; mediaTarget = null; mediaRange = null; return; }
  if (replaceTarget && canvas.contains(replaceTarget)) {
    replaceTarget.src = uploads[0].path;
    selImg = replaceTarget; replaceTarget.classList.add("sel"); replaceTarget = null;
    mediaRange = null; mediaTarget = null;
    pushHist(); imgButtons(); markDirty(); hint(); status.textContent = "picture replaced — Save to stage";
    return;
  }
  canvas.focus();
  if (mediaRange && canvas.contains(mediaRange.commonAncestorContainer)) restoreRange(mediaRange);
  mediaRange = null;
  if (galleryMode && uploads.length > 1) {
    const g = document.createElement("div"); g.className = "gallery";
    uploads.forEach(item => {
    const img = document.createElement("img"); img.src = item.path; img.alt = item.name.replace(/\\.[^.]+$/, "");
      g.appendChild(img);
    });
    appendNode(g);
  } else {
    uploads.forEach(item => {
      const img = document.createElement("img");
      img.src = item.path; img.className = "secimg medium"; img.alt = item.name.replace(/\\.[^.]+$/, "");
      appendNode(img);
    });
  }
  mediaRange = null; mediaTarget = null;
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
async function openMediaLibrary(){
  if (!editing) return;
  mediaTarget = selImg && canvas.contains(selImg) ? selImg : null;
  const r = activeRange(); mediaRange = r ? r.cloneRange() : null;
  $("#mediaGrid").innerHTML = ""; $("#mediaEmpty").hidden = true;
  $("#mediaDlg").hidden = false;
  try {
    const j = await api("/api/media");
    const images = j.images || [];
    $("#mediaEmpty").hidden = images.length !== 0;
    images.forEach(item => {
      const b = document.createElement("button"); b.type = "button"; b.className = "media-item";
      const img = document.createElement("img"); img.src = "/" + item.path; img.alt = "";
      const name = document.createElement("span"); name.textContent = item.name;
      b.append(img, name);
      b.onclick = () => chooseMedia(item.path, item.name);
      $("#mediaGrid").appendChild(b);
    });
  } catch (err) { $("#mediaEmpty").hidden = false; $("#mediaEmpty").textContent = "Could not load images: " + err.message; }
}
function chooseMedia(path, name){
  $("#mediaDlg").hidden = true;
  canvas.focus();
  if (mediaTarget && canvas.contains(mediaTarget)) {
    mediaTarget.src = path; selImg = mediaTarget; selImg.classList.add("sel");
  } else {
    if (mediaRange && canvas.contains(mediaRange.commonAncestorContainer)) restoreRange(mediaRange);
    const img = document.createElement("img"); img.src = path;
    img.className = "secimg medium"; img.alt = name.replace(/\\.[^.]+$/, "");
    appendNode(img); selImg = img; img.classList.add("sel");
  }
  mediaTarget = null; mediaRange = null;
  pushHist(); imgButtons(); markDirty(); hint(); status.textContent = "picture added — Save to stage";
}
$("#mediaClose").onclick = () => { $("#mediaDlg").hidden = true; mediaTarget = null; mediaRange = null; };
$("#mediaUpload").onclick = () => { $("#mediaDlg").hidden = true; replaceTarget = mediaTarget; mediaTarget = null; galleryMode = false; filepick.multiple = false; filepick.click(); };
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
  if (saving) return false;
  readInputsToSite();
  // The Center/Left/Right buttons write text-align; fold alignment into the
  // "ac"/"ar" classes, and keep a validated wheel color and highlight, a
  // validated font size, one of the ten toolbar fonts, and validated
  // line-height and letter-spacing, before saving so all six survive the
  // page cleaner. The canonical form is
  // style="color:X;background-color:Y;font-size:Zpx;font-family:W;line-height:L;letter-spacing:T" — anything else is dropped.
  // The server re-validates every property, so nothing hostile survives there.
  canvas.querySelectorAll("a.bigbtn[style], a.btn[style]").forEach(el => {
    // the button box: geometry + colors in the server's canonical order
    const st = el.style;
    const keep = [];
    if (st.position === "absolute") keep.push("position:absolute");
    const mg = (st.margin || "").replace(/\\s+/g, "");
    if (st.position === "absolute" && (mg === "0px" || mg === "0")) keep.push("margin:0px");
    [["left",-100,5000],["top",-100,5000],["width",40,2000],["height",40,2000]].forEach(([prop,lo,hi]) => {
      const raw = (st[prop] || "").trim();
      const m = /^(-?\\d+(?:\\.\\d{1,2})?)px$/.exec(raw.replace(/\\s+/g, ""));
      if (m && +m[1] >= lo && +m[1] <= hi) {
        keep.push(prop + ":" + String(Math.round(+m[1] * 100) / 100) + "px");
      }
    });
    const col = (st.color || "").trim().replace(/\\s+/g, "");
    if (col && !/url|expression|javascript/i.test(col)) keep.push("color:" + col);
    const bgc = (st.backgroundColor || "").trim().replace(/\\s+/g, "");
    if (bgc && !/url|expression|javascript/i.test(bgc)) keep.push("background-color:" + bgc);
    const fsz = (st.fontSize || "").trim();
    const f = /^(\\d{1,3})px$/.exec(fsz.replace(/\\s+/g, ""));
    if (f && +f[1] >= SIZE_MIN && +f[1] <= SIZE_MAX) keep.push("font-size:" + (+f[1]) + "px");
    const ffm = (st.fontFamily || "").trim();
    const ffk = ffm.toLowerCase().replace(/["'\\s]+/g, "");
    if (FONT_CANON[ffk]) keep.push("font-family:" + FONT_CANON[ffk]);
    const lht = (st.lineHeight || "").trim();
    const lm = /^(\\d+(?:\\.\\d{1,2})?)$/.exec(lht.replace(/\\s+/g, ""));
    if (lm && +lm[1] >= LEAD_MIN && +lm[1] <= LEAD_MAX) keep.push("line-height:" + String(Math.round(+lm[1] * 100) / 100));
    const lsp = (st.letterSpacing || "").trim();
    const tm = /^(-?\\d+(?:\\.\\d{1,2})?)px$/i.exec(lsp.replace(/\\s+/g, ""));
    if (tm && +tm[1] >= TRACK_MIN && +tm[1] <= TRACK_MAX) keep.push("letter-spacing:" + String(Math.round(+tm[1] * 100) / 100) + "px");
    el.setAttribute("style", keep.join(";"));
  });
  canvas.querySelectorAll("[style]").forEach(el => {
    if (el.classList.contains("bigbtn") || el.classList.contains("btn")) return;   // handled above
    const wasCentered = el.classList.contains("ac");
    const wasRight = el.classList.contains("ar");
    const t = (el.style.textAlign || "").toLowerCase();
    const col = (el.style.color || "").trim();
    const fsz = (el.style.fontSize || "").trim();
    const ffm = (el.style.fontFamily || "").trim();
    const bgc = (el.style.backgroundColor || "").trim();
    const lht = (el.style.lineHeight || "").trim();
    const lsp = (el.style.letterSpacing || "").trim();
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
    const bv = bgc.replace(/\\s+/g, "");
    if (/^(#[0-9a-f]{3,8}|[a-z]{3,30}|rgba?\\([0-9,.%/]+\\)|hsla?\\([0-9,.%/]+(?:deg|turn)?\\))$/i.test(bv)
        && !/url|expression|javascript/i.test(bv)) {
      keep.push("background-color:" + bv);
    }
    const f = /^(\\d{1,3})px$/.exec(fsz.replace(/\\s+/g, ""));
    if (f && +f[1] >= SIZE_MIN && +f[1] <= SIZE_MAX) {
      keep.push("font-size:" + (+f[1]) + "px");
    }
    const ffk = ffm.toLowerCase().replace(/["'\\s]+/g, "");
    if (FONT_CANON[ffk]) keep.push("font-family:" + FONT_CANON[ffk]);
    const lm = /^(\\d+(?:\\.\\d{1,2})?)$/.exec(lht.replace(/\\s+/g, ""));
    if (lm && +lm[1] >= LEAD_MIN && +lm[1] <= LEAD_MAX) {
      keep.push("line-height:" + String(Math.round(+lm[1] * 100) / 100));
    }
    const tm = /^(-?\\d+(?:\\.\\d{1,2})?)px$/i.exec(lsp.replace(/\\s+/g, ""));
    if (tm && +tm[1] >= TRACK_MIN && +tm[1] <= TRACK_MAX) {
      keep.push("letter-spacing:" + String(Math.round(+tm[1] * 100) / 100) + "px");
    }
    if (keep.length) el.setAttribute("style", keep.join(";"));
  });
  ensureExitBlock();
  pushHist();
  const saveSlug = SLUG, saveRevision = editRevision;
  const sentBody = bodyForSave();
  const settings = {
    title: SITE.title.slice(0, 300) || "Flamingo and Cactus",
    tagline: SITE.tagline.slice(0, 300),
    description: (SITE.description || "").slice(0, 300),
    custom_css: (SITE.custom_css || "").slice(0, 20000),
    footer: SITE.footer.slice(0, 500),
    theme: SITE.theme
  };
  status.textContent = "saving…";
  saving = true; updateSaveBtn();
  try {
    const result = await api("/api/save", Object.assign({}, settings, {slug:saveSlug, body:sentBody, siteBase:serverSiteSnapshot, form:FORM, calendar:CAL_CFG}));
    serverSiteSnapshot = JSON.stringify(globalSettings(result.site));
    if (SLUG !== saveSlug) return false;
    if (editRevision !== saveRevision) {
      dirty = true; writeDraft(); updateSaveBtn();
      status.textContent = "Saved this version; newer edits are still unsaved";
      return false;
    }
    replaceSite(result.site);
    const cleanChanged = result.body !== sentBody;
    if (cleanChanged) {
      canvas.innerHTML = result.body; syncFormMarker(); ensureExitBlock(); histReset(); selImg = null; replaceTarget = null;
      renderCalBlock();
    }
    dirty = false; clearDraft(SLUG);
    renderAll(); updateSaveBtn();
    const updateBar = document.getElementById("builderupdated");
    if (updateBar) updateBar.hidden = true;
    status.textContent = cleanChanged
      ? "Saved and staged; unsupported markup was cleaned. Preview before publishing."
      : "Saved and staged — commit/push in GitKraken to publish";
    return true;
  } catch (err) {
    status.textContent = "Save failed — your draft is kept: " + err.message;
    return false;
  } finally { saving = false; updateSaveBtn(); }
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
  if (!(await confirmLeave())) return;
  await fetch("/api/shutdown", {method: "POST"});
  status.textContent = "builder stopped — close this tab";
};
renderAll();
setEditing(false);
ensureExitBlock();
histReset();
renderCalBlock();
renderFormBlock();
async function initEditor(){
  let last = null;
  try { last = localStorage.getItem(LAST_PAGE_KEY); } catch (e) {}
  if (last && last !== SLUG && SITE.pages.some(p => p.slug === last)) {
    try {
      const j = await api("/api/page?slug=" + encodeURIComponent(last));
      SLUG = last; canvas.innerHTML = j.body; syncFormMarker(); ensureExitBlock();
      histReset(); renderAll(); updateSaveBtn();
    } catch (err) { status.textContent = "could not reopen last page: " + err.message; }
  }
  try { localStorage.setItem(LAST_PAGE_KEY, SLUG); } catch (e) {}
  await restoreDraft();
}
initEditor();
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
    page = page.replace("__STAR_ANIMATION__", site_build.STAR_ANIMATION_DEFAULT)
    page = page.replace("__BODY__", canvas_body)
    page = page.replace("__FORMPREVIEW__", "")
    page = page.replace("__FORM_JSON__", json.dumps(form_cfg, ensure_ascii=False).replace("<", "\\u003c"))
    page = page.replace("__FORM_DEFAULTS__", json.dumps(site_build.FORM_DEFAULTS, ensure_ascii=False).replace("<", "\\u003c"))
    page = page.replace("__SITE_JSON__", json.dumps(site, ensure_ascii=False).replace("<", "\\u003c"))
    page = page.replace("__SLUG__", slug)
    st = Path(__file__).stat()
    stamp = f"{st.st_mtime_ns}-{st.st_size}"
    bping = json.dumps({"pid": os.getpid(), "stamp": stamp})
    page = page.replace('"__BPING__"', bping)
    page = page.replace("__CUSTOM_CSS__", site_build.safe_custom_css(site.get("custom_css", "")) if apply_custom_css else "")
    page = page.replace("__CUSTOM_CSS_ENABLED__", "true" if apply_custom_css else "false")
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "FlamingoSiteBuilder/3.0"

    def log_message(self, *a):
        pass

    def _loopback_ok(self) -> bool:
        """Refuse requests that did not arrive via this machine's loopback.

        The builder binds 127.0.0.1 only, but a hostile page open in the same
        browser could otherwise POST to it (plain POSTs need no preflight),
        and DNS rebinding could make a foreign Host header resolve here."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            return False
        return True

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if not self._loopback_ok():
            return self._json(403, {"error": "forbidden"})
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                site = read_site()
                slug = "index"
                apply_custom_css = qs.get("no-css", ["0"])[0] != "1"
                self._send(200, editor_page(site, slug, read_body(slug), live_base(), apply_custom_css))
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
            elif url.path == "/api/media":
                media = []
                if UPLOADS.is_dir():
                    for p in sorted(UPLOADS.iterdir(), key=lambda x: x.name.lower()):
                        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif"):
                            media.append({"name": p.name, "path": "uploads/" + p.name,
                                          "bytes": p.stat().st_size})
                self._json(200, {"images": media})
            elif url.path == "/api/page":
                slug = qs.get("slug", [""])[0]
                if not site_build.SLUG_RE.match(slug):
                    return self._json(400, {"error": "bad slug"})
                self._json(200, {"slug": slug, "body": read_body(slug)})
            elif url.path == "/api/ping":
                # lets the open builder page notice new builder code on disk
                # (edit.py itself), or a restarted server (fresh code live)
                st = Path(__file__).stat()
                stamp = f"{st.st_mtime_ns}-{st.st_size}"
                self._json(200, {"pid": os.getpid(), "stamp": stamp})
                return
            elif url.path == "/api/restart":
                # swap this process for a fresh one running the on-disk
                # edit.py — the page polls the pid and reloads on change.
                # POST-only (state changing); GET here is refused.
                self._json(405, {"error": "use POST"})
            elif url.path.endswith(".html"):
                # Serve a built page directly, e.g. /financials.html or the
                # unlisted /flank/index.html ledger, so the local preview
                # matches the live URL structure.
                site = read_site()
                known = {p["slug"] for p in site["pages"]} | {"404"}
                rel = url.path.lstrip("/")
                name = rel.rsplit(".", 1)[0]
                is_known_page = "/" not in name and site_build.SLUG_RE.match(name) and name in known
                is_flank = rel in ("flank/index.html", "flank/about.html")
                if is_known_page or is_flank:
                    try:
                        rebuild()
                    except Exception as e:
                        return self._json(500, {"error": str(e)[:300]})
                    p = SITE / rel
                    if p.is_file():
                        return self._send(200, p.read_bytes())
                    if is_known_page:
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
        with _MUTEX:
            return self._save_site_locked(site)

    def _save_site_locked(self, site: dict) -> str:
        PAGES.mkdir(parents=True, exist_ok=True)
        old_data = DATA.read_bytes() if DATA.is_file() else None
        write_text_atomic(DATA, json.dumps(site, ensure_ascii=False, indent=2) + "\n")
        try:
            rebuild()
        except Exception:
            if old_data is None:
                DATA.unlink(missing_ok=True)
            else:
                write_text_atomic(DATA, old_data.decode("utf-8"))
            raise
        paths = [DATA] + [PAGES / f"{p['slug']}.html" for p in site["pages"]] \
            + [SITE, LOGO, UPLOADS]
        return git_add([p for p in paths if p.exists()])

    def _handle_save(self, doc) -> None:
        slug = str(doc.get("slug", ""))
        if not site_build.SLUG_RE.match(slug):
            return self._json(400, {"error": "bad slug"})
        site = read_site()
        if slug not in [p["slug"] for p in site["pages"]]:
            return self._json(400, {"error": "unknown page"})
        site["title"] = str(doc.get("title", ""))[:300] or "Flamingo and Cactus"
        site["tagline"] = str(doc.get("tagline", ""))[:300]
        site["description"] = str(doc.get("description", ""))[:300]
        custom_css = str(doc.get("custom_css", ""))
        css_error = site_build.custom_css_error(custom_css)
        if css_error:
            return self._json(400, {"error": css_error})
        site["custom_css"] = site_build.safe_custom_css(custom_css)
        # The star burst is locked to the comic style - there is no user
        # toggle by design. Force the key on every save so any stale tab
        # or external editor cannot drift it back to portal.
        site["star_animation"] = site_build.STAR_ANIMATION_DEFAULT
        site["footer"] = str(doc.get("footer", ""))[:500]
        if str(doc.get("theme", "")) in site_build.THEMES:
            site["theme"] = str(doc.get("theme"))
        clean = site_build.sanitize(str(doc.get("body", ""))[:200000])
        page_path = PAGES / f"{slug}.html"
        old_data = DATA.read_bytes() if DATA.is_file() else None
        old_page = page_path.read_bytes() if page_path.is_file() else None
        # The contact form's wording travels with the page save.
        form_cfg = None
        if isinstance(doc.get("form"), dict):
            form_cfg = site_build.sanitize_form_config(
                doc.get("form"), site_build.load_form_config(FORM_PATH.parent))
        old_form = FORM_PATH.read_bytes() if FORM_PATH.is_file() else None
        # The calendar's marker (symbol + colour) travels with it too.
        cal_cfg = None
        if isinstance(doc.get("calendar"), dict):
            cal_cfg = site_build.sanitize_calendar_config(doc.get("calendar"))
        old_cal = CAL_PATH.read_bytes() if CAL_PATH.is_file() else None
        PAGES.mkdir(parents=True, exist_ok=True)
        try:
            write_text_atomic(DATA, json.dumps(site, ensure_ascii=False, indent=2) + "\n")
            write_text_atomic(page_path, clean)
            if form_cfg is not None:
                write_text_atomic(FORM_PATH,
                                  json.dumps(form_cfg, ensure_ascii=False, indent=2) + "\n")
            if cal_cfg is not None:
                write_text_atomic(CAL_PATH,
                                  json.dumps(cal_cfg, ensure_ascii=False, indent=2) + "\n")
            rebuild()
        except Exception:
            if old_data is None:
                DATA.unlink(missing_ok=True)
            else:
                write_text_atomic(DATA, old_data.decode("utf-8"))
            if old_page is None:
                page_path.unlink(missing_ok=True)
            else:
                write_text_atomic(page_path, old_page.decode("utf-8"))
            if form_cfg is not None:
                if old_form is None:
                    FORM_PATH.unlink(missing_ok=True)
                else:
                    write_text_atomic(FORM_PATH, old_form.decode("utf-8"))
            if cal_cfg is not None:
                if old_cal is None:
                    CAL_PATH.unlink(missing_ok=True)
                else:
                    write_text_atomic(CAL_PATH, old_cal.decode("utf-8"))
            raise
        saved_site = read_site()
        paths = [DATA] + [PAGES / f"{p['slug']}.html" for p in saved_site["pages"]] \
            + [SITE, LOGO, UPLOADS, FORM_PATH, CAL_PATH]
        note = git_add([p for p in paths if p.exists()])
        return self._json(200, {"ok": True, "site": saved_site, "body": clean, "note": note})

    def do_POST(self):
        if not self._loopback_ok():
            return self._json(403, {"error": "forbidden"})
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/restart":
                # swap this process for a fresh one running the on-disk
                # edit.py — the page polls /api/ping and reloads on change
                self._json(200, {"ok": True})
                def _relaunch():
                    os.execv(sys.executable, [sys.executable, "-u",
                              str(Path(__file__).resolve()),
                              "--port", str(BUILDER_PORT), "--no-open"])
                threading.Timer(0.4, _relaunch).start()
                return
            if url.path == "/api/save":
                doc = self._read_json()
                with _MUTEX:
                    return self._handle_save(doc)
            if url.path == "/api/form":
                # The contact form as the editor shows it, with the owner's
                # wording applied. Nothing here can send anything.
                doc = self._read_json()
                cfg = site_build.sanitize_form_config(doc.get("form") or {})
                return self._json(200, {"form": cfg, "html": inert_form_html(cfg)})
            if url.path == "/api/events":
                # The event list -> the events it holds and the month grid
                # drawn from them. The same parser the site build uses.
                doc = self._read_json()
                block = str(doc.get("block", ""))[:40000]
                try:
                    year = int(doc.get("y") or 0)
                    month = int(doc.get("m") or 0)
                except (TypeError, ValueError):
                    year = month = 0
                events = site_build.page_events(block)
                if not year or not month:
                    today = datetime.date.today()
                    year, month = today.year, today.month
                    if events:
                        near = min(events, key=lambda e: (e["y"], e["m"], e["d"]))
                        year, month = near["y"], near["m"]
                cfg = site_build.sanitize_calendar_config(
                    {"symbol": doc.get("symbol"), "color": doc.get("color"),
                     "size": doc.get("size")})
                grid = site_build.calendar_static_html(events, year, month, nav=True, cfg=cfg)
                return self._json(200, {"events": events, "html": grid,
                                        "y": year, "m": month, "cfg": cfg})
            if url.path == "/api/settings":
                doc = self._read_json()
                site = read_site()
                site["title"] = str(doc.get("title", ""))[:300] or "Flamingo and Cactus"
                site["tagline"] = str(doc.get("tagline", ""))[:300]
                site["description"] = str(doc.get("description", ""))[:300]
                custom_css = str(doc.get("custom_css", ""))
                css_error = site_build.custom_css_error(custom_css)
                if css_error:
                    return self._json(400, {"error": css_error})
                site["custom_css"] = site_build.safe_custom_css(custom_css)
                site["star_animation"] = site_build.STAR_ANIMATION_DEFAULT
                site["footer"] = str(doc.get("footer", ""))[:500]
                if str(doc.get("theme", "")) in site_build.THEMES:
                    site["theme"] = str(doc.get("theme"))
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/clean":
                doc = self._read_json()
                clean = site_build.sanitize(str(doc.get("body", ""))[:200000])
                return self._json(200, {"ok": True, "body": clean})
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
                return self._json(200, {"ok": True, "note": note, "body": clean})
            if url.path == "/api/pages/settings":
                doc = self._read_json()
                slug = str(doc.get("slug", ""))
                nav = str(doc.get("nav", ""))[:60].strip()
                badge = str(doc.get("badge", ""))
                if not site_build.SLUG_RE.match(slug):
                    return self._json(400, {"error": "bad slug"})
                if not nav:
                    return self._json(400, {"error": "menu name cannot be empty"})
                if badge not in ("", "none", "star", "gem"):
                    return self._json(400, {"error": "bad badge"})
                if slug == "index" and (bool(doc.get("hidden")) or badge):
                    return self._json(400, {"error": "the home page must stay visible and cannot have a badge"})
                site = read_site()
                target = next((p for p in site["pages"] if p["slug"] == slug), None)
                if target is None:
                    return self._json(400, {"error": "unknown page"})
                target["nav"] = nav
                page_title = str(doc.get("title", ""))[:300].strip()
                page_description = str(doc.get("description", ""))[:300].strip()
                if page_title:
                    target["title"] = page_title
                else:
                    target.pop("title", None)
                if page_description:
                    target["description"] = page_description
                else:
                    target.pop("description", None)
                target["form"] = bool(doc.get("form"))
                if badge:
                    for p in site["pages"]:
                        if p is not target and p.get("hidden") and p.get("badge", "star") == badge:
                            p["badge"] = "none"
                    target["hidden"] = True
                    target["badge"] = badge
                elif bool(doc.get("hidden")):
                    target["hidden"] = True
                    target["badge"] = "none"
                else:
                    target.pop("hidden", None)
                    target.pop("badge", None)
                note = self._save_site(site)
                return self._json(200, {"ok": True, "site": site, "note": note})
            if url.path == "/api/pages/duplicate":
                doc = self._read_json()
                source_slug = str(doc.get("slug", ""))
                nav = str(doc.get("nav", ""))[:60].strip() or "New page"
                if not site_build.SLUG_RE.match(source_slug):
                    return self._json(400, {"error": "bad slug"})
                site = read_site()
                source_index = next((i for i,p in enumerate(site["pages"]) if p["slug"] == source_slug), None)
                if source_index is None:
                    return self._json(400, {"error": "unknown page"})
                taken = {p["slug"] for p in site["pages"]}
                slug = unique_slug(nav, taken)
                source = site["pages"][source_index]
                clone = dict(source)
                clone["slug"], clone["nav"] = slug, nav
                clone.pop("hidden", None)
                clone.pop("badge", None)
                site["pages"].insert(source_index + 1, clone)
                body = read_body(source_slug)
                write_text_atomic(PAGES / f"{slug}.html", body)
                note = self._save_site(site)
                return self._json(200, {"ok": True, "slug": slug, "site": site,
                                        "body": body, "note": note})
            if url.path == "/api/pages/add":
                doc = self._read_json()
                nav = str(doc.get("nav", ""))[:60].strip() or "New page"
                site = read_site()
                taken = {p["slug"] for p in site["pages"]}
                slug = unique_slug(nav, taken)
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
    global BUILDER_PORT
    BUILDER_PORT = args.port
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

#!/usr/bin/env python3
"""
Build the Flamingo & Cactus site from local data.

    python3 scripts/build.py            # data/ -> site/
    python3 scripts/build.py --check    # build to temp dir and verify

Stdlib only. Idempotent: same inputs produce byte-identical output.

Layout:
    data/site.json      settings (title, tagline, footer, theme) + page list
    data/pages/<slug>.html   one sanitized HTML fragment per page
    assets/logo.png     masthead logo (embedded as a data URI, like Flank)
    assets/uploads/     pictures dropped into pages (copied to site/uploads/)
    scripts/build.py    this script (also owns the HTML sanitizer)
    site/               output: one .html per page, 404.html, favicon.png
"""

import argparse
import base64
import html
import json
import re
import shutil
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "site.json"
DEFAULT_PAGES = ROOT / "data" / "pages"
DEFAULT_LOGO = ROOT / "assets" / "logo.png"
DEFAULT_UPLOADS = ROOT / "assets" / "uploads"
DEFAULT_OUT = ROOT / "site"

THEMES = ("classic", "parchment", "ink")
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def logo_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def load_site(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    pages = doc.get("pages", [])
    if not pages:
        pages = [{"slug": "index", "nav": "Home"}]
    clean_pages = []
    seen = set()
    for p in pages:
        slug = str(p.get("slug", ""))[:40]
        if not SLUG_RE.match(slug) or slug in seen:
            continue
        seen.add(slug)
        clean_pages.append({"slug": slug, "nav": str(p.get("nav", slug))[:60] or slug})
    if "index" not in seen:
        clean_pages.insert(0, {"slug": "index", "nav": "Home"})
    theme = str(doc.get("theme", "classic"))
    return {
        "title": str(doc.get("title", "Flamingo and Cactus"))[:300],
        "tagline": str(doc.get("tagline", ""))[:300],
        "footer": str(doc.get("footer", ""))[:500],
        "theme": theme if theme in THEMES else "classic",
        "pages": clean_pages,
    }


# ---------------------------------------------------------------------------
# Sanitizer: page bodies are freeform editor HTML, so only an allowlist of
# plain tags survives. editor and builder share this.
ALLOWED_TAGS = {"p", "h2", "h3", "br", "strong", "b", "em", "i", "u", "s",
                "ul", "ol", "li", "a", "img", "blockquote", "hr", "div"}
DROP_WITH_CONTENT = {"script", "style", "form", "iframe", "object", "embed"}
IMG_CLASSES = {"secimg", "small", "medium", "full"}


def _clean_href(href: str) -> str | None:
    h = (href or "").strip()
    if h.startswith(("http://", "https://", "mailto:", "#", "/")):
        return h
    if re.fullmatch(r"[A-Za-z0-9_.-]+\.html", h):
        return h
    return None


def _clean_src(src: str) -> str | None:
    s = (src or "").strip()
    if s.startswith("uploads/") and ".." not in s and '"' not in s:
        return s
    return None


class _Sanitize(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth:
            return
        ad = dict(attrs)
        if tag == "div":
            # only galleries survive as divs; everything else is a paragraph
            if "gallery" not in ad.get("class", "").split():
                tag = "p"
        if tag not in ALLOWED_TAGS:
            return
        if tag in ("br", "hr"):
            self.out.append(f"<{tag}>")
            return
        if tag == "a":
            href = _clean_href(ad.get("href", ""))
            if not href:
                return
            cls = " ".join(c for c in ad.get("class", "").split() if c == "btn")
            cls_attr = f' class="{cls}"' if cls else ""
            # new tabs: only a literal _blank target and noopener survive
            tgt_attr = ' target="_blank"' if ad.get("target", "") == "_blank" else ""
            rel_attr = ""
            if tgt_attr and ad.get("rel", "") == "noopener":
                rel_attr = ' rel="noopener"'
            self.out.append(f'<a href="{html.escape(href, quote=True)}"{cls_attr}{tgt_attr}{rel_attr}>')
        elif tag == "img":
            src = _clean_src(ad.get("src", ""))
            if not src:
                return
            cls = " ".join(c for c in ad.get("class", "").split()
                           if c in IMG_CLASSES) or "secimg medium"
            alt = html.escape(ad.get("alt", ""), quote=True)[:200]
            self.out.append(
                f'<img class="{cls}" src="{html.escape(src, quote=True)}" alt="{alt}">')
        elif tag == "div":
            self.out.append('<div class="gallery">')
        else:
            extra = ""
            if tag in ("p", "h2", "h3", "blockquote"):
                # keep text centering: from an inline style (toolbar Center
                # button) or from an "ac" class already on the tag, so a
                # rebuild's second sanitize pass preserves it. Every other
                # style and class is dropped.
                keep = []
                if "ac" in ad.get("class", "").split():
                    keep.append("ac")
                if re.search(r"text-align\s*:\s*center", ad.get("style", ""), re.I):
                    if "ac" not in keep:
                        keep.append("ac")
                if keep:
                    extra = f' class="{" ".join(keep)}"'
            self.out.append(f"<{tag}{extra}>")
        self.open.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth:
            return
        if tag == "div":
            tag = "div"  # matches the gallery div kept above
        if tag in ALLOWED_TAGS and tag not in ("br", "hr", "img") and tag in self.open:
            self.open.remove(tag)
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.drop_depth:
            return
        if data.strip():
            self.out.append(html.escape(data))

    def result(self) -> str:
        return "".join(self.out)


def sanitize(fragment: str) -> str:
    p = _Sanitize()
    p.feed(fragment or "")
    p.close()
    return p.result()


# ---------------------------------------------------------------------------

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
  }
  body[data-theme="parchment"]{
    --paper:#f7f1e1;
    --paper2:#f3ecda;
    --ink:#2b2417;
    --ink2:#4a4130;
    --muted:#7a6f57;
    --faint:#9a8f74;
    --rule:#b8ab88;
    --rule2:#d6cab0;
    --link:#1a3a7c;
  }
  body[data-theme="ink"]{
    --paper:#161513;
    --paper2:#1e1c19;
    --ink:#f0e9da;
    --ink2:#d6cdbb;
    --muted:#a89d85;
    --faint:#7d7461;
    --rule:#4a453a;
    --rule2:#35312a;
    --link:#9db8ff;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:var(--paper);
    color:var(--ink);
    font:16px/1.6 Georgia,"Times New Roman",Times,serif;
    min-width:1120px;
    -webkit-font-smoothing:antialiased;
  }
  .page{max-width:860px;margin:0 auto;padding:26px 28px 64px}
  .masthead{text-align:center;padding:6px 0 4px}
  .masthead img{display:block;margin:0 auto;width:96px;height:96px;object-fit:contain}
  h1{
    font-size:34px;font-weight:400;letter-spacing:2.5px;text-transform:uppercase;
    margin:14px 0 2px;
  }
  .tagline{color:var(--muted);font-size:14px;font-style:italic;margin:0 0 14px}
  nav.sitenav{
    text-align:center;margin:0 0 18px;
    font:12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;
  }
  nav.sitenav a{color:var(--muted);text-decoration:none;margin:0 12px}
  nav.sitenav a:hover{color:var(--ink);text-decoration:underline}
  nav.sitenav .cur{color:var(--ink);margin:0 12px;border-bottom:2px solid var(--ink);padding-bottom:2px}
  .hr{height:2px;background:var(--ink);margin:0}
  .hr.thin{height:1px;background:var(--rule)}
  .canvas{color:var(--ink2);font-size:15.5px;line-height:1.65;padding:10px 0 0}
  .canvas:empty{padding:0}
  .canvas p{margin:0 0 12px}
  .canvas .ac{text-align:center}
  .canvas h2{
    font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.6px;
    text-transform:uppercase;color:var(--ink2);margin:26px 0 8px;
  }
  .canvas h3{font-size:16px;color:var(--ink2);margin:20px 0 6px}
  .canvas ul,.canvas ol{margin:0 0 12px;padding-left:26px}
  .canvas li{margin:0 0 4px}
  .canvas blockquote{
    margin:0 0 12px;padding-left:12px;color:var(--muted);
    border-left:2px solid var(--rule);
  }
  .canvas a{color:var(--link)}
  .canvas a.btn{
    display:inline-block;margin:4px 0;padding:8px 18px;
    border:1px solid var(--ink);border-radius:2px;
    color:var(--ink);text-decoration:none;
    font:13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;text-transform:uppercase;
  }
  .canvas img.secimg{display:block;height:auto;margin:12px 0;border:1px solid var(--rule2)}
  .canvas img.small{width:220px}
  .canvas img.medium{width:420px}
  .canvas img.full{width:100%}
  .canvas img{max-width:100%}
  .canvas .gallery{display:flex;gap:10px;margin:0 0 12px}
  .canvas .gallery img{flex:1;min-width:0;object-fit:cover;margin:0;border:1px solid var(--rule2)}
  footer{
    margin-top:34px;padding-top:14px;border-top:1px solid var(--rule);
    font:12.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint);text-align:center;
  }
'''


def nav_html(pages, current: str) -> str:
    if len(pages) <= 1:
        return ""
    parts = []
    for p in pages:
        if p["slug"] == current:
            parts.append(f'<span class="cur">{html.escape(p["nav"])}</span>')
        else:
            parts.append(
                f'<a href="{html.escape(p["slug"])}.html">{html.escape(p["nav"])}</a>')
    return '  <nav class="sitenav">\n    ' + "\n    ".join(parts) + "\n  </nav>\n"


def render_page(title: str, tagline: str, footer: str, theme: str,
                pages, slug: str, body_html: str, logo_uri: str) -> str:
    canvas = f'  <div class="canvas">\n{body_html}\n  </div>\n' if body_html else ""
    footer_block = f'  <footer>\n{html.escape(footer)}\n  </footer>\n' if footer.strip() else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)} &mdash; Flamingo &amp; Cactus</title>\n"
        "<style>\n" + CSS + "\n</style>\n"
        "</head>\n"
        f'<body data-theme="{theme}">\n'
        '<div class="page">\n'
        "\n"
        '  <div class="masthead">\n'
        f'    <img src="{logo_uri}" alt="Flamingo and Cactus">\n'
        f"    <h1>{html.escape(title)}</h1>\n"
        f"    <p class=\"tagline\">{html.escape(tagline)}</p>\n"
        "</div>\n"
        f"{nav_html(pages, slug)}"
        '  <div class="hr"></div>\n'
        "\n"
        f"{canvas}"
        f"{footer_block}"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


NOTFOUND = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Page not found &mdash; Flamingo &amp; Cactus</title>
<style>
__CSS__
</style>
</head>
<body data-theme="classic">
<div class="page">
  <div class="masthead">
    <img src="__LOGO__" alt="Flamingo and Cactus">
    <h1>Flamingo &amp; Cactus</h1>
    <p class="tagline">Flamingo &amp; Cactus Group</p>
  </div>
  <div class="hr"></div>
</div>
</body>
</html>
"""


def read_body(pages_dir: Path, slug: str) -> str:
    p = pages_dir / f"{slug}.html"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def build(data_path: Path, pages_dir: Path, logo_path: Path,
          uploads_dir: Path, out_dir: Path):
    site = load_site(data_path)
    logo_uri = logo_data_uri(logo_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    for p in site["pages"]:
        raw = read_body(pages_dir, p["slug"])
        clean = sanitize(raw)
        if clean != raw:
            (pages_dir / f"{p['slug']}.html").write_text(clean, encoding="utf-8")
        (out_dir / f"{p['slug']}.html").write_text(
            render_page(site["title"], site["tagline"], site["footer"],
                        site["theme"], site["pages"], p["slug"], clean, logo_uri),
            encoding="utf-8")
    nf = NOTFOUND.replace("__CSS__", CSS).replace("__LOGO__", logo_uri)
    (out_dir / "404.html").write_text(nf, encoding="utf-8")
    shutil.copyfile(logo_path, out_dir / "favicon.png")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    site_uploads = out_dir / "uploads"
    if site_uploads.exists():
        shutil.rmtree(site_uploads)
    if uploads_dir.is_dir():
        files = [f for f in sorted(uploads_dir.iterdir()) if f.is_file()]
        if files:
            site_uploads.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copyfile(f, site_uploads / f.name)

    print(f"wrote {len(site['pages'])} page(s) to {out_dir}")
    return site


def check(data_path: Path, pages_dir: Path, logo_path: Path, uploads_dir: Path):
    tmp = Path(tempfile.mkdtemp(prefix="flamingo-site-check-"))
    try:
        site = build(data_path, pages_dir, logo_path, uploads_dir, tmp)
        ok = True

        def expect(cond, msg):
            nonlocal ok
            print(("PASS  " if cond else "FAIL  ") + msg)
            ok = ok and cond

        idx = (tmp / "index.html").read_text(encoding="utf-8")
        expect(len(idx) > 0, "index.html non-empty")
        expect(html.escape(site["title"]) in idx, "title present")
        expect("min-width:1120px" in idx, "desktop-only floor kept")
        expect("<script" not in idx.lower(), "no script in built page")
        expect("onclick" not in idx.lower(), "no event-handler attributes in built page")
        expect('body[data-theme="parchment"]' in idx, "parchment theme CSS present")
        expect('body[data-theme="ink"]' in idx, "ink theme CSS present")
        if len(site["pages"]) > 1:
            for p in site["pages"]:
                expect((tmp / f"{p['slug']}.html").is_file(), f"{p['slug']}.html built")
                if p["slug"] != "index":
                    expect(f'href="{p["slug"]}.html"' in idx, f"nav links to {p['slug']}")
        nasty = ('<script>alert(1)</script><p onclick="x()">hi</p>'
                 '<img src="http://evil/x.png"><a href="javascript:1">z</a>'
                 '<div class="gallery"><img src="uploads/a.png"></div>')
        clean = sanitize(nasty)
        expect("<script" not in clean and "onclick" not in clean, "sanitizer drops scripts/handlers")
        expect("http://evil" not in clean and "javascript:" not in clean,
               "sanitizer drops off-site images and js links")
        expect("hi" in clean, "sanitizer keeps the text")
        expect('<div class="gallery">' in clean, "sanitizer keeps galleries")
        expect(clean.count("<a") == clean.count("</a>"), "no stray closing tags")
        centered = sanitize('<p style="text-align: center">x</p>')
        expect('class="ac"' in centered and "style=" not in centered,
               "sanitizer keeps centering as a class, drops the style")
        expect('class="ac"' in sanitize(centered),
               "centering survives a rebuild (second sanitize pass)")
        newtab = sanitize('<p><a class="btn" href="https://example.com/x" '
                          'target="_blank" rel="noopener">Hi</a></p>')
        expect('target="_blank"' in newtab and 'rel="noopener"' in newtab,
               "sanitizer keeps new-tab buttons")
        notab = sanitize('<a href="https://example.com/x" target="_self">Hi</a>')
        expect("target=" not in notab, "sanitizer drops non-blank targets")
        if not ok:
            raise SystemExit("check FAILED")
        print(f"check passed in {tmp}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Flamingo & Cactus site (stdlib only).")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--pages", type=Path, default=DEFAULT_PAGES)
    ap.add_argument("--logo", type=Path, default=DEFAULT_LOGO)
    ap.add_argument("--uploads", type=Path, default=DEFAULT_UPLOADS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        check(args.data.expanduser().resolve(), args.pages.expanduser().resolve(),
              args.logo.expanduser().resolve(), args.uploads.expanduser().resolve())
    else:
        build(args.data.expanduser().resolve(), args.pages.expanduser().resolve(),
              args.logo.expanduser().resolve(), args.uploads.expanduser().resolve(),
              args.out.expanduser().resolve())


if __name__ == "__main__":
    main()

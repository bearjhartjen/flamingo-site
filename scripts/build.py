#!/usr/bin/env python3
"""
Build the Flamingo & Cactus site from local data.

    python3 scripts/build.py            # data/ -> site/
    python3 scripts/build.py --check    # build to temp dir and verify

Stdlib only. Idempotent: same inputs produce byte-identical output.

Layout:
    data/site.json      settings (title, tagline, footer, theme) + page list
    data/pages/<slug>.html   one sanitized HTML fragment per page
    data/The Flank.md   the ledger, synced from ~/Desktop/The Flank.md
    assets/logo.png     masthead logo (embedded as a data URI, like Flank)
    assets/uploads/     pictures dropped into pages (copied to site/uploads/)
    scripts/build.py    this script (also owns the HTML sanitizer)
    scripts/build_flank.py  the hidden ledger generator (site/flank/)
    site/               output: one .html per page, 404.html, favicon.png,
                        CNAME, plus the unlisted flank/ directory
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "site.json"
DEFAULT_PAGES = ROOT / "data" / "pages"
DEFAULT_LOGO = ROOT / "assets" / "logo.png"
DEFAULT_UPLOADS = ROOT / "assets" / "uploads"
DEFAULT_OUT = ROOT / "site"

# Custom domain for the published site. Written to site/CNAME on every
# build so the domain survives rebuilds and gh-pages publishes.
CUSTOM_DOMAIN = "flamingoandcactus.com"

THEMES = ("classic", "parchment", "ink", "midnight")
STAR_ANIMATIONS = ("portal", "fade", "none")
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def safe_custom_css(value: str | None) -> str:
    """Keep a bounded, user-authored CSS override without allowing a style close."""
    css = str(value or "")[:20000]
    return re.sub(r"</style", lambda _: "\\3c /style", css, flags=re.I)


def custom_css_error(value: str | None) -> str | None:
    """Reject custom CSS that imports remote code or fetches off-site assets."""
    css = str(value or "")
    if len(css) > 20000:
        return "Custom CSS is limited to 20,000 characters."
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Decode CSS escapes before checking the dangerous at-rule/function names.
    plain = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda m: chr(int(m.group(1), 16)), plain)
    plain = re.sub(r"\\(.)", r"\1", plain)
    if re.search(r"</style", plain, re.I):
        return "Custom CSS cannot contain a closing style tag."
    if re.search(r"@\s*import\b", plain, re.I):
        return "Custom CSS cannot import remote stylesheets."
    if re.search(r"image-set\s*\(", plain, re.I):
        return "Custom CSS cannot use image-set() — use url(uploads/...) instead."
    for match in re.finditer(r"url\(\s*(?:['\"]([^'\"]*)['\"]|([^)]*))\s*\)", plain, re.I):
        target = (match.group(1) if match.group(1) is not None else match.group(2) or "").strip()
        if not target.startswith(("uploads/", "/uploads/")) or ".." in target or "\\" in target:
            return "Custom CSS images must come from this site's uploads/ folder."
    return None


def logo_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def write_text_atomic(path: Path, text: str) -> None:
    """Truncate-free write: temp file + rename, so concurrent readers never
    see a half-written page during a rebuild."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        entry = {"slug": slug, "nav": str(p.get("nav", slug))[:60] or slug}
        if str(p.get("title", "")).strip():
            entry["title"] = str(p["title"]).strip()[:300]
        if str(p.get("description", "")).strip():
            entry["description"] = str(p["description"]).strip()[:300]
        if p.get("hidden"):
            entry["hidden"] = True
        if p.get("badge"):
            entry["badge"] = str(p["badge"])[:20]
        if p.get("form"):
            entry["form"] = True
        clean_pages.append(entry)
    if "index" not in seen:
        clean_pages.insert(0, {"slug": "index", "nav": "Home"})
    theme = str(doc.get("theme", "classic"))
    star_animation = str(doc.get("star_animation", "portal"))
    raw_custom_css = str(doc.get("custom_css", ""))
    css_error = custom_css_error(raw_custom_css)
    if css_error:
        raise ValueError(css_error)
    custom_css = safe_custom_css(raw_custom_css)
    return {
        "title": str(doc.get("title", "Flamingo and Cactus"))[:300],
        "tagline": str(doc.get("tagline", ""))[:300],
        "footer": str(doc.get("footer", ""))[:500],
        "theme": theme if theme in THEMES else "classic",
        "star_animation": star_animation if star_animation in STAR_ANIMATIONS else "portal",
        "description": str(doc.get("description", ""))[:300],
        "custom_css": custom_css,
        "pages": clean_pages,
    }


# ---------------------------------------------------------------------------
# Sanitizer: page bodies are freeform editor HTML, so only an allowlist of
# plain tags survives. editor and builder share this.
ALLOWED_TAGS = {"p", "h2", "h3", "br", "strong", "b", "em", "i", "u", "s",
                "ul", "ol", "li", "a", "img", "blockquote", "hr", "div", "span",
                "table", "thead", "tbody", "tr", "th", "td"}
DROP_WITH_CONTENT = {"script", "style", "form", "iframe", "object", "embed"}
IMG_CLASSES = {"secimg", "small", "medium", "full", "ileft", "iright", "icenter"}
# Text size / colour classes. Sizes work on blocks and inline spans; colours
# are the same set. Anything not listed here (other than "ac"/"ar", below) is
# dropped. Inline styles are dropped except strictly validated `color`
# (the colour wheel), `background-color` (the highlight wheel), numeric
# `font-size` (Google-Slides-style pixel sizes), unitless `line-height`
# (1.0-3.0) and px `letter-spacing` (-5..30); those are normalised into a
# single canonical `style="..."` attribute (see _style_attr).
SIZE_CLASSES = {"t-tiny", "t-small", "t-large", "t-huge", "t-giant"}
COLOR_CLASSES = {"c-muted", "c-red", "c-blue", "c-green", "c-gold"}
TEXT_CLASSES = SIZE_CLASSES | COLOR_CLASSES
# Block tags that keep the "ac"/"ar" alignment classes as well as size/colour.
CENTERED_TAGS = ("p", "h2", "h3", "blockquote", "li")

# A `color:` value is safe only in these shapes: hex, plain colour name, or a
# functional rgb()/hsl() with plain numbers. Anything else (url(), var(),
# expression(), quotes, braces) is rejected so no CSS escape hatch survives.
COLOR_VALUE_RE = re.compile(
    r"(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,30}"
    r"|rgba?\([0-9\s,./%]+\)|hsla?\([0-9\s,./%]+(?:deg|turn)?\))"
)


def _clean_color(style: str | None) -> str | None:
    """Return a canonical colour value from a style attribute, or None.

    Only the `color` property is read; every other declaration is dropped.
    Returns the value with whitespace removed (hex lowercased), or None when
    no safe colour is found.
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "color":
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not COLOR_VALUE_RE.fullmatch(v):
            continue
        if v.startswith("#"):
            v = "#" + v[1:].lower()
        return v
    return None


# Font stacks the toolbar can write. Keys are derived from the canonical
# values the same way input is normalized (whitespace and quotes stripped,
# lowercased), so only exact allowlist matches survive and every kept value
# is already canonical — a rebuild's second sanitize pass is byte-stable.
_FONT_STACKS = (
    "Georgia,Times New Roman,serif",
    "Arial,Helvetica,sans-serif",
    "Times New Roman,Times,serif",
    "Courier New,Courier,monospace",
    "Verdana,Geneva,sans-serif",
    "Trebuchet MS,Verdana,sans-serif",
    "Palatino Linotype,Palatino,serif",
    "Helvetica Neue,Helvetica,Arial,sans-serif",
    "Tahoma,Geneva,sans-serif",
    "Garamond,Georgia,serif",
)


def _font_key(value: str) -> str:
    return re.sub(r"\s+", "", value).replace('"', "").replace("'", "").lower()


_FONT_CANON = {_font_key(s): s for s in _FONT_STACKS}


def _clean_font(style: str | None) -> str | None:
    """Return a canonical font-family stack from a style attribute, or None.

    Only exact allowlist matches survive (compared with whitespace and
    quotes stripped, lowercased), so no hostile value can pass and every
    kept value is already canonical.
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "font-family":
            continue
        v = _font_key(val)
        if not re.fullmatch(r"[a-z0-9,.\-]+", v):
            continue
        if v in _FONT_CANON:
            return _FONT_CANON[v]
    return None


# A `font-size` value is safe only as a plain pixel length: an integer or
# one-to-two-decimal number of pixels (case-insensitive unit) in the
# inclusive range 8-200. Anything else (em/%/rem/ex, url(), expression(),
# quotes, braces, huge numbers) is rejected so no CSS escape hatch survives.
SIZE_VALUE_RE = re.compile(r"\d+(?:\.\d{1,2})?px", re.IGNORECASE)


def _clean_size(style: str | None) -> str | None:
    """Return a canonical font-size value from a style attribute, or None.

    Only the `font-size` property is read; every other declaration is dropped.
    Accepts an integer or one-decimal number of pixels from 8 to 200 inclusive
    and returns it with whitespace and leading zeros stripped (`18px`, `12.5px`),
    or None for anything else.
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "font-size":
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not SIZE_VALUE_RE.fullmatch(v):
            continue
        num_part = v[:-2]  # strip the literal `px` (any case)
        try:
            px = float(num_part)
        except ValueError:
            continue
        if not (8 <= px <= 200):  # 8px to 200px, inclusive
            continue
        if px == int(px):
            return f"{int(px)}px"
        s2 = f"{px:.2f}".rstrip("0").rstrip(".")
        return s2 + "px"
    return None


def _clean_bgcolor(style: str | None) -> str | None:
    """Return a canonical background-color value from a style attribute, or None.

    Same safety rules as :func:`_clean_color` (hex, plain colour name, or a
    functional rgb()/hsl() with plain numbers); every other declaration and
    any hostile value is dropped.
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "background-color":
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not COLOR_VALUE_RE.fullmatch(v):
            continue
        if v.startswith("#"):
            v = "#" + v[1:].lower()
        return v
    return None


# A `line-height` value is safe only as a plain unitless multiplier from 1.0
# to 3.0 (an integer or one-to-two-decimal number, e.g. `1.5`). Unitless
# values scale with the font size and cannot smuggle lengths or urls.
SIZE_MULT_RE = re.compile(r"\d+(?:\.\d{1,2})?")


def _clean_leading(style: str | None) -> str | None:
    """Return a canonical line-height multiplier from a style attribute, or None.

    Accepts a unitless number from 1.0 to 3.0 inclusive and returns it with
    whitespace stripped and trailing zeros removed (`1.5`, `2`), or None
    for anything else (px/em/%/keywords/hostile values are dropped).
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "line-height":
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not SIZE_MULT_RE.fullmatch(v):
            continue
        try:
            mult = float(v)
        except ValueError:
            continue
        if not (1.0 <= mult <= 3.0):
            continue
        if mult == int(mult):
            return f"{int(mult)}"
        s2 = f"{mult:.2f}".rstrip("0").rstrip(".")
        return s2
    return None


# A `letter-spacing` value is safe only as a plain pixel length from -5px to
# 30px (same shape rules as font-size). Anything else is rejected so no CSS
# escape hatch survives.
TRACK_VALUE_RE = re.compile(r"-?\d+(?:\.\d{1,2})?px", re.IGNORECASE)


def _clean_tracking(style: str | None) -> str | None:
    """Return a canonical letter-spacing value from a style attribute, or None.

    Accepts px lengths from -5 to 30 inclusive and returns them normalised
    (`2px`, `-1.5px`), or None for anything else (`normal`, em, hostile).
    """
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep or prop.strip().lower() != "letter-spacing":
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not TRACK_VALUE_RE.fullmatch(v):
            continue
        num_part = v[:-2]  # strip the literal `px` (any case)
        try:
            px = float(num_part)
        except ValueError:
            continue
        if not (-5 <= px <= 30):
            continue
        if px == int(px):
            return f"{int(px)}px"
        s2 = f"{px:.2f}".rstrip("0").rstrip(".")
        return s2 + "px"
    return None


def _style_attr(style: str | None) -> str:
    """Build one canonical style attribute: colour, highlight, then size, font, leading, tracking.

    Returns an empty string when no declaration is kept, so callers
    emit no style attribute at all.
    """
    parts = []
    col = _clean_color(style)
    if col:
        parts.append(f"color:{col}")
    bg = _clean_bgcolor(style)
    if bg:
        parts.append(f"background-color:{bg}")
    size = _clean_size(style)
    if size:
        parts.append(f"font-size:{size}")
    fnt = _clean_font(style)
    if fnt:
        parts.append(f"font-family:{fnt}")
    lead = _clean_leading(style)
    if lead:
        parts.append(f"line-height:{lead}")
    track = _clean_tracking(style)
    if track:
        parts.append(f"letter-spacing:{track}")
    return f' style="{";".join(parts)}"' if parts else ""


def _clean_href(href: str) -> str | None:
    h = (href or "").strip()
    # Reject traversal up front, before any allowlist branch.
    if ".." in h:
        return None
    if h.startswith(("http://", "https://", "mailto:", "tel:")):
        return h
    if h.startswith("#"):
        return h
    # Absolute paths: only safe page paths (no empty segments, no "//").
    if h.startswith("/"):
        if h.startswith("//"):
            return None
        if h == "/" or re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.html", h):
            return h
        return None
    # Plain page links (index.html) and relative subpage links (flank/index.html).
    if re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.html", h):
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
        # parallel to self.open: the original source tag for each emitted
        # element, so a close tag can be matched to its emitted counterpart
        # even when the tag was remapped (e.g. a plain <div> emitted as <p>)
        self.source_stack: list[str] = []
        # one entry per <span> start: True when the tag itself was emitted,
        # False when it was dropped for having no kept class (children stay)
        self.span_open: list[bool] = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        orig = tag.lower()
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth:
            return
        ad = dict(attrs)
        if tag == "div":
            # galleries and cards survive as divs; everything else is a paragraph
            classes = ad.get("class", "").split()
            if "gallery" in classes:
                pass
            elif "card" in classes:
                ad["class"] = "card"
            else:
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
            # new tabs: only a literal _blank target survives, always with noopener
            tgt_attr = ' target="_blank"' if ad.get("target", "") == "_blank" else ""
            rel_attr = ' rel="noopener"' if tgt_attr else ""
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
            if ad.get("class", "") == "card":
                self.out.append('<div class="card">')
            else:
                self.out.append('<div class="gallery">')
        elif tag == "span":
            # inline size/colour wrapper. A span with nothing left to keep
            # must not emit an empty tag: drop the tag, keep the children.
            keep = []
            for c in ad.get("class", "").split():
                if c in TEXT_CLASSES and c not in keep:
                    keep.append(c)
            attrs = ""
            if keep:
                attrs += f' class="{" ".join(keep)}"'
            # one canonical style attribute: colour first, then font-size
            attrs += _style_attr(ad.get("style", ""))
            if attrs:
                self.out.append(f"<span{attrs}>")
                self.span_open.append(True)
            else:
                self.span_open.append(False)
        else:
            extra = ""
            if tag in CENTERED_TAGS:
                # keep text alignment: from an inline style (toolbar
                # Left/Center/Right buttons) or from an "ac"/"ar" class
                # already on the tag, plus any allowed size/colour class, so
                # a rebuild's second sanitize pass preserves them. Every
                # other style and class is dropped.
                keep = []
                if "ac" in ad.get("class", "").split():
                    keep.append("ac")
                if "ar" in ad.get("class", "").split():
                    keep.append("ar")
                if re.search(r"text-align\s*:\s*center", ad.get("style", ""), re.I):
                    if "ac" not in keep:
                        keep.append("ac")
                if re.search(r"text-align\s*:\s*right", ad.get("style", ""), re.I):
                    if "ar" not in keep:
                        keep.append("ar")
                for c in ad.get("class", "").split():
                    if c in TEXT_CLASSES and c not in keep:
                        keep.append(c)
                if keep:
                    extra = f' class="{" ".join(keep)}"'
                # wheel-picked colour and numeric size survive together as
                # one validated inline style: colour first, then font-size
                extra += _style_attr(ad.get("style", ""))
            self.out.append(f"<{tag}{extra}>")
        if tag != "span":
            self.open.append(tag)
            self.source_stack.append(orig)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth:
            return
        if tag == "span":
            # closes the matching <span> start; </span> is emitted only when
            # the start tag itself was kept (see handle_starttag)
            if self.span_open and self.span_open.pop():
                self.out.append("</span>")
            return
        # Match by the original source tag so remapped elements (e.g. a plain
        # <div> emitted as <p>) close with the tag that was actually emitted.
        idx = None
        for i in range(len(self.source_stack) - 1, -1, -1):
            if self.source_stack[i] == tag:
                idx = i
                break
        if idx is None:
            return
        emitted = self.open[idx]
        del self.source_stack[idx]
        del self.open[idx]
        if emitted in ("br", "hr", "img"):
            return
        self.out.append(f"</{emitted}>")

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
  body[data-theme="midnight"]{
    --paper:#0d0a14;
    --paper2:#161021;
    --ink:#ece5f7;
    --ink2:#cfc3e8;
    --muted:#9d90b8;
    --faint:#6f6488;
    --rule:#33264d;
    --rule2:#241b38;
    --link:#d3b8ff;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:var(--paper);
    color:var(--ink);
    font:16px/1.6 Georgia,"Times New Roman",Times,serif;
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
  .canvas .ar{text-align:right}
  .canvas .t-tiny{font-size:12px}
  .canvas .t-small{font-size:13px}
  .canvas .t-large{font-size:19px}
  .canvas .t-huge{font-size:26px;line-height:1.35}
  .canvas .t-giant{font-size:34px;line-height:1.3}
  .canvas .c-muted{color:var(--muted)}
  .canvas .c-red{color:#b3261e}
  .canvas .c-blue{color:#0b2a8c}
  .canvas .c-green{color:#1a7f37}
  .canvas .c-gold{color:#9a6700}
  body[data-theme="ink"] .canvas .c-red{color:#ff8a80}
  body[data-theme="ink"] .canvas .c-blue{color:#9db8ff}
  body[data-theme="ink"] .canvas .c-green{color:#7ee2a8}
  body[data-theme="ink"] .canvas .c-gold{color:#d29922}
  body[data-theme="midnight"] .canvas .c-red{color:#ff9d9d}
  body[data-theme="midnight"] .canvas .c-blue{color:#b3a6ff}
  body[data-theme="midnight"] .canvas .c-green{color:#8fe6ae}
  body[data-theme="midnight"] .canvas .c-gold{color:#e3b83b}
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
  .canvas .card{
    border:1px solid var(--rule);border-radius:6px;background:var(--paper2);
    padding:18px 20px;margin:0 0 16px;
  }
  .canvas .card h2:first-child{margin-top:0}
  .canvas .card p:last-child{margin-bottom:0}
  .canvas .gallery{display:flex;gap:10px;margin:0 0 12px}
  .star-badge{
    position:fixed;top:80px;left:calc(50% - 606px);z-index:200;width:168px;height:168px;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    background:radial-gradient(circle at 35% 30%,#f3d97b,#c9a227 58%,#a37f14);
    color:#3a2d00;text-decoration:none;text-align:center;
    clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%);
    filter:drop-shadow(0 6px 10px rgba(0,0,0,.28));
    transition:transform .18s ease;
  }
  .star-badge:hover{transform:scale(1.07) rotate(-4deg)}
  .star-badge .star-title{display:block;max-width:58%;font:700 12px/1.3 "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase}
  .star-badge .star-sub{display:block;margin-top:4px;font:italic 10px Georgia,serif;animation:starpulse 3s ease-in-out infinite}
  @keyframes starpulse{0%,100%{opacity:.45}50%{opacity:.9}}
  @media (max-width:1250px){.star-badge{top:20px;left:56px}}
  @media print{.star-badge,.gem-badge{display:none}}
  .star-badge{cursor:pointer}
  .gem-badge{
    position:fixed;top:80px;left:calc(50% + 438px);z-index:200;width:170px;height:110px;
    display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#8fbef5,#1c4fd8 58%,#0b2a8c);
    color:#fff;text-decoration:none;text-align:center;
    border-radius:16px;
    filter:drop-shadow(0 6px 10px rgba(0,0,0,.28));
    transition:transform .18s ease;
  }
  .gem-badge:hover{transform:scale(1.06) rotate(3deg)}
  .gem-badge .gem-title{display:block;max-width:80%;font:700 13px/1.4 "Helvetica Neue",Arial,sans-serif;letter-spacing:2px;text-transform:uppercase}
  @media (max-width:1250px){.gem-badge{top:20px;left:auto;right:56px}}
  @media print{.gem-badge{display:none}}
  .star-badge.warp,.gem-badge.warp{animation:badgepop .6s cubic-bezier(.3,1.3,.4,1) forwards;z-index:1001;will-change:transform}
  @keyframes badgepop{
    0%{transform:scale(1);opacity:1}
    35%{transform:scale(1.22);opacity:1}
    100%{transform:scale(1.05);opacity:.25}
  }
  .star-badge.warp .star-sub{opacity:0}
  body.warping{overflow:hidden}
  .page{transition:opacity .3s ease,transform .3s ease}
  body.warping .page{opacity:0;transform:scale(.985)}
  #fx{position:fixed;inset:0;z-index:1000;pointer-events:none;overflow:hidden}
  #fx .flash{position:absolute;inset:0;background:radial-gradient(circle at var(--fx-x,50%) var(--fy,30%),#fffbe8,#f3d97b 45%,rgba(201,162,39,0));opacity:0;animation:fxflash .5s ease-out}
  @keyframes fxflash{0%{opacity:0}25%{opacity:.85}100%{opacity:0}}
  #fx canvas{position:absolute;inset:0;width:100%;height:100%}
  body.arrive .page{animation:arrivefade .55s cubic-bezier(.2,.9,.3,1) both}
  @keyframes arrivefade{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  #arriveflash{position:fixed;inset:0;z-index:1000;pointer-events:none;background:radial-gradient(circle at 50% 30%,#fffbe8,#f3d97b 45%,rgba(201,162,39,0));opacity:0;animation:flashfade .5s ease-out}
  @keyframes flashfade{0%{opacity:0}25%{opacity:.7}100%{opacity:0}}
  .canvas .gallery img{flex:1;min-width:0;object-fit:cover;margin:0;border:1px solid var(--rule2)}
  .canvas::after{content:"";display:table;clear:both}
  .canvas table{border-collapse:collapse;margin:0 0 12px;max-width:100%}
  .canvas th,.canvas td{border:1px solid var(--rule);padding:7px 10px;text-align:left;font-size:14.5px}
  .canvas th{background:var(--paper2);font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;color:var(--ink2)}
  .canvas img.ileft{float:left;margin:4px 14px 8px 0}
  .canvas img.iright{float:right;margin:4px 0 8px 14px}
  .canvas img.icenter{display:block;margin-left:auto;margin-right:auto}
  .contactform{max-width:560px;margin:26px auto 0;padding:20px 22px;border:1px solid var(--rule);border-radius:4px;background:var(--paper2)}
  .contactform label{display:block;font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink2);margin:14px 0 5px}
  .contactform label:first-of-type{margin-top:0}
  .contactform .sub{display:block;font:italic 12px Georgia,serif;letter-spacing:0;text-transform:none;color:var(--muted);margin-top:3px}
  .contactform input[type=text],.contactform input[type=email],.contactform input[type=tel],.contactform textarea{display:block;width:100%;box-sizing:border-box;font:15px Georgia,serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px;padding:9px 10px;margin:0}
  .contactform input[type=file]:not(.filepick){display:block;width:100%;box-sizing:border-box;font:15px Georgia,serif;color:var(--ink);background:var(--paper);border:1px dashed var(--rule);border-radius:2px;padding:14px 12px;margin:0;cursor:pointer}
  .contactform input.filepick{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;pointer-events:none}
  .contactform .dropzone{display:block;width:100%;margin:0;border:1px dashed var(--rule);border-radius:2px;background:var(--paper);padding:16px 14px;text-align:center;font:13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;text-transform:none;color:var(--muted);cursor:pointer;touch-action:manipulation}
  .contactform .dropzone:hover{color:var(--ink2)}
  .contactform .dropzone:focus{outline:1px solid #1c4fd8;border-color:#1c4fd8}
  .contactform .dropzone.over{border-color:#1c4fd8;color:var(--ink);background:var(--paper2)}
  .contactform .filelist{list-style:none;margin:8px 0 0;padding:0;font-size:13px;color:var(--ink2)}
  .contactform .filelist li{display:flex;gap:10px;align-items:center;justify-content:space-between;min-width:0;padding:5px 0;border-bottom:1px solid var(--rule2)}
  .contactform .filelist li span{flex:1;min-width:0;overflow-wrap:anywhere}
  .contactform .filelist button{margin:0;min-height:44px;min-width:72px;padding:8px 10px;background:none;border:1px solid var(--rule);border-radius:2px;color:var(--muted);cursor:pointer;font:12px "Helvetica Neue",Arial,sans-serif;letter-spacing:0;text-transform:none;flex:0 0 auto}
  .contactform .filelist button:hover{color:#b3261e;border-color:#b3261e}
  body[data-theme="ink"] .contactform .filelist button:hover{color:#ff8a80;border-color:#ff8a80}
  .contactform .filehint{font:italic 12px Georgia,serif;color:var(--muted);margin:6px 0 0}
  .contactform .filehint.over{color:#b3261e}
  body[data-theme="ink"] .contactform .filehint.over{color:#ff8a80}
  .contactform textarea{min-height:120px;resize:vertical}
  .contactform input:focus,.contactform textarea:focus{outline:2px solid var(--link);outline-offset:-1px;border-color:var(--link)}
  .contactform button{margin-top:16px;padding:10px 26px;font:13px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;background:var(--ink);color:var(--paper);border:1px solid var(--ink);border-radius:2px;cursor:pointer}
  .contactform button:hover{opacity:.85}
  footer{
    margin-top:34px;padding-top:14px;border-top:1px solid var(--rule);
    font:12.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint);text-align:center;
  }
  footer .themetoggle{
    display:inline-block;margin:10px 0 0;padding:7px 16px;
    border:1px solid var(--rule);border-radius:2px;background:transparent;
    color:var(--muted);cursor:pointer;
    font:11px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;
  }
  footer .themetoggle:hover{color:var(--ink);border-color:var(--ink);}
  img{max-width:100%;height:auto}
  /* ---------- small screens: same site, reshaped ---------- */
  @media (max-width:700px){
    body{font-size:15px}
    .page{padding:16px 14px 48px;overflow-x:hidden}
    .masthead{padding-top:140px}
    .masthead img{width:72px;height:72px}
    h1{font-size:22px;letter-spacing:1px;overflow-wrap:break-word}
    .tagline{font-size:13px}
    nav.sitenav{
      display:flex;flex-wrap:wrap;justify-content:center;gap:2px 4px;
      font-size:11px;line-height:2.1;
    }
    nav.sitenav a,nav.sitenav .cur{margin:0 8px;white-space:nowrap}
    .star-badge{top:10px;left:10px;right:auto;width:140px;height:140px}
    .star-badge .star-title{font-size:7px;max-width:62%;line-height:1.3;letter-spacing:.5px}
    .star-badge .star-sub{display:none}
    .gem-badge{top:10px;left:auto;right:10px;width:112px;height:76px;border-radius:11px}
    .gem-badge .gem-title{font-size:9px;letter-spacing:1.2px}
    .canvas{overflow-wrap:break-word}
    .canvas *{overflow-wrap:anywhere}
    /* giant inline titles (the toolbar writes up to 200px) stay dramatic
       on phones instead of running off the screen */
    .canvas [style*="font-size:4"],.canvas [style*="font-size:5"],
    .canvas [style*="font-size:6"],.canvas [style*="font-size:7"],
    .canvas [style*="font-size:8"],.canvas [style*="font-size:9"],
    .canvas [style*="font-size:10"],.canvas [style*="font-size:11"],
    .canvas [style*="font-size:12"],.canvas [style*="font-size:13"],
    .canvas [style*="font-size:14"],.canvas [style*="font-size:15"],
    .canvas [style*="font-size:16"],.canvas [style*="font-size:17"],
    .canvas [style*="font-size:18"],.canvas [style*="font-size:19"],
    .canvas [style*="font-size:200"]{
      font-size:min(11vw,40px) !important;line-height:1.15;
    }
    .canvas img.small{width:140px}
    .canvas img.medium{width:100%}
    .canvas table{display:block;overflow-x:auto}
    .canvas th,.canvas td{font-size:13.5px;padding:6px 8px}
    .canvas .t-giant{font-size:26px;line-height:1.25}
    .canvas .t-huge{font-size:22px}
    .canvas .gallery{flex-direction:column}
    .canvas .gallery img{width:100%}
    .canvas .card{padding:14px 14px}
    .contactform{margin:22px 0 0;padding:16px 14px}
  }
  @media (hover:none){.contactform .dropzone{padding:18px 14px}}
'''


# Structured data: who we are, for search engines. Plain string (not an
# f-string) so the JSON braces survive untouched.
LD_ORG = (
    '<script type="application/ld+json">{"@context":"https://schema.org",'
    '"@type":"Organization","name":"Flamingo and Cactus",'
    f'"url":"https://{CUSTOM_DOMAIN}/",'
    f'"logo":"https://{CUSTOM_DOMAIN}/favicon.png"}}'
    "</script>"
)


STAR_TITLE = "Latest and greatest"
STAR_SUB = "Click me"

# Inbox for the contact form. Submissions POST here and FormSubmit forwards
# them to this address (first submission needs a one-time activation click
# in the email FormSubmit sends). The address is already printed in plain
# text on the contact page, so the form exposes nothing new.
CONTACT_EMAIL = "flamingoncactusgroup@gmail.com"

CONTACT_FORM_HTML = (
  '<form class="contactform" action="https://formsubmit.co/' + CONTACT_EMAIL + '" method="POST" enctype="multipart/form-data">\n'
  '  <input type="hidden" name="_subject" value="New message from the Flamingo and Cactus contact form">\n'
  '  <input type="hidden" name="_template" value="table">\n'
  '  <input type="text" name="_honey" style="display:none">\n'
  '  <input type="hidden" id="cf-sentat" name="sent_at" value="">\n'
  '  <label for="cf-name">Your name<span class="sub">You can put &quot;Random&quot; if you would rather not say who you are.</span></label>\n'
  '  <input type="text" id="cf-name" name="name" required maxlength="120" autocomplete="name" placeholder="e.g. Random">\n'
  '  <label for="cf-contact">Email or phone<span class="sub">Optional &mdash; only if you want a reply.</span></label>\n'
  '  <input type="text" id="cf-contact" name="contact" maxlength="160" autocomplete="email" placeholder="Optional">\n'
  '  <label for="cf-note">Note</label>\n'
  '  <textarea id="cf-note" name="note" required maxlength="5000" placeholder="Anything you want us to know&hellip;"></textarea>\n'
  '  <label id="cf-files-label">Files<span class="sub">Optional — multiple files on supported browsers, 10 MB total.</span></label>\n'
  '  <input type="file" id="cf-photo" name="attachment" aria-labelledby="cf-files-label">\n'
  '  <p class="filehint" id="cf-filehint">No files yet.</p>\n'
  '  <noscript><p class="filehint">With JavaScript off, send one file per message.</p></noscript>\n'
  '  <button type="submit">Send it our way</button>\n'
  "</form>"
  '<script>(function(){try{'
  'var inp=document.getElementById("cf-photo"),hint=document.getElementById("cf-filehint"),form=inp&&inp.form;'
  'if(!inp||!hint||!form)return;'
  'function stampTime(){try{var st=document.getElementById("cf-sentat");if(st)st.value=new Date().toString();}catch(_){}}'
  'stampTime();'
  'function setFiles(target,files){try{var dt=new DataTransfer();for(var i=0;i<files.length;i++)dt.items.add(files[i]);target.files=dt.files;'
  'if(!target.files||target.files.length!==files.length)return false;'
  'for(var j=0;j<files.length;j++)if(target.files[j].name!==files[j].name||target.files[j].size!==files[j].size)return false;'
  'return true;}catch(_){return false;}}'
  'if(typeof DataTransfer==="undefined"||typeof File==="undefined"||!setFiles(inp,[new File(["check"],"fc-upload-check.txt",{type:"text/plain"})])){'
  'try{inp.value="";}catch(_){}hint.textContent="This browser can send one file per message. Update it to enable multi-file upload.";return;}'
  'try{inp.value="";}catch(_){}'
  'inp.className="filepick";inp.multiple=true;inp.setAttribute("tabindex","-1");inp.setAttribute("aria-hidden","true");'
  'var picker=document.createElement("input");picker.type="file";picker.id="cf-file-picker";picker.multiple=true;picker.className="filepick";picker.tabIndex=-1;picker.setAttribute("aria-hidden","true");'
  'var zone=document.createElement("button");zone.type="button";zone.className="dropzone";zone.id="cf-drop";'
  'zone.setAttribute("aria-labelledby","cf-files-label cf-drop-instructions");'
  'var instructions=document.createElement("span");instructions.id="cf-drop-instructions";'
  'instructions.textContent=window.matchMedia&&window.matchMedia("(hover:none)").matches?"Tap to browse, or paste files here.":"Drop files here, paste them, or click to browse.";zone.appendChild(instructions);'
  'var lst=document.createElement("ul");lst.className="filelist";lst.id="cf-files";'
  'inp.parentNode.insertBefore(zone,inp.nextSibling);zone.parentNode.insertBefore(lst,zone.nextSibling);'
  'form.appendChild(picker);var all=[],extras=[],MAX_BYTES=10000000;'
  'function mb(n){return (n/1000000).toFixed(1)+" MB";}'
  'function sync(){return setFiles(inp,all);}'
  'function failToNative(msg){all=[];for(var i=0;i<extras.length;i++)if(extras[i].parentNode)extras[i].parentNode.removeChild(extras[i]);extras=[];'
  'try{inp.value="";picker.value="";}catch(_){}inp.multiple=false;inp.className="";inp.removeAttribute("tabindex");inp.removeAttribute("aria-hidden");'
  'picker.disabled=true;zone.hidden=true;lst.hidden=true;hint.textContent=msg;hint.classList.add("over");}'
  'function render(){var t=0,i;lst.innerHTML="";'
  'for(i=0;i<all.length;i++)(function(f,ix){t+=f.size;'
  'var li=document.createElement("li"),nm=document.createElement("span"),b=document.createElement("button");'
  'nm.textContent=f.name+" ("+mb(f.size)+")";li.appendChild(nm);'
  'b.type="button";b.textContent="Remove";b.setAttribute("aria-label","Remove "+f.name);'
  'b.onclick=function(){all.splice(ix,1);sync();render();};li.appendChild(b);lst.appendChild(li);})(all[i],i);'
  'var over=t>MAX_BYTES;'
  'hint.textContent=!all.length?"No files yet.":all.length+(all.length===1?" file, ":" files, ")+mb(t)+" of 10 MB.";'
  'hint.classList.toggle("over",over);return !over;}'
  'function add(fs){for(var i=0;i<fs.length;i++)all.push(fs[i]);if(!sync()){failToNative("This browser could not safely combine files. Use the file picker and send one file per message.");return false;}render();return true;}'
  'zone.addEventListener("click",function(){picker.click();});'
  'zone.addEventListener("dragover",function(e){if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.indexOf("Files")>=0){e.preventDefault();zone.classList.add("over");}});'
  'zone.addEventListener("dragleave",function(){zone.classList.remove("over");});'
  'zone.addEventListener("drop",function(e){e.preventDefault();zone.classList.remove("over");var d=e.dataTransfer;if(d&&d.files&&d.files.length)add(d.files);});'
  'form.addEventListener("paste",function(e){var d=e.clipboardData;if(d&&d.files&&d.files.length){e.preventDefault();add(d.files);}});'
  'picker.addEventListener("change",function(){if(picker.files&&picker.files.length)add(picker.files);try{picker.value="";}catch(_){}});'
  'form.addEventListener("submit",function(e){stampTime();if(!render()){e.preventDefault();'
  'hint.textContent="Those files are over 10 MB in total — remove one and try again.";return;}'
  'for(var x=0;x<extras.length;x++)if(extras[x].parentNode)extras[x].parentNode.removeChild(extras[x]);extras=[];'
  'if(all.length){if(!setFiles(inp,[all[0]])){e.preventDefault();failToNative("This browser could not prepare the files. Use the file picker and send one file per message.");return;}'
  'inp.multiple=false;'
  'for(var xi=1;xi<all.length;xi++){var ni=document.createElement("input");ni.type="file";ni.name="attachment_"+(xi+1);ni.className="filepick filepick-extra";ni.tabIndex=-1;ni.setAttribute("aria-hidden","true");'
  'if(!setFiles(ni,[all[xi]])){e.preventDefault();failToNative("This browser could not prepare every file. Use the file picker and send one file per message.");return;}'
  'form.appendChild(ni);extras.push(ni);}}'
  'var btn=form.querySelector("button[type=submit]");if(btn){btn.disabled=true;btn.textContent="Sending…";}});'
  'window.addEventListener("pageshow",function(){var btn=form.querySelector("button[type=submit]");if(btn){btn.disabled=false;btn.textContent="Send it our way";}});'
  'render();}catch(_){}})();</script>'
)


def place_form(body_html: str, form_html: str) -> tuple:
    """Swap a `[contact form]` marker paragraph for the form.

    Returns (canvas_inner, leftover): when no marker matches, the canvas is
    untouched and the form comes back as leftover so the caller can render
    it after the canvas instead. The marker is plain page text, so it
    survives the sanitizer and normal editing.
    """
    first = True

    def replace_marker(_match):
        nonlocal first
        if first:
            first = False
            return form_html
        return ""

    placed, n = re.subn(r"<p[^>]*>\s*\[contact form\]\s*</p>",
                        replace_marker, body_html)
    if n:
        return placed, ""
    return body_html, form_html


def badge_slug(pages, name):
    """Slug of the hidden page carrying this badge, or None.

    Hidden pages without an explicit "badge" flag count as "star", so the
    original single-hidden-page setup keeps working unchanged."""
    return next((p["slug"] for p in pages
                 if p.get("hidden") and p.get("badge", "star") == name), None)


def star_slug(pages):
    """Slug of the hidden page the star links to, or None when there is none."""
    return badge_slug(pages, "star")


GEM_TITLE = "Feedback"


def star_html(pages):
    slug = star_slug(pages)
    if not slug:
        return ""
    return (
        f'<a class="star-badge" href="{html.escape(slug)}.html" title="{STAR_TITLE}">'
        f'<span class="star-title">{STAR_TITLE}</span>'
        f'<span class="star-sub">{STAR_SUB}</span></a>'
    )


def gem_html(pages):
    slug = badge_slug(pages, "gem")
    if not slug:
        return ""
    title = next((p["nav"] for p in pages if p["slug"] == slug), GEM_TITLE)
    return (
        f'<a class="gem-badge" href="{html.escape(slug)}.html" title="{html.escape(title)}">'
        f'<span class="gem-title">{html.escape(title)}</span></a>'
    )


def nav_html(pages, current: str) -> str:
    if len([p for p in pages if not p.get("hidden")]) <= 1:
        return ""
    parts = []
    for p in [p for p in pages if not p.get("hidden")]:
        if p["slug"] == current:
            parts.append(f'<span class="cur">{html.escape(p["nav"])}</span>')
        else:
            parts.append(
                f'<a href="{html.escape(p["slug"])}.html">{html.escape(p["nav"])}</a>')
    return '  <nav class="sitenav">\n    ' + "\n    ".join(parts) + "\n  </nav>\n"


def render_page(title: str, tagline: str, footer: str, theme: str,
                pages, slug: str, body_html: str, logo_uri: str,
                description: str = "", custom_css: str = "",
                star_animation: str = "portal") -> str:
    page_meta = next((p for p in pages if p["slug"] == slug), {})
    page_title = page_meta.get("title", "").strip() or title
    desc = page_meta.get("description", "").strip() or description.strip() or tagline.strip() or title
    canon = f"https://{CUSTOM_DOMAIN}/" if slug == "index" else f"https://{CUSTOM_DOMAIN}/{slug}.html"
    canvas = ""
    form_block = ""
    if any(p["slug"] == slug and p.get("form") for p in pages):
        # `[contact form]` typed anywhere in the page body marks the spot;
        # without it the form lands after the whole page body.
        placed, leftover = place_form(body_html, CONTACT_FORM_HTML)
        canvas = f'  <div class="canvas">\n{placed}\n  </div>\n' if placed else ""
        if leftover:
            form_block = f"  {leftover}\n"
    elif body_html:
        # The marker is reserved for the generated contact form; never let
        # the literal implementation token leak onto a page without a form.
        body_html = re.sub(r"<p[^>]*>\s*\[contact form\]\s*</p>", "", body_html)
        canvas = f'  <div class="canvas">\n{body_html}\n  </div>\n' if body_html else ""
    footer_text = html.escape(footer) + "\n" if footer.strip() else ""
    footer_block = ('  <footer>\n' + footer_text +
                    '  <div><button type="button" class="themetoggle" id="theme-toggle">Dark mode</button></div>\n'
                    "  </footer>\n")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(page_title)} &mdash; Flamingo &amp; Cactus</title>\n"
        f'<meta name="description" content="{html.escape(desc, quote=True)}">\n'
        f'<link rel="canonical" href="{canon}">\n'
        f'<meta property="og:title" content="{html.escape(page_title, quote=True)} &mdash; Flamingo &amp; Cactus">\n'
        f'<meta property="og:description" content="{html.escape(desc, quote=True)}">\n'
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:url" content="{canon}">\n'
        + LD_ORG + "\n"
        "<style>\n" + CSS + "\n" + safe_custom_css(custom_css) + "\n</style>\n"
        "</head>\n"
        f'<body data-theme="{theme}" data-star-animation="{star_animation if star_animation in STAR_ANIMATIONS else "portal"}">\n'
        '<div class="page">\n'
        f"{star_html(pages)}"
        f"{gem_html(pages)}"
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
        f"{form_block}"
        f"{footer_block}"
        "</div>\n"
        "<script>/* starburst: golden portal. Badge pops, a canvas starburst fires, the page fades — then go. Arrival is a soft flash and rise. All transform, opacity and canvas only. */"
        "(function(){"
        "try{"
        "var flag='fcWarp';"
        "try{var themeDef=document.body.dataset.theme||'classic';"
        "var themeSaved=null;try{themeSaved=localStorage.getItem('fc-theme');}catch(_){}"
        "if(themeSaved==='midnight'||themeSaved==='classic'||themeSaved==='parchment'||themeSaved==='ink'){document.body.dataset.theme=themeSaved;}"
        "var themeBtn=document.getElementById('theme-toggle');"
        "var themeLabel=function(){if(themeBtn)themeBtn.textContent=(document.body.dataset.theme==='midnight')?'Light mode':'Dark mode';};"
        "themeLabel();"
        "if(themeBtn)themeBtn.addEventListener('click',function(){"
        "var next=(document.body.dataset.theme==='midnight')?themeDef:'midnight';"
        "document.body.dataset.theme=next;"
        "try{localStorage.setItem('fc-theme',next);}catch(_){}"
        "themeLabel();});}catch(_){}"
        "if(sessionStorage.getItem(flag)){sessionStorage.removeItem(flag);"
        "var afl=null;if((document.body.dataset.starAnimation||'portal')==='portal'){afl=document.createElement('div');afl.id='arriveflash';document.body.appendChild(afl);}"
        "if((document.body.dataset.starAnimation||'portal')!=='none'){document.body.classList.add('arrive');"
        "setTimeout(function(){document.body.classList.remove('arrive');if(afl&&afl.parentNode)afl.parentNode.removeChild(afl);},700);}}"
        "}catch(_){}"
        "try{"
        "var instant=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;"
        "var star=document.querySelector('a.star-badge, a.gem-badge');"
        "if(!star||!star.getAttribute('href'))return;"
        "star.addEventListener('click',function(e){"
        "var href=star.getAttribute('href');"
        "var mode=document.body.dataset.starAnimation||'portal';"
        "if(!href||href.charAt(0)==='#')return;"
        "e.preventDefault();"
        "if(instant||mode==='none'){window.location.href=href;return;}"
        "if(star.classList.contains('warp'))return;"
        "if(mode==='fade'){try{sessionStorage.setItem(flag,'1')}catch(_){}star.classList.add('warp');document.body.classList.add('warping');"
        "setTimeout(function(){window.location.href=href;},320);setTimeout(function(){document.body.classList.remove('warping');star.classList.remove('warp');try{sessionStorage.removeItem(flag)}catch(_){}},3000);return;}"
        "var r=star.getBoundingClientRect();"
        "var x=r.left+r.width/2,y=r.top+r.height/2;"
        "try{sessionStorage.setItem(flag,'1')}catch(_){}"
        "star.classList.add('warp');document.body.classList.add('warping');"
        "var fx=document.createElement('div');fx.id='fx';"
        "fx.style.setProperty('--fx-x',x+'px');fx.style.setProperty('--fx-y',y+'px');"
        "var fl=document.createElement('div');fl.className='flash';fx.appendChild(fl);"
        "var cv=document.createElement('canvas');fx.appendChild(cv);"
        "document.body.appendChild(fx);"
        "var dpr=Math.min(window.devicePixelRatio||1,2);"
        "var W=window.innerWidth,H=window.innerHeight;"
        "cv.width=W*dpr;cv.height=H*dpr;"
        "var cx2=cv.getContext('2d');cx2.scale(dpr,dpr);"
        "var cols=['#fffbe8','#f7e08b','#c9a227','#e8c84a','#8a6a0f'];"
        "var N=W<700?26:60,P=[],i,a,sp;"
        "for(i=0;i<N;i++){a=Math.random()*Math.PI*2;sp=2+Math.random()*6;"
        "P.push({x:x,y:y,vx:Math.cos(a)*sp,vy:Math.sin(a)*sp-1.5,r:3+Math.random()*6,rot:Math.random()*Math.PI*2,vr:(Math.random()-.5)*.3,age:0,life:35+Math.random()*30,c:cols[(Math.random()*cols.length)|0]});}"
        "var ring={age:0,life:30};"
        "function drawStar(s){cx2.beginPath();for(var k=0;k<10;k++){var rr=k%2?s.r*.45:s.r,aa=s.rot+k*Math.PI/5;cx2[k?'lineTo':'moveTo'](s.x+Math.cos(aa)*rr,s.y+Math.sin(aa)*rr);}cx2.closePath();}"
        "var t0=performance.now();"
        "(function frame(){"
        "var alive=false,j,s;"
        "cx2.clearRect(0,0,W,H);"
        "for(j=0;j<P.length;j++){s=P[j];s.age++;if(s.age>s.life)continue;alive=true;"
        "s.x+=s.vx;s.y+=s.vy;s.vx*=.985;s.vy=s.vy*.985+.06;s.rot+=s.vr;"
        "cx2.globalAlpha=Math.max(0,1-s.age/s.life);cx2.fillStyle=s.c;drawStar(s);cx2.fill();}"
        "if(ring.age<ring.life){ring.age++;alive=true;"
        "var rp=ring.age/ring.life;cx2.globalAlpha=(1-rp)*.8;cx2.strokeStyle='#f3d97b';cx2.lineWidth=4*(1-rp)+1;"
        "cx2.beginPath();cx2.arc(x,y,8+rp*Math.max(W,H)*.35,0,Math.PI*2);cx2.stroke();}"
        "cx2.globalAlpha=1;"
        "if(alive&&performance.now()-t0<1500)requestAnimationFrame(frame);"
        "else if(fx.parentNode)fx.parentNode.removeChild(fx);"
        "})();"
        "var go=false;function nav(){if(go)return;go=true;window.location.href=href;}"
        "setTimeout(nav,550);setTimeout(nav,1200);"
        "setTimeout(function(){document.body.classList.remove('warping');star.classList.remove('warp');var f=document.getElementById('fx');if(f&&f.parentNode)f.parentNode.removeChild(f);},2500);});"
        "}catch(_){}"
        "})();</script>"
        "</body>\n"
        "</html>\n"
    )


NOTFOUND = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found &mdash; Flamingo &amp; Cactus</title>
<meta name="robots" content="noindex">
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
  <footer>
  <div><button type="button" class="themetoggle" id="theme-toggle">Dark mode</button></div>
  </footer>
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
            write_text_atomic(pages_dir / f"{p['slug']}.html", clean)
        write_text_atomic(out_dir / f"{p['slug']}.html",
            render_page(site["title"], site["tagline"], site["footer"],
        site["theme"], site["pages"], p["slug"], clean, logo_uri,
                        site.get("description", ""), site.get("custom_css", ""),
                        site.get("star_animation", "portal")))
    nf = (NOTFOUND.replace("__CSS__", CSS + "\n" + safe_custom_css(site.get("custom_css", "")))
          .replace("__LOGO__", logo_uri)
          .replace('data-theme="classic"', f'data-theme="{site["theme"]}"'))
    write_text_atomic(out_dir / "404.html", nf)
    shutil.copyfile(logo_path, out_dir / "favicon.png")
    write_text_atomic(out_dir / ".nojekyll", "")
    write_text_atomic(out_dir / "CNAME", CUSTOM_DOMAIN + "\n")
    write_text_atomic(out_dir / "robots.txt",
        f"User-agent: *\nAllow: /\nSitemap: https://{CUSTOM_DOMAIN}/sitemap.xml\n")
    listed = [p["slug"] for p in site["pages"] if not p.get("hidden")]
    if "index" not in listed:
        listed.insert(0, "index")
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for slug in listed:
        loc = f"https://{CUSTOM_DOMAIN}/" if slug == "index" else f"https://{CUSTOM_DOMAIN}/{slug}.html"
        sitemap.append(f"<url><loc>{loc}</loc></url>")
    sitemap.append("</urlset>")
    write_text_atomic(out_dir / "sitemap.xml", "\n".join(sitemap) + "\n")

    site_uploads = out_dir / "uploads"
    if site_uploads.exists():
        shutil.rmtree(site_uploads)
    if uploads_dir.is_dir():
        site_uploads.mkdir(parents=True, exist_ok=True)
        files = [f for f in sorted(uploads_dir.iterdir()) if f.is_file()]
        for f in files:
            shutil.copyfile(f, site_uploads / f.name)

    # The hidden ledger: data/The Flank.md -> site/flank/. Unlisted (not in
    # the nav, not in site.json) — the Financials page button is the only
    # link to it. Skipped with a warning when the ledger is absent so the
    # main pages still build.
    ledger_path = data_path.parent / "The Flank.md"
    if ledger_path.is_file():
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build_flank
        flank_logo = build_flank.DEFAULT_LOGO
        if not flank_logo.is_file():
            print(f"warning: flank logo not found, reusing main logo: {flank_logo}")
            flank_logo = logo_path
        build_flank.build(ledger_path, flank_logo, out_dir / "flank")
    else:
        print(f"warning: ledger not found, skipping flank/: {ledger_path}")

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
        expect(('data-star-animation="' + site.get("star_animation", "portal") + '"') in idx,
               "site selects its star transition mode in generated pages")
        page_meta = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                [{"slug":"index", "nav":"Home", "title":"Page-specific title",
                                  "description":"Page-specific search text"}], "index", "", "x",
                                site.get("description", ""))
        expect("Page-specific title" in page_meta and 'content="Page-specific search text"' in page_meta,
               "page-level browser title and search description override site defaults")
        style_test = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                 site["pages"], "index", "", "x", site.get("description", ""),
                                 ".mobile-test{font-size:20px}", "fade")
        expect('.mobile-test{font-size:20px}' in style_test and 'data-star-animation="fade"' in style_test,
               "advanced CSS and star transition settings reach the generated page")
        expect("</style" not in safe_custom_css(".x{} </STYLE><script>bad()</script>"),
               "advanced CSS cannot close the site's style element")
        expect(custom_css_error("@import url(https://example.invalid/a.css);") is not None
               and custom_css_error(".x{background:url(https://example.invalid/a.png)}") is not None,
               "advanced CSS blocks remote imports and off-site requests")
        expect(custom_css_error(".x{background:url(uploads/site-image.png)}") is None,
               "advanced CSS permits local uploaded images")
        expect(custom_css_error("</style><script>bad()</script>") is not None,
               "advanced CSS rejects a closing style tag instead of rewriting user text")
        expect(custom_css_error(".x{background-image:image-set(url(uploads/a.png) 1x)}") is not None,
               "advanced CSS blocks image-set() off-ramps too")
        expect(custom_css_error("/* </style> */ .x{color:red}") is None
               and "</style" not in safe_custom_css("/* </style> */ .x{}"),
               "commented-out closing tags are neutralized by escaping, not rejected")
        dupmark = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                              site["pages"], "contact", "<p>[contact form]</p><p>mid</p><p>[contact form]</p>", "x")
        expect(dupmark.count('class="contactform"') == 1 and "[contact form]" not in dupmark,
               "duplicate form markers collapse to one form, never leak marker text")
        nomark = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                             [{"slug":"contact", "nav":"Contact"}], "contact",
                             "<p>[contact form]</p><p>hi</p>", "x")
        expect("[contact form]" not in nomark and ">hi<" in nomark,
               "pages without the form never publish the marker text")
        expect("viewport" in idx and "width=device-width" in idx, "pages scale on phones (viewport meta)")
        expect("@media (max-width:" in idx, "pages reshape on small screens")
        expect("left:10px" in idx and "flex-wrap:wrap" in idx,
               "phone header: star top-left, nav wraps without touching")
        expect("overflow-wrap:anywhere" in idx,
               "unbreakable titles wrap on phones (no sideways cutoff)")
        expect("starburst" in idx and "badgepop" in idx and "arrivefade" in idx,
               "portal: badge pop, canvas starburst and soft arrival present")
        warp_scripts = idx.lower().count("<script")
        expect(warp_scripts == 2 and "starburst" in idx.lower() and "ld+json" in idx.lower(),
               "built pages carry only the star-warp + structured-data scripts")
        expect('name="description"' in idx and html.escape(site["description"])[:40] in idx,
               "every page carries the search snippet")
        expect('rel="canonical" href="https://flamingoandcactus.com/"' in idx,
               "home page canonical points at the domain root")
        expect('property="og:title"' in idx and 'property="og:url"' in idx,
               "open-graph tags present for link previews")
        expect((tmp / "robots.txt").is_file(), "robots.txt built")
        sm = (tmp / "sitemap.xml").read_text(encoding="utf-8") if (tmp / "sitemap.xml").is_file() else ""
        expect("<loc>https://flamingoandcactus.com/</loc>" in sm, "sitemap lists home")
        expect("<loc>https://flamingoandcactus.com/contact.html</loc>" in sm, "sitemap lists contact")
        expect("flank" not in sm and "latest" not in sm and "feedback" not in sm,
               "sitemap keeps unlisted pages unlisted")
        contact_built = (tmp / "contact.html").read_text(encoding="utf-8") if (tmp / "contact.html").is_file() else ""
        if any(p.get("form") for p in site["pages"]):
            expect('action="https://formsubmit.co/' in contact_built, "contact page posts to the form inbox")
            expect('name="name"' in contact_built and 'name="note"' in contact_built, "contact form asks for name + note")
            expect("&quot;Random&quot;" in contact_built, "contact form notes Random is fine")
            expect('name="contact"' in contact_built, "contact form has one optional email-or-phone field")
            expect('name="_honey"' in contact_built, "contact form carries a spam honeypot")
            expect('name="sent_at"' in contact_built and "stampTime" in contact_built,
                   "contact form stamps the sender's local time on every submit")
            expect('type="file"' in contact_built and 'name="attachment"' in contact_built
                   and 'ni.name="attachment_"+(xi+1)' in contact_built
                   and "MAX_BYTES=10000000" in contact_built,
                   "contact form uses one uniquely named field per file, capped at 10 MB")
            expect("formsubmit.co" not in idx, "form appears only on the flagged page, not home")
            marked = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                 site["pages"], "contact", "<p>hi</p><p>[contact form]</p><p>bye</p>", "x",
                                 site.get("description", ""))
            expect("formsubmit.co" in marked and "[contact form]" not in marked,
                   "marker paragraph swaps in the live form")
            expect(marked.index("contactform") < marked.index("bye"),
                   "marker puts the form where it was typed, not at the end")
            repeated = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                   [{"slug":"contact", "nav":"Contact", "form":True}], "contact",
                                   "<p>[contact form]</p><p>Middle</p><p>[contact form]</p>", "x")
            expect(repeated.count('class="contactform"') == 1 and "[contact form]" not in repeated,
                   "duplicate contact markers never duplicate the form or leak marker text")
            disabled_marker = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                          [{"slug":"contact", "nav":"Contact"}], "contact",
                                          "<p>[contact form]</p><p>Contact text</p>", "x")
            expect("[contact form]" not in disabled_marker and "Contact text" in disabled_marker,
                   "disabling a form removes its reserved marker from the public page")
        expect("onclick" not in idx.lower(), "no event-handler attributes in built page")
        expect('body[data-theme="parchment"]' in idx, "parchment theme CSS present")
        expect('body[data-theme="ink"]' in idx, "ink theme CSS present")
        if len(site["pages"]) > 1:
            for p in site["pages"]:
                expect((tmp / f"{p['slug']}.html").is_file(), f"{p['slug']}.html built")
                if p["slug"] != "index" and not p.get("hidden"):
                    expect(f'href="{p["slug"]}.html"' in idx, f"nav links to {p['slug']}")
        # build artifacts
        expect((tmp / "404.html").is_file(), "404.html built")
        expect((tmp / "favicon.png").is_file(), "favicon.png built")
        expect((tmp / ".nojekyll").is_file(), ".nojekyll built")
        expect((tmp / "CNAME").is_file(), "CNAME built")
        expect((tmp / "uploads").is_dir(), "site/uploads/ exists in build")

        # FIX 1: hostile hrefs dropped, safe ones survive
        for bad_href in ("//evil.com/x", "/../../etc/passwd", "javascript:alert(1)", "data:text/html,hi"):
            bad = sanitize(f'<a href="{bad_href}">Hi</a>')
            expect("href=" not in bad and "Hi" in bad, f"sanitizer drops hostile href {bad_href}")
        for good_href in ("https://example.com/x", "flank/index.html", "index.html", "#",
                           "mailto:a@b.com", "tel:+16072056663"):
            good = sanitize(f'<a href="{good_href}">Hi</a>')
            expect(f'href="{good_href}"' in good, f"sanitizer keeps safe href {good_href}")
        # FIX 2-ish handled in flank check; FIX 3: 150px capped in CSS
        expect('font-size:15' in idx, "150px titles are capped on phones")
        # FIX 4: div close balanced
        divbalanced = sanitize('<div><p>x</p></div>')
        expect(divbalanced.count("<p") == divbalanced.count("</p>"), "div close emits matching p close")

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
        right = sanitize('<p style="text-align: right">x</p>')
        expect(right == '<p class="ar">x</p>',
               "sanitizer keeps right-align as a class, drops the style")
        expect(sanitize(right) == right,
               "right-align survives a rebuild (second sanitize pass)")
        # text size / colour classes
        styled = sanitize('<p class="t-large c-red">Hi</p>')
        expect(styled == '<p class="t-large c-red">Hi</p>',
               "sanitizer keeps size/color classes on p")
        spanned = sanitize('<p><span class="c-blue t-small">x</span></p>')
        expect(spanned == '<p><span class="c-blue t-small">x</span></p>',
               "sanitizer keeps size/color classes on span")
        wiped = sanitize('<p class="evil" style="font-size:999px;color:lime">Hi</p>')
        expect(wiped == '<p style="color:lime">Hi</p>',
                "sanitizer drops unknown classes and styles but keeps a safe color")
        evil_style = sanitize('<p style="color:red;background:url(x);font-size:10em">Hi</p>')
        expect(evil_style == '<p style="color:red">Hi</p>',
                "sanitizer keeps only the color declaration, drops the rest")
        expr = sanitize('<p style="color:expression(alert(1))">Hi</p>')
        expect("style=" not in expr and "Hi" in expr,
                "sanitizer drops hostile color values entirely")
        hexed = sanitize('<p style="COLOR:#B3261E">Hi</p>')
        expect(hexed == '<p style="color:#b3261e">Hi</p>',
                "sanitizer normalises hex colors (second-pass stable)")
        expect(sanitize(hexed) == hexed,
                "wheel colors survive a second sanitize pass (rebuild stability)")
        wheel_span = sanitize('<p><span style="color:#123456">x</span></p>')
        expect(wheel_span == '<p><span style="color:#123456">x</span></p>',
                "sanitizer keeps wheel colors on spans")
        bare = sanitize('<p><span class="evil">x</span><span>y</span></p>')
        expect(bare == "<p>xy</p>", "classless spans are omitted, children kept")
        mixed = sanitize('<p class="t-huge" style="text-align: center">x</p>')
        expect(mixed == '<p class="ac t-huge">x</p>',
               "centering and size/color kept together on the same tag")
        liStyled = sanitize('<li class="c-gold t-small">x</li>')
        expect(liStyled == '<li class="c-gold t-small">x</li>',
               "sanitizer keeps size/color classes on li")
        again = sanitize(styled + spanned + mixed + liStyled)
        expect(again == styled + spanned + mixed + liStyled,
               "size/color survive a second sanitize pass (rebuild stability)")
        # numeric font sizes (Google Slides style), additive to the t-* classes
        sized_p = sanitize('<p style="font-size:36px">x</p>')
        expect(sized_p == '<p style="font-size:36px">x</p>',
               "sanitizer keeps font-size:36px on p")
        sized_span = sanitize('<p><span style="font-size:36px">x</span></p>')
        expect(sized_span == '<p><span style="font-size:36px">x</span></p>',
               "sanitizer keeps font-size:36px on span")
        canonical = sanitize('<p style="font-size: 018.0px">x</p>')
        expect(canonical == '<p style="font-size:18px">x</p>',
               "font-size canonicalised (whitespace and leading zeros stripped)")
        sized_h2 = sanitize('<h2 style="font-size:36px">x</h2>')
        expect(sized_h2 == '<h2 style="font-size:36px">x</h2>',
               "inline font-size kept on h2 (beats the 12px rule by specificity)")
        for bad_size in ("font-size:999px", "font-size:10em", "font-size:10%",
                         "font-size:expression(alert(1))"):
            dropped = sanitize(f'<p style="{bad_size}">Hi</p>')
            expect("style=" not in dropped and "Hi" in dropped,
                   f"sanitizer drops {bad_size}")
        combined = sanitize('<p style="color:red;font-size:36px">x</p>')
        expect(combined == '<p style="color:red;font-size:36px">x</p>',
               "combined style=color + font-size on one attribute, byte-identical")
        expect(sanitize(combined) == combined,
               "combined color+font-size survives a second sanitize pass (byte-identical)")
        combined_span = sanitize('<p><span class="t-small" style="color:#b3261e;'
                                 'font-size:36px">x</span></p>')
        expect(combined_span == '<p><span class="t-small" '
                                 'style="color:#b3261e;font-size:36px">x</span></p>',
               "kept class plus combined color/size on one span attribute")
        expect(sanitize(combined_span) == combined_span,
               "combined span style survives a second sanitize pass")
        sized_centered = sanitize('<p style="text-align:center;color:#123456;'
                                  'font-size:24px">x</p>')
        expect(sized_centered == '<p class="ac" style="color:#123456;font-size:24px">x</p>',
               "centered tag keeps class, color and size in canonical order")
        bare_size = sanitize('<p><span style="font-size:7px">x</span></p>')
        expect(bare_size == "<p>x</p>",
               "span with no kept class/color/size emits no tag, children kept")
        base_rules = (".canvas .t-tiny", ".canvas .t-small", ".canvas .t-large",
                      ".canvas .t-huge", ".canvas .t-giant",
                      ".canvas .c-muted", ".canvas .c-red", ".canvas .c-blue",
                      ".canvas .c-green", ".canvas .c-gold")
        missing = [r for r in base_rules if r not in CSS]
        expect(not missing, f"CSS size/color rules present ({', '.join(missing) or 'all'})")
        ink_rules = tuple(r.replace(".canvas .c", 'body[data-theme="ink"] .canvas .c')
                          for r in base_rules if ".canvas .c-" in r and r != ".canvas .c-muted")
        missing = [r for r in ink_rules if r not in CSS]
        expect(not missing, f"CSS ink-theme color overrides present ({', '.join(missing) or 'all'})")
        night_rules = tuple(r.replace(".canvas .c", 'body[data-theme="midnight"] .canvas .c')
                            for r in base_rules if ".canvas .c-" in r and r != ".canvas .c-muted")
        missing = [r for r in night_rules if r not in CSS]
        expect(not missing, f"CSS midnight-theme color overrides present ({', '.join(missing) or 'all'})")
        expect('body[data-theme="midnight"]' in CSS,
               "midnight dark-purple palette present in CSS")
        expect('id="theme-toggle"' in idx,
               "every built page carries the dark-mode toggle")
        expect(all(r in idx for r in base_rules),
               "size/color CSS reaches the built page")
        card = sanitize('<div class="card"><h2 class="t-giant">T</h2><p>x</p></div>')
        expect(card == '<div class="card"><h2 class="t-giant">T</h2><p>x</p></div>',
                "sanitizer keeps card sections with their content")
        expect(sanitize(card) == card,
                "card sections survive a second sanitize pass")
        expect(".canvas .card" in CSS and ".canvas .t-giant" in CSS
               and ".canvas .t-tiny" in CSS,
                "card + extended-size CSS present")
        fonted = sanitize('<p style="font-family:Arial,Helvetica,sans-serif">Hi</p>')
        expect(fonted == '<p style="font-family:Arial,Helvetica,sans-serif">Hi</p>',
               "sanitizer keeps an allowlisted font stack on p")
        expect(sanitize(fonted) == fonted,
               "font stacks survive a second sanitize pass (byte-stable)")
        quoted = sanitize('<p style="font-family: Georgia, \'Times New Roman\', serif">Hi</p>')
        expect(quoted == '<p style="font-family:Georgia,Times New Roman,serif">Hi</p>',
               "sanitizer canonicalises spaced/quoted stacks")
        for bad_font in ("font-family:expression(alert(1))", "font-family:Comic Sans",
                         "font-family:Arial;url(x)", "font-family:Georgia,serif,evil()"):
            dropped = sanitize('<p style="%s">Hi</p>' % bad_font)
            expect("font-family" not in dropped and "Hi" in dropped,
                   "sanitizer drops hostile font " + bad_font)
        # line height (unitless multiplier), additive to everything else
        leaded = sanitize('<p style="line-height:1.5">x</p>')
        expect(leaded == '<p style="line-height:1.5">x</p>',
               "sanitizer keeps line-height:1.5 on p")
        leaded_span = sanitize('<p><span style="line-height:2">x</span></p>')
        expect(leaded_span == '<p><span style="line-height:2">x</span></p>',
               "sanitizer keeps line-height:2 on span, trailing zero stripped")
        expect(sanitize(leaded) == leaded and sanitize(leaded_span) == leaded_span,
               "line-height survives a second sanitize pass (byte-stable)")
        for bad_lead in ("line-height:0.5", "line-height:5", "line-height:24px",
                         "line-height:1.5em", "line-height:normal",
                         "line-height:expression(alert(1))"):
            dropped = sanitize(f'<p style="{bad_lead}">Hi</p>')
            expect("style=" not in dropped and "Hi" in dropped,
                   f"sanitizer drops {bad_lead}")
        # letter spacing, additive to everything else
        tracked = sanitize('<p style="letter-spacing:2px">x</p>')
        expect(tracked == '<p style="letter-spacing:2px">x</p>',
               "sanitizer keeps letter-spacing:2px on p")
        tracked_neg = sanitize('<p><span style="letter-spacing:-1.5px">x</span></p>')
        expect(tracked_neg == '<p><span style="letter-spacing:-1.5px">x</span></p>',
               "sanitizer keeps negative letter-spacing on span")
        expect(sanitize(tracked) == tracked and sanitize(tracked_neg) == tracked_neg,
               "letter-spacing survives a second sanitize pass (byte-stable)")
        for bad_track in ("letter-spacing:99px", "letter-spacing:-99px",
                          "letter-spacing:1em", "letter-spacing:normal",
                          "letter-spacing:expression(alert(1))"):
            dropped = sanitize(f'<p style="{bad_track}">Hi</p>')
            expect("style=" not in dropped and "Hi" in dropped,
                   f"sanitizer drops {bad_track}")
        # text highlight (background-color), same value rules as text colour
        hiled = sanitize('<p style="background-color:#f3d97b">x</p>')
        expect(hiled == '<p style="background-color:#f3d97b">x</p>',
               "sanitizer keeps background-color highlight on p")
        hiled_named = sanitize('<p style="background-color:lime">x</p>')
        expect(hiled_named == '<p style="background-color:lime">x</p>',
               "sanitizer keeps named highlight colours")
        expect(sanitize(hiled) == hiled,
               "highlight survives a second sanitize pass (byte-stable)")
        for bad_hl in ("background-color:url(x)", "background-color:expression(alert(1))",
                       "background-color:red url(x)"):
            dropped = sanitize(f'<p style="{bad_hl}">Hi</p>')
            expect("background-color" not in dropped and "Hi" in dropped,
                   f"sanitizer drops hostile highlight {bad_hl}")
        full_house = sanitize('<p style="color:#b3261e;background-color:#fffbe8;'
                              'font-size:18px;font-family:Tahoma,Geneva,sans-serif;'
                              'line-height:1.5;letter-spacing:1px">x</p>')
        expect(full_house == '<p style="color:#b3261e;background-color:#fffbe8;'
                             'font-size:18px;font-family:Tahoma,Geneva,sans-serif;'
                             'line-height:1.5;letter-spacing:1px">x</p>',
               "all six text properties share one canonical attribute, in order")
        expect(sanitize(full_house) == full_house,
               "full six-property style survives a second sanitize pass")
        # new font stacks behave exactly like the old seven
        for stack in ("Helvetica Neue,Helvetica,Arial,sans-serif",
                      "Tahoma,Geneva,sans-serif",
                      "Garamond,Georgia,serif"):
            sf = sanitize('<p style="font-family:%s">Hi</p>' % stack)
            expect(sf == '<p style="font-family:%s">Hi</p>' % stack and sanitize(sf) == sf,
                   "sanitizer keeps new stack " + stack.split(",")[0])
        tabled = sanitize('<table><tbody><tr><th>H</th></tr><tr><td>x</td></tr></tbody></table>')
        expect(tabled == '<table><tbody><tr><th>H</th></tr><tr><td>x</td></tr></tbody></table>',
               "sanitizer keeps plain tables")
        expect(sanitize('<img src="uploads/a.png" class="secimg medium ileft">') ==
               '<img class="secimg medium ileft" src="uploads/a.png" alt="">',
               "sanitizer keeps image alignment classes")
        expect(".canvas table" in CSS and ".canvas img.ileft" in CSS,
               "table + image-alignment CSS present")
        star = star_html([{"slug": "a", "nav": "A"}, {"slug": "z", "nav": "Z", "hidden": True}])
        expect('href="z.html"' in star and "Latest and greatest" in star and "Click me" in star,
                "star links the hidden page with its text")
        expect(star_html([{"slug": "a", "nav": "A"}]) == "",
                "no star without a hidden page")
        gem = gem_html([{"slug": "a", "nav": "A"}, {"slug": "f", "nav": "Feedback", "hidden": True, "badge": "gem"}])
        expect('href="f.html"' in gem and "Feedback" in gem,
                "gem badge links the gem-flagged hidden page")
        expect(gem_html([{"slug": "a", "nav": "A"}]) == "",
                "no gem badge without a gem-flagged page")
        expect(gem_html([{"slug": "a", "nav": "A"}, {"slug": "z", "nav": "Z", "hidden": True}]) == "",
                "a plain hidden page does not summon the gem badge")
        expect('href="feedback.html"' in idx and "gem-badge" in idx,
                "built home carries the gem badge to the feedback page")
        nohidden = nav_html([{"slug": "a", "nav": "A"}, {"slug": "b", "nav": "B"}, {"slug": "z", "nav": "Z", "hidden": True}], "a")
        expect("z.html" not in nohidden and ">A</span>" in nohidden and "b.html" in nohidden,
                "hidden pages stay out of the nav")
        expect('class="cur"' not in nav_html([{"slug": "a", "nav": "A"}, {"slug": "z", "nav": "Z", "hidden": True}], "z"),
                "open hidden page shows sibling nav with no current marker")
        newtab = sanitize('<p><a class="btn" href="https://example.com/x" '
                          'target="_blank" rel="noopener">Hi</a></p>')
        expect('target="_blank"' in newtab and 'rel="noopener"' in newtab,
               "sanitizer keeps new-tab buttons")
        notab = sanitize('<a href="https://example.com/x" target="_self">Hi</a>')
        expect("target=" not in notab, "sanitizer drops non-blank targets")
        sub = sanitize('<p class="ac"><a href="flank/index.html" class="btn">The Flank</a></p>')
        expect('href="flank/index.html"' in sub, "sanitizer keeps relative subpage links")
        for bad_href in ("../secret.html", "flank/../../etc/passwd",
                         "javascript:alert(1)", "data:text/html,hi"):
            bad = sanitize(f'<a href="{bad_href}">Hi</a>')
            expect("href=" not in bad and "Hi" in bad,
                   f"sanitizer drops hostile href {bad_href}")
        expect((tmp / "CNAME").read_text(encoding="utf-8").strip() == CUSTOM_DOMAIN,
               "CNAME carries the custom domain")
        flank_idx = tmp / "flank" / "index.html"
        flank_about = tmp / "flank" / "about.html"
        expect(flank_idx.is_file(), "site/flank/index.html built")
        expect(flank_about.is_file(), "site/flank/about.html built")
        if flank_idx.is_file():
            flank_html = flank_idx.read_text(encoding="utf-8")
            expect('href="../index.html"' in flank_html,
                   "ledger links back to the main site relatively")
            expect("bearjhartjen.github.io/Flank-ledger" not in flank_html,
                   "ledger has no links to the old standalone site")
            expect("const TX = [" in flank_html, "ledger embeds the transaction data")
        if "sitenav" in idx:
            nav_block = idx.split('<nav class="sitenav">')[1].split("</nav>")[0]
            expect("flank" not in nav_block,
                   "ledger stays out of the nav (unlisted page)")
        if not ok:
            raise SystemExit("check FAILED")
        print(f"check passed in {tmp}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The ledger generator's own checks run against the real ledger + logo.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_flank
    build_flank.check(data_path.parent / "The Flank.md", build_flank.DEFAULT_LOGO)


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

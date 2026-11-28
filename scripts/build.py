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
import datetime
import html
import json
import os
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import page_extras

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "site.json"
DEFAULT_PAGES = ROOT / "data" / "pages"
DEFAULT_LOGO = ROOT / "assets" / "logo.png"
DEFAULT_FAVICON = ROOT / "assets" / "favicon.png"
DEFAULT_TOUCH_ICON = ROOT / "assets" / "apple-touch-icon.png"
DEFAULT_UPLOADS = ROOT / "assets" / "uploads"
DEFAULT_OUT = ROOT / "site"

# Custom domain for the published site. Written to site/CNAME on every
# build so the domain survives rebuilds and gh-pages publishes.
CUSTOM_DOMAIN = "flamingoandcactus.com"

THEMES = ("classic", "parchment", "ink", "midnight")
# Star burst: the comic look is the only one this site ships. Earlier
# variants (Golden burst portal, Soft fade, No animation) were removed in
# October 2026 — the comic ink-and-paper starburst is the house style.
# site.json may carry the key but the build always renders comic.
STAR_ANIMATIONS = ("comic",)
STAR_ANIMATION_DEFAULT = "comic"
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
# What may be copied from assets/uploads/ to the live site. Pictures and
# PDFs only: anything else (scripts, HTML, a stray key) stays local.
UPLOAD_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")


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


GISCUS_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GISCUS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,100}$")
GISCUS_MAPPINGS = ("pathname", "url", "title", "og:title", "specific", "number")


def sanitize_giscus(doc) -> dict:
    """Keep only the public giscus settings the page can safely use.

    The repository ids and category id are public values (they appear in
    the script tag on every site that uses giscus). Anything malformed is
    dropped; the page then falls back to a plain GitHub Discussions link
    instead of shipping a broken embed.
    """
    if not isinstance(doc, dict):
        return {}
    repo = str(doc.get("repo", "")).strip()
    if not GISCUS_REPO_RE.match(repo):
        return {}
    out = {"repo": repo}
    for key in ("repo_id", "category_id"):
        value = str(doc.get(key, "")).strip()
        if GISCUS_ID_RE.match(value):
            out[key] = value
    category = str(doc.get("category", "General")).strip()[:80]
    if category:
        out["category"] = category
    mapping = str(doc.get("mapping", "number")).strip()
    out["mapping"] = mapping if mapping in GISCUS_MAPPINGS else "number"
    term = str(doc.get("term", "")).strip()
    if term.isdigit() and len(term) <= 12:
        out["term"] = term
    return out


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
        if "heading" in p:
            # "" (or false) means the page body carries its own h1; a string
            # overrides the page name used for the generated heading.
            entry["heading"] = str(p.get("heading") or "")[:80]
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
    raw_custom_css = str(doc.get("custom_css", ""))
    css_error = custom_css_error(raw_custom_css)
    if css_error:
        raise ValueError(css_error)
    custom_css = safe_custom_css(raw_custom_css)
    return {
        "title": str(doc.get("title", "Flamingo & Cactus"))[:300],
        "tagline": str(doc.get("tagline", ""))[:300],
        "footer": str(doc.get("footer", ""))[:500],
        "theme": theme if theme in THEMES else "classic",
        # star_animation is locked to comic. Read site.json's value for
        # diagnostic logging only -- the builder, the renderer, and the
        # live HTML all hard-code comic. A hand-edit can no longer reach.
        "star_animation": STAR_ANIMATION_DEFAULT,
        "description": str(doc.get("description", ""))[:300],
        "custom_css": custom_css,
        "giscus": sanitize_giscus(doc.get("giscus")),
        "pages": clean_pages,
    }


# ---------------------------------------------------------------------------
# Sanitizer: page bodies are freeform editor HTML, so only an allowlist of
# plain tags survives. editor and builder share this.
ALLOWED_TAGS = {"p", "h1", "h2", "h3", "br", "strong", "b", "em", "i", "u", "s",
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
# A section heading written as a real h1/h2/h3 keeps the serif look
# the page used when it was only a big paragraph.
SECTION_CLASSES = {"sec"}
# Block tags that keep the "ac"/"ar" alignment classes as well as size/colour.
CENTERED_TAGS = ("p", "h1", "h2", "h3", "blockquote", "li")

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


def _phone_big_class(style: str | None) -> str:
    """Return 't-big' (with a leading space) when the size needs a phone cap.

    Sizes the toolbar treats as giant titles (40px and up) get a class the
    CSS caps on small screens, so phone sizing comes from a real number in
    Python instead of attribute-substring pattern matching, which cannot
    tell 10px from 100px."""
    size = _clean_size(style)
    if not size:
        return ""
    try:
        if float(size[:-2]) >= 40:
            return " t-big"
    except ValueError:
        return ""
    return ""


# Geometry for the big button box: strict px lengths on a strict range, so
# dragging a box anywhere can never smuggle anything but a position through.
_LEN_PX_RE = re.compile(r"-?\d+(?:\.\d{1,2})?px", re.IGNORECASE)


def _clean_len_px(style: str | None, prop: str, lo: int, hi: int) -> str | None:
    """Return one canonical px declaration (prop:value) from a style attribute."""
    if not style:
        return None
    for decl in style.split(";"):
        p, sep, val = decl.partition(":")
        if not sep or p.strip().lower() != prop:
            continue
        v = re.sub(r"\s+", "", val)
        if any(bad in v.lower() for bad in (
                "url", "expression", "javascript", "<", ">", '"', "'", "{", "}")):
            continue
        if not _LEN_PX_RE.fullmatch(v):
            continue
        try:
            px = float(v[:-2])
        except ValueError:
            continue
        if not (lo <= px <= hi):
            continue
        if px == int(px):
            return f"{prop}:{int(px)}px"
        s2 = f"{px:.2f}".rstrip("0").rstrip(".")
        return f"{prop}:{s2}px"
    return None


def _style_attr_box(style: str | None) -> str:
    """Canonical style for a.bigbtn / a.btn: geometry first, then text properties.

    position may only be the literal `absolute` (dragging), margin only the
    literal `0px` (dragging cancels the auto-centering). Everything else is
    the same validated set as _style_attr, in the same order after geometry.
    """
    parts = []
    decls = [d.strip().lower() for d in (style or "").split(";")]
    for d in decls:
        if d == "position:absolute":
            parts.append("position:absolute")
        elif d in ("margin:0px", "margin:0"):
            parts.append("margin:0px")
    for prop, lo, hi in (("left", -100, 5000), ("top", -100, 5000),
                         ("width", 40, 2000), ("height", 40, 2000)):
        g = _clean_len_px(style, prop, lo, hi)
        if g:
            parts.append(g)
    text = _style_attr(style)
    if text:
        parts.append(text[len(' style="'):-1])
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
    # Absolute paths: safe page paths and uploaded files (no empty segments, no "//").
    if h.startswith("/"):
        if h.startswith("//"):
            return None
        segs = h.lstrip("/").split("/")
        if h == "/" or (all(segs) and all(re.fullmatch(r"[A-Za-z0-9_.-]+", s) for s in segs)
                        and segs[-1].lower().endswith((".html", ".pdf"))):
            return h
        return None
    # Relative page links (index.html), subpage links (flank/index.html), and
    # uploaded files (uploads/foo.pdf).
    segs = h.split("/")
    if (all(segs) and all(re.fullmatch(r"[A-Za-z0-9_.-]+", s) for s in segs)
            and segs[-1].lower().endswith((".html", ".pdf"))):
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
            # galleries, cards and the hidden events source survive as divs;
            # everything else is a paragraph
            classes = ad.get("class", "").split()
            if "gallery" in classes:
                pass
            elif "card" in classes:
                ad["class"] = "card"
            elif "evsrc" in classes:
                ad["class"] = "evsrc"
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
            rawCls = [c for c in ad.get("class", "").split() if c in ("btn", "bigbtn")]
            cls = "bigbtn" if "bigbtn" in rawCls else ("btn" if rawCls else "")
            cls_attr = f' class="{cls}"' if cls else ""
            style_attr = _style_attr_box(ad.get("style", "")) if cls in ("btn", "bigbtn") else ""
            # new tabs: only a literal _blank target survives, always with noopener
            tgt_attr = ' target="_blank"' if ad.get("target", "") == "_blank" else ""
            rel_attr = ' rel="noopener"' if tgt_attr else ""
            self.out.append(f'<a href="{html.escape(href, quote=True)}"{cls_attr}{tgt_attr}{rel_attr}{style_attr}>')
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
            cls = ad.get("class", "")
            if cls == "card":
                self.out.append('<div class="card">')
            elif cls == "evsrc":
                self.out.append('<div class="evsrc">')
            else:
                self.out.append('<div class="gallery">')
        elif tag == "span":
            # inline size/colour wrapper. A span with nothing left to keep
            # must not emit an empty tag: drop the tag, keep the children.
            keep = []
            for c in ad.get("class", "").split():
                if c in TEXT_CLASSES and c not in keep:
                    keep.append(c)
            big = _phone_big_class(ad.get("style", ""))
            if big and big.strip() not in keep:
                keep.append(big.strip())
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
                    if c in TEXT_CLASSES | SECTION_CLASSES and c not in keep:
                        keep.append(c)
                big = _phone_big_class(ad.get("style", ""))
                if big and big.strip() not in keep:
                    keep.append(big.strip())
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
    --paper2:#f4f4f4;
    --ink:#141414;
    --ink2:#3d3d3d;
    --muted:#6b6b6b;
    --faint:#8c8c8c;
    --rule:#d9d9d9;
    --rule2:#ececec;
    --link:#0b2a8c;
    --gold:#c9a227;
  }
  body[data-theme="classic"] .contactform{background:#ffffff}
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
    --paper:#262220;
    --paper2:#2f2b28;
    --ink:#f5efe0;
    --ink2:#ddd2c0;
    --muted:#b8ac92;
    --faint:#928872;
    --rule:#6b6152;
    --rule2:#544b3e;
    --link:#9db8ff;
    --gold:#d29922;
  }
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
    --gold:#e3b83b;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;-webkit-text-size-adjust:100%;text-size-adjust:100%}
  body{
    background:var(--paper);
    color:var(--ink);
    font:16px/1.6 Georgia,"Times New Roman",Times,serif;
    -webkit-font-smoothing:antialiased;
  }
  .page{max-width:860px;margin:0 auto;padding:26px 28px 64px}
  .masthead{text-align:center;padding:6px 0 4px}
  .masthead img{display:block;margin:0 auto;width:96px;height:96px;object-fit:contain}
  h1,.masthead .sitetitle{
    font-size:34px;font-weight:400;letter-spacing:2.5px;text-transform:uppercase;
    margin:14px 0 2px;
  }
  .tagline{color:var(--muted);font-size:14px;font-style:italic;margin:0 0 14px}
  nav.sitenav{
    display:flex;flex-wrap:wrap;justify-content:center;align-items:center;
    gap:5px 0;
    margin:0 0 18px;
    font:11.5px "Helvetica Neue",Arial,sans-serif;letter-spacing:.5px;text-transform:uppercase;
  }
  nav.sitenav a,nav.sitenav .cur{margin:0 6px;white-space:nowrap;padding:3px 0}
  nav.sitenav a{color:var(--muted);text-decoration:none}
  nav.sitenav a:hover{color:var(--ink);text-decoration:underline}
  nav.sitenav .cur{color:var(--ink);border-bottom:2px solid var(--ink);padding-bottom:1px}
  .hr{height:2px;background:var(--ink);margin:0}
  .hr.thin{height:1px;background:var(--rule)}
  .canvas{position:relative;color:var(--ink2);font-size:15.5px;line-height:1.65;padding:10px 0 0}
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
  .canvas h1{
    font:400 30px/1.25 Georgia,"Times New Roman",Times,serif;
    letter-spacing:.3px;text-transform:none;color:var(--ink);margin:0 0 16px;
  }
  .canvas h1.sec,.canvas h2.sec,.canvas h3.sec{
    font-family:Georgia,"Times New Roman",Times,serif;font-weight:400;
    font-size:22px;letter-spacing:0;text-transform:none;color:var(--ink);
    margin:26px 0 8px;
  }
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
  /* ---------- PCBWay order button (Products): sunset block. Selector keys
     on the share-project href so no inline gradient is needed (the
     sanitizer only keeps solid background-color). Flows centered inside
     p.ac on desktop and phone; no absolute positioning. ---------- */
  .canvas a.btn[href*="pcbway.com/project/shareproject/"]{
    border:none;border-radius:99px;background:linear-gradient(135deg,#ff5e78,#ff9a3c 60%,#c9a227);
    color:#1a0005;padding:14px 36px;
    font:800 14px "Helvetica Neue",Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;
    box-shadow:0 8px 24px rgba(255,94,120,.35);
    transition:transform .15s ease,box-shadow .15s ease;
  }
  .canvas a.btn[href*="pcbway.com/project/shareproject/"]:hover{transform:translateY(-2px) scale(1.02);box-shadow:0 12px 32px rgba(255,94,120,.45)}
  /* the big draggable button box: a huge rounded clickable block.
     Colors, size and (when dragged) position come from a validated inline
     style; without one it flows centered like a normal block. */
  .canvas a.bigbtn{
    display:flex;align-items:center;justify-content:center;
    width:fit-content;max-width:100%;min-width:280px;min-height:84px;
    margin:28px auto;padding:26px 60px;
    border-radius:18px;background:#1c4fd8;color:#fff;
    text-align:center;text-decoration:none;
    font:700 26px "Helvetica Neue",Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;
    box-shadow:0 10px 24px rgba(0,0,0,.18);
    transition:transform .15s ease;
  }
  .canvas a.bigbtn:hover{transform:scale(1.03)}
  .canvas img.secimg{display:block;height:auto;margin:12px 0;border:1px solid var(--rule2)}
  .canvas img.small{width:220px}
  .canvas img.medium{width:420px}
  .canvas img.full{width:100%}
  .canvas img{max-width:100%}
  .canvas .card{
    border:1px solid var(--rule);border-radius:6px;background:var(--paper2);
    padding:18px 20px;margin:0 0 16px;
  }
  .canvas p.ev-hit,.canvas li.ev-hit{outline:2px solid var(--ink);outline-offset:3px}
  body[data-theme="ink"] .canvas .card{background:#332e28;border-color:#6b6152}
  body[data-theme="midnight"] .canvas .card{background:#2b2336;border-color:#4d3c6b}
  .canvas .card h2:first-child{margin-top:0}
  .canvas .card p:last-child{margin-bottom:0}
  .canvas .gallery{display:flex;gap:10px;margin:0 0 12px}

  /* ---------- Live GitHub repository strip (Products + Home) ----------
     A quiet line of metadata under a product, not a dashboard. The gold
     left rule is the only decoration: it marks the strip as part of the
     open-source story without competing with the board render. */
  .canvas .gh-repo{
    display:grid;grid-template-columns:minmax(250px,1fr) minmax(0,2.4fr) auto;
    align-items:center;gap:10px 18px;
    margin:16px 0 26px;padding:11px 14px;
    border:1px solid var(--rule);border-left:3px solid var(--gold);
    border-radius:6px;background:var(--paper2);
    color:var(--ink2);font-family:"Helvetica Neue",Arial,sans-serif;
  }
  .canvas .gh-id{display:flex;align-items:center;gap:10px;min-width:0}
  .canvas .gh-mark{flex:0 0 auto;color:var(--ink)}
  .canvas .gh-idtext{min-width:0}
  .canvas .gh-kicker{
    display:block;font:600 9.5px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);
  }
  .canvas .gh-name{
    display:block;font:600 13.5px "Helvetica Neue",Arial,sans-serif;
    color:var(--ink);text-decoration:none;overflow-wrap:anywhere;
  }
  .canvas .gh-name:hover{text-decoration:underline}
  .canvas .gh-live{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px;min-width:0}
  .canvas .gh-stat{
    display:inline-flex;align-items:baseline;gap:5px;min-width:0;
    font:12px "Helvetica Neue",Arial,sans-serif;color:var(--muted);text-decoration:none;
  }
  .canvas a.gh-stat:hover{color:var(--ink)}
  .canvas .gh-stat b{
    font:600 13px "Helvetica Neue",Arial,sans-serif;color:var(--ink2);
    font-variant-numeric:tabular-nums;
  }
  .canvas .gh-stat .gh-k{
    display:block;font:600 9px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:1.1px;text-transform:uppercase;color:var(--faint);
  }
  .canvas .gh-stat-wide{display:flex;flex-direction:column;align-items:flex-start;gap:1px;flex:1 1 110px;min-width:100px;max-width:none}
  .canvas .gh-commit,.canvas .gh-release{
    display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    font-weight:600;color:var(--ink2);
  }
  .canvas .gh-release{white-space:normal;overflow-wrap:anywhere}
  .canvas .gh-when{font:10.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint)}
  .canvas .gh-link{
    white-space:nowrap;font:600 10.5px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:.7px;text-transform:uppercase;text-decoration:none;
    color:var(--ink);border-bottom:1px solid var(--gold);padding-bottom:2px;
  }
  .canvas .gh-link:hover{color:var(--link);border-color:var(--link)}
  .canvas .gh-actions{display:flex;flex-direction:column;align-items:flex-end;gap:5px}
  .canvas .gh-actions .gh-talk{color:var(--ink);border-color:var(--gold)}
  .canvas .gh-actions .gh-talk:hover{color:var(--link);border-color:var(--link)}
  .canvas .gh-repo[data-state="loading"] .gh-commit,
  .canvas .gh-repo[data-state="loading"] .gh-release{color:var(--faint);font-weight:400}
  .canvas .gh-repo[data-state="error"] .gh-commit,
  .canvas .gh-repo[data-state="error"] .gh-release{color:var(--muted);font-weight:400}
  @media (max-width:760px){
    .canvas .gh-repo{grid-template-columns:1fr;gap:10px}
    .canvas .gh-live{gap:8px 16px}
    .canvas .gh-stat-wide{flex:1 1 100%;max-width:none}
    .canvas .gh-actions{flex-direction:row;align-items:center;gap:14px}
  }

  /* ---------- Financials dashboard (Chart.js) ----------
     The data is embedded by the build; this is only the frame. Keep the
     canvases on the paper colour so the chart gridlines and tooltips can
     follow all four themes. */
  .canvas .fin{margin:30px 0 0}
  .canvas .fin-head p{margin:0 0 14px;color:var(--muted);font-size:14px}
  .canvas .fin-controls{
    display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;
    margin:0 0 14px;padding:10px 12px;
    border:1px solid var(--rule);border-radius:6px;background:var(--paper2);
  }
  .canvas .fin-controls label{
    font:600 10px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.4px;
    text-transform:uppercase;color:var(--muted);
  }
  .canvas .fin-controls select{
    max-width:100%;padding:6px 10px;border:1px solid var(--rule);border-radius:4px;
    background:var(--paper);color:var(--ink);font:16px Georgia,"Times New Roman",serif;
  }
  .canvas .fin-range{
    margin-left:auto;font:10.5px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:.4px;color:var(--faint);
  }
  .canvas .fin-tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:0 0 16px}
  .canvas .fin-tile{
    min-width:0;padding:9px 11px;border:1px solid var(--rule);border-radius:6px;
    background:var(--paper);
  }
  .canvas .fin-tile .k{
    display:block;font:600 9px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);
  }
  .canvas .fin-tile .v{
    display:block;margin-top:2px;font:600 19px/1.2 Georgia,"Times New Roman",serif;
    color:var(--ink);font-variant-numeric:tabular-nums;overflow-wrap:anywhere;
  }
  .canvas .fin-tile .v.up{color:#1c6b3a}
  .canvas .fin-tile .v.down{color:#9c1f16}
  body[data-theme="ink"] .canvas .fin-tile .v.up,
  body[data-theme="midnight"] .canvas .fin-tile .v.up{color:#7ee2a8}
  body[data-theme="ink"] .canvas .fin-tile .v.down,
  body[data-theme="midnight"] .canvas .fin-tile .v.down{color:#ff9d9d}
  .canvas .fin-chartcard{
    margin:0 0 14px;padding:13px 15px 9px;
    border:1px solid var(--rule);border-radius:6px;background:var(--paper2);
  }
  .canvas .fin-charthead{
    display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
    gap:3px 12px;margin:0 0 8px;
  }
  .canvas .fin-charthead h3{
    margin:0;font:600 11px "Helvetica Neue",Arial,sans-serif;
    letter-spacing:1.3px;text-transform:uppercase;color:var(--ink2);
  }
  .canvas .fin-charthead span{font:10.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint)}
  .canvas .fin-canvas{position:relative;height:260px}
  .canvas .fin-note{font:12.5px/1.55 Georgia,"Times New Roman",serif;color:var(--muted)}
  .canvas .fin-cta{margin:18px 0 4px}
  @media (max-width:640px){
    .canvas .fin-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
    .canvas .fin-canvas{height:220px}
    .canvas .fin-range{margin-left:0;flex-basis:100%}
    .canvas .fin-tile .v{font-size:17px}
  }
  @media (max-width:380px){
    .canvas .fin-tiles{grid-template-columns:1fr}
  }

  /* ---------- Giscus-ting forum ---------- */
  .canvas .giscus-wrap{
    margin:18px 0 0;padding:8px;border:1px solid var(--rule);
    border-radius:6px;background:var(--paper2);min-height:180px;
  }
  .canvas .giscus-wrap.is-fallback{min-height:0}
  .canvas .giscus-wrap.is-fallback .giscus{display:none}
  .canvas .giscus{min-height:160px}
  .canvas .giscus-loading{
    margin:0;padding:58px 12px;text-align:center;
    font:italic 13px Georgia,"Times New Roman",serif;color:var(--muted);
  }
  .canvas .giscus-wrap.is-ready .giscus-loading,
  .canvas .giscus-wrap.is-fallback .giscus-loading{display:none}
  .canvas .giscus-wrap.is-ready .giscus[hidden]{display:block}
  .canvas .giscus-frame{width:100%;border:0}
  .canvas .giscus-fallback{
    margin:0;padding:14px;text-align:center;
    font:13px/1.55 Georgia,"Times New Roman",serif;color:var(--muted);
  }
  .canvas .giscus-fallback[hidden]{display:none}
  .canvas .giscus-note{font-size:12.5px;color:var(--muted)}

  /* Header badges — comic book, black and white, level with each other,
     both kept the same distance from the centred logo (438px either side).
     The blue stays only on the feedback page's Contact us button. */
  .star-badge{
    position:absolute;top:64px;left:calc(50% - 606px);z-index:200;width:168px;height:168px;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    color:#111;text-decoration:none;text-align:center;
    /* the hard offset shadow rides the whole silhouette; the ::before/::after
       pseudo pair draws the black star with a white star inset inside it,
       since clip-path would otherwise cut any painted outline */
    filter:drop-shadow(7px 9px 0 rgba(0,0,0,.9));
    transition:transform .15s ease;
  }
  .star-badge::before,.star-badge::after{content:"";position:absolute;clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%)}
  .star-badge::before{inset:0;background:#111}
  .star-badge::after{inset:3px;background:#fffdf7}
  .star-badge .star-title,.star-badge .star-sub{position:relative;z-index:1}
  .star-badge:hover{transform:scale(1.06) rotate(-6deg)}
  .star-badge .star-title{display:block;max-width:70%;font:800 12px/1.25 "Arial Black","Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;text-transform:uppercase}
  .star-badge .star-sub{display:block;margin-top:2px;font:700 8px "Helvetica Neue",Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;animation:starpulse 3s ease-in-out infinite}
  .star-badge .star-title,.star-badge .star-sub{transform:translateY(-5px)}
  @keyframes starpulse{0%,100%{opacity:.45}50%{opacity:.9}}
  @media (max-width:1250px) and (min-width:701px){
    .star-badge{top:20px;left:56px;width:140px;height:140px}
    .star-badge .star-title{font-size:9px}
    .star-badge .star-sub{font-size:7px}
    /* pad the masthead so the badges never sit on the logo or title */
    body.has-badges .masthead{padding-top:150px}
  }
  @media print{.star-badge,.gem-badge{display:none}}
  .star-badge{cursor:pointer}
  .gem-badge{
    position:absolute;top:94px;left:calc(50% + 438px);z-index:200;width:170px;height:110px;
    display:flex;align-items:center;justify-content:center;
    background:#fffdf7;
    color:#111;text-decoration:none;text-align:center;
    border:3px solid #111;border-radius:14px;
    box-shadow:7px 8px 0 #111;
    transform:rotate(1.5deg);
    transition:transform .15s ease;
  }
  .gem-badge:hover{transform:rotate(-1deg) scale(1.05)}
  .gem-badge .gem-title{display:block;max-width:84%;font:800 14px/1.3 "Arial Black","Helvetica Neue",Arial,sans-serif;letter-spacing:2.5px;text-transform:uppercase}
  @media (max-width:1250px) and (min-width:701px){
    .gem-badge{top:52px;left:auto;right:56px;width:150px;height:76px;border-radius:11px;box-shadow:5px 6px 0 #111}
    .gem-badge .gem-title{font-size:11px}
  }
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
  /* Black-and-ink impact frame on the burst - the only star animation
     this site ships. The canvas script draws jagged rays and shards. */
  #fx .comicflash{position:absolute;inset:0;background:#111;opacity:0;animation:comicko .45s ease-out}
  @keyframes comicko{0%{opacity:0}18%{opacity:.92}38%{opacity:.15}55%{opacity:.6}100%{opacity:0}}
  #fx canvas{position:absolute;inset:0;width:100%;height:100%}
  body.arrive .page{animation:arrivefade .55s cubic-bezier(.2,.9,.3,1) both}
  @keyframes arrivefade{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  /* Arrival flash: dark on the default themes, paper-bright on ink and
     midnight so the same flash keyframes always read. */
  #arriveflash{position:fixed;inset:0;z-index:1000;pointer-events:none;background:#111;opacity:0;animation:flashfadeink .5s ease-out}
  @keyframes flashfadeink{0%{opacity:0}28%{opacity:.85}100%{opacity:0}}
  body[data-theme="midnight"] #arriveflash,body[data-theme="ink"] #arriveflash{background:#fffdf7}
  .canvas .gallery img{flex:1;min-width:0;object-fit:cover;margin:0;border:1px solid var(--rule2)}
  .canvas::after{content:"";display:table;clear:both}
  .canvas table{border-collapse:collapse;margin:0 0 12px;max-width:100%}
  .canvas th,.canvas td{border:1px solid var(--rule);padding:7px 10px;text-align:left;font-size:14.5px}
  .canvas th{background:var(--paper2);font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;color:var(--ink2)}
  .canvas img.ileft{float:left;margin:4px 14px 8px 0}
  .canvas img.iright{float:right;margin:4px 0 8px 14px}
  .canvas img.icenter{display:block;margin-left:auto;margin-right:auto}
  .masthead a.logolink{display:inline-block;text-decoration:none;transition:opacity .15s ease}
  .masthead a.logolink:hover{opacity:.78}
  .contactform{max-width:560px;margin:26px auto 0;padding:20px 22px;border:1px solid var(--rule);border-radius:4px;background:#ecdcc0}
  .contactform label{display:block;font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink2);margin:14px 0 5px}
  .contactform label:first-of-type{margin-top:0}
  .contactform .sub{display:block;font:italic 12px Georgia,serif;letter-spacing:0;text-transform:none;color:var(--muted);margin-top:3px}
  .contactform input[type=text],.contactform input[type=email],.contactform input[type=tel],.contactform textarea{display:block;width:100%;box-sizing:border-box;font:16px Georgia,serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px;padding:9px 10px;margin:0}
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
  .contactform button:active{transform:translateY(1px)}
  .contactform button:disabled{opacity:.55;cursor:progress}
  /* ---- polish pass (Oct 2026) ---- */
  :focus-visible{outline:2px solid var(--link);outline-offset:2px}
  .contactform input:focus-visible,.contactform textarea:focus-visible{outline-offset:-1px}
  html{scroll-behavior:smooth}
  @media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important}}
  ::selection{background:var(--gold);color:#141414}
  .contactform input[type=text],.contactform textarea{transition:border-color .15s}
  .contactform input[type=text]:hover,.contactform textarea:hover{border-color:var(--faint)}
  .cf-disclaimer{margin-bottom:6px}
  .page hr{border:0;border-top:1px solid var(--rule);margin:34px 0 22px}
  @media (max-width:520px){.contactform button[type=submit]{width:100%;padding:13px 18px}}
  /* status line: success banner + the fallback when a submit hangs */
  .cf-status{display:block;padding:10px 12px;border:1px solid var(--rule);border-radius:2px;background:var(--paper);font:italic 13px/1.5 Georgia,Times,serif;color:var(--ink2);margin:10px 0 0}
  .cf-status[hidden]{display:none}
  .cf-status a{color:var(--link)}
  .cf-status.is-err{border-color:#b3261e;color:#b3261e}
  body[data-theme="ink"] .cf-status.is-err{border-color:#ff8a80;color:#ff8a80}
  body[data-theme="midnight"] .cf-status.is-err{border-color:#ff9d9d;color:#ff9d9d}
  /* while a send is in flight: a moving bar under the status text, and the
     file controls locked so the list cannot change mid-upload */
  .cf-status.is-busy{position:relative;overflow:hidden;padding-bottom:15px}
  .cf-status.is-busy::after{content:"";position:absolute;left:-40%;bottom:0;height:3px;width:40%;background:var(--link);animation:cfbar 1.2s ease-in-out infinite}
  @keyframes cfbar{0%{left:-40%}100%{left:100%}}
  @media (prefers-reduced-motion:reduce){.cf-status.is-busy::after{animation:none;left:0;width:100%;opacity:.45}}
  .contactform.is-sending .dropzone,.contactform.is-sending .filelist button{pointer-events:none;opacity:.55}
  /* Disclaimer card sitting just under the contact form. Carries who
     runs the form (one third-party service), what happens when a send
     is not confirmed (no automatic retry; email directly), and a peer
     email link so visitors always have a way through. Same border/paper
     rhythm as the form for unity. */
  .cf-disclaimer{max-width:560px;box-sizing:border-box;margin:22px auto 0;padding:14px 18px;border:1px solid var(--rule);border-left:3px solid var(--link);border-radius:2px;background:var(--paper2);color:var(--ink2);font:13px/1.55 Georgia,Times,serif}
  .cf-disclaimer-h{font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink);margin:0 0 8px}
  .cf-disclaimer-p{margin:0 0 6px}
  .cf-disclaimer-p:last-child{margin-bottom:0}
  .cf-svc{display:inline-block;font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;background:var(--paper);border:1px solid var(--rule);border-radius:2px;padding:2px 7px;margin:0 4px 4px 0}
  .cf-svc-or{font:italic 12px Georgia,Times,serif;color:var(--muted);margin:0 4px 4px 0}
  .cf-svc-sep+.cf-svc{margin-left:0}
  body[data-theme="ink"] .cf-disclaimer{background:#332e28;border-color:#857763;border-left-color:#9db8ff}
  body[data-theme="midnight"] .cf-disclaimer{background:#2b2336;border-color:#4d3c6b;border-left-color:#9db8ff}
  body[data-theme="ink"] .cf-svc,body[data-theme="midnight"] .cf-svc{background:transparent;border-color:#857763}
  /* the big yes/no panels: on success the form is replaced by the panel, so
     there is no way to miss that the note went through (or did not) */
  .contactform[hidden],.cf-done[hidden],.cf-error[hidden]{display:none}
  .cf-done,.cf-error{max-width:560px;box-sizing:border-box;margin:26px auto 0;padding:22px;border-radius:4px;text-align:left}
  .cf-done{border:2px solid #1a7f37;background:#eaf4ec}
  .cf-error{border:2px solid #b3261e;background:#fbeeed}
  .cf-done-head{font:700 22px Georgia,Times,serif;color:#1a7f37;margin:0 0 8px}
  .cf-done-sub{font:14px/1.55 Georgia,Times,serif;color:var(--ink2);margin:0}
  .cf-done-stamp{font:italic 12px Georgia,Times,serif;color:var(--muted);margin:8px 0 0}
  .cf-again{margin-top:16px;padding:10px 26px;font:13px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;background:var(--ink);color:var(--paper);border:1px solid var(--ink);border-radius:2px;cursor:pointer}
  .cf-again:hover{opacity:.85}
  .cf-error-head{font:700 18px Georgia,Times,serif;color:#b3261e;margin:0 0 8px}
  .cf-error-body{font:14px/1.55 Georgia,Times,serif;color:var(--ink2);margin:0 0 6px}
  .cf-error-body a{color:var(--link)}
  body[data-theme="ink"] .cf-done{border-color:#2f6f4a;background:#243328}
  body[data-theme="ink"] .cf-done-head{color:#7ee2a8}
  body[data-theme="ink"] .cf-done-sub,body[data-theme="ink"] .cf-done-stamp,body[data-theme="ink"] .cf-error-body{color:#ddd2c0}
  body[data-theme="ink"] .cf-error{border-color:#8a4a44;background:#3a2624}
  body[data-theme="ink"] .cf-error-head{color:#ff8a80}
  body[data-theme="midnight"] .cf-done{border-color:#3f7f57;background:#28362e}
  body[data-theme="midnight"] .cf-done-head{color:#7ee2a8}
  body[data-theme="midnight"] .cf-done-sub,body[data-theme="midnight"] .cf-done-stamp,body[data-theme="midnight"] .cf-error-body{color:#d9cff0}
  body[data-theme="midnight"] .cf-error{border-color:#8a4a4a;background:#3a262c}
  body[data-theme="midnight"] .cf-error-head{color:#ff9d9d}
  /* dark themes: the form fields need to be plainly visible, not black on black */
  body[data-theme="ink"] .contactform{background:#332e28;border-color:#6b6152}
  body[data-theme="ink"] .contactform input[type=text],body[data-theme="ink"] .contactform input[type=email],body[data-theme="ink"] .contactform input[type=tel],body[data-theme="ink"] .contactform textarea{background:#3f3830;border-color:#857763;color:#f5efe0}
  body[data-theme="ink"] .contactform .dropzone{background:#3f3830;border-color:#857763;color:#ddd2c0}
  body[data-theme="midnight"] .contactform{background:#2b2336;border-color:#4d3c6b}
  body[data-theme="midnight"] .contactform input[type=text],body[data-theme="midnight"] .contactform input[type=email],body[data-theme="midnight"] .contactform input[type=tel],body[data-theme="midnight"] .contactform textarea{background:#383046;border-color:#9a87c0;color:#f0e9f7}
  body[data-theme="midnight"] .contactform .dropzone{background:#383046;border-color:#9a87c0;color:#d9cff0}
  /* ---------- events page: month calendar woven from the list ---------- */
  /* hidden until the calendar script takes over (no-JS keeps the page clean) */
  .canvas .evsrc{display:none}
  .evcal{max-width:680px;margin:22px auto 12px;border:1px solid var(--rule);border-radius:10px;background:var(--paper);padding:20px 20px 14px;box-shadow:0 4px 20px rgba(0,0,0,.04);display:none}
  body[data-theme="ink"] .evcal{background:var(--paper);border-color:var(--rule)}
  body[data-theme="midnight"] .evcal{background:var(--paper);border-color:var(--rule)}
  .evcal-nav-tabs{display:flex;gap:8px;justify-content:center;margin-bottom:18px;border-bottom:1px solid var(--rule2);padding-bottom:12px}
  .evcal-nav-tabs button{padding:7px 18px;font:600 13px "Helvetica Neue",Arial,sans-serif;color:var(--muted);background:transparent;border:1px solid transparent;border-radius:6px;cursor:pointer;transition:all .15s ease}
  .evcal-nav-tabs button:hover{color:var(--ink);background:rgba(0,0,0,.03)}
  .evcal-nav-tabs button.active{color:var(--ink);background:var(--paper);border-color:var(--rule);box-shadow:0 1px 4px rgba(0,0,0,.06)}
  body[data-theme="ink"] .evcal-nav-tabs button.active,body[data-theme="midnight"] .evcal-nav-tabs button.active{background:var(--paper2)}
  .ev-cards-list{display:flex;flex-direction:column;gap:14px;margin:12px 0}
  .ev-card{display:flex;gap:16px;align-items:flex-start;padding:16px 18px;background:var(--paper);border:1px solid var(--rule2);border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.03);text-align:left;transition:transform .15s ease,box-shadow .15s ease}
  body[data-theme="ink"] .ev-card,body[data-theme="midnight"] .ev-card{background:var(--paper2);border-color:var(--rule)}
  .ev-card:hover{box-shadow:0 4px 16px rgba(0,0,0,.07);transform:translateY(-1px)}
  .ev-date-badge{flex:0 0 70px;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#2b241d;color:#fbf7ee;border-radius:6px;padding:8px 4px;text-align:center}
  body[data-theme="ink"] .ev-date-badge,body[data-theme="midnight"] .ev-date-badge{background:var(--paper2);border:1px solid var(--rule);color:var(--ink)}
  .ev-date-badge .ev-badge-month{font:700 11px "Helvetica Neue",Arial,sans-serif;letter-spacing:1.2px;text-transform:uppercase;color:#e2b84b}
  .ev-date-badge .ev-badge-day{font:800 24px Georgia,serif;line-height:1.1;margin:2px 0}
  .ev-date-badge .ev-badge-wd{font:600 10px "Helvetica Neue",Arial,sans-serif;opacity:.85;letter-spacing:.5px}
  .ev-card-content{flex:1;min-width:0}
  .ev-card-title{margin:0 0 6px;font:700 17px "Helvetica Neue",Arial,sans-serif;color:var(--ink);line-height:1.3}
  .ev-card-tags{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
  .ev-card-tag{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;background:rgba(0,0,0,.04);border-radius:4px;font:600 11.5px "Helvetica Neue",Arial,sans-serif;color:var(--ink2)}
  body[data-theme="ink"] .ev-card-tag,body[data-theme="midnight"] .ev-card-tag{background:rgba(255,255,255,.07);color:var(--ink)}
  .ev-card-desc{margin:0 0 10px;font:14px/1.45 Georgia,Times,serif;color:var(--ink2)}
  .ev-card-actions{margin-top:6px}
  .canvas a.ev-card-btn{display:inline-flex;align-items:center;gap:8px;padding:11px 22px;margin-top:10px;background:var(--ink);color:var(--paper)!important;border:2px solid var(--ink);border-radius:6px;text-decoration:none;font:700 14px/1.2 "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;box-shadow:0 2px 6px rgba(0,0,0,.15)}
  .canvas a.ev-card-btn:hover{opacity:.9;text-decoration:none;color:var(--paper)!important}
  .canvas a.ev-card-btn:visited{color:var(--paper)!important}
  .canvas a.ev-card-btn:focus-visible{outline:3px solid var(--gold);outline-offset:2px}
  .ev-empty-card{text-align:center;padding:32px 18px;border:1px dashed var(--rule);border-radius:8px;background:rgba(0,0,0,.015);margin:16px 0}
  .ev-empty-card h3{font:700 16px "Helvetica Neue",Arial,sans-serif;color:var(--ink);margin:0 0 6px}
  .ev-empty-card p{font:14px Georgia,serif;color:var(--muted);margin:0}
  .ev-past-drawer{margin:24px 0 8px;border-top:1px solid var(--rule);padding-top:14px;text-align:left}
  .ev-past-drawer summary{cursor:pointer;font:600 12.5px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin-bottom:10px}
  @media (max-width:560px){
    .ev-card{flex-direction:column;gap:12px;padding:12px 14px}
    .ev-date-badge{flex-direction:row;gap:8px;width:100%;padding:5px 10px;justify-content:flex-start}
    .ev-date-badge .ev-badge-day{font-size:18px}
  }
  .evcal .evcal-bar{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;justify-content:center}
  .evcal .evcal-bar button{min-height:40px;min-width:40px;padding:6px 10px;font:700 16px "Helvetica Neue",Arial,sans-serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px;cursor:pointer}
  .evcal .evcal-bar select{min-height:40px;padding:6px 8px;font:600 16px "Helvetica Neue",Arial,sans-serif;letter-spacing:.5px;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px}
  body[data-theme="ink"] .evcal .evcal-bar button,body[data-theme="ink"] .evcal .evcal-bar select{background:#312a22;border-color:#7a6e58;color:#f0e9da}
  body[data-theme="midnight"] .evcal .evcal-bar button,body[data-theme="midnight"] .evcal .evcal-bar select{background:#2b2140;border-color:#8471a6;color:#ece5f7}
  .evcal .ev-search{flex:1 1 200px;max-width:260px;min-height:40px;padding:8px 10px;font:16px Georgia,Times,serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px}
  body[data-theme="ink"] .evcal .ev-search{background:#312a22;border-color:#7a6e58;color:#f0e9da}
  body[data-theme="midnight"] .evcal .ev-search{background:#2b2140;border-color:#8471a6;color:#ece5f7}
  .evcal .evcal-bar button:hover{opacity:.85}
  .evcal .evcal-head{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin:12px 0 2px;font:600 10px "Helvetica Neue",Arial,sans-serif;letter-spacing:1px;text-transform:uppercase;color:var(--faint);text-align:center}
  .evcal .evcal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
  .evcal .ev-day{position:relative;min-height:52px;padding:4px 2px 2px;font:13px Georgia,Times,serif;color:var(--ink2);background:var(--paper);border:1px solid var(--rule2);border-radius:2px;cursor:default;text-align:left}
  body[data-theme="ink"] .evcal .ev-day,body[data-theme="midnight"] .evcal .ev-day{background:var(--paper2)}
  body[data-theme="ink"] .evcal .ev-day.empty,body[data-theme="midnight"] .evcal .ev-day.empty{background:transparent}
  body[data-theme="ink"] .evcal .ev-day .ev-dot,body[data-theme="midnight"] .evcal .ev-day .ev-dot{color:var(--ev-mark,#7ee2a8)}
  .evcal .ev-day.empty{background:transparent;border-color:transparent}
  .evcal .ev-day.today{box-shadow:inset 0 0 0 2px var(--ink)}
  .evcal .ev-day.ev-set{border-color:var(--ink)}
  .evcal .ev-day.ev-set:hover{transform:scale(1.06);z-index:2}
  .evcal .ev-day .ev-dot{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
    font-size:var(--ev-mark-size,17px);line-height:1;color:var(--ev-mark,#1a7f37);background:none;border-radius:0}
  .evcal .ev-day.ev-open{box-shadow:inset 0 0 0 2px var(--ink)}
  .evcal .evcal-detail,.evcal .ev-detail{margin:8px 0 4px;padding:10px 12px;border:1px solid var(--rule);
    border-radius:4px;background:var(--paper);text-align:left}
  body[data-theme="ink"] .evcal .ev-detail,body[data-theme="midnight"] .evcal .ev-detail{background:var(--paper2)}
  .evcal .ev-detail .ev-detail-head{margin:0 0 6px;font:700 13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.4px;color:var(--ink)}
  .evcal .ev-detail .ev-detail-item{margin:0 0 6px;font:13.5px Georgia,Times,serif;color:var(--ink2)}
  .evcal .ev-detail .ev-detail-item:last-child{margin-bottom:0}
  .evcal .ev-detail .ev-detail-time{font:700 12px "Helvetica Neue",Arial,sans-serif;color:var(--ev-mark,#1a7f37);margin-right:6px}
  .evcal .ev-day .ev-tag{display:none}
  .evcal .evcal-legend{display:flex;flex-wrap:wrap;gap:4px 12px;justify-content:center;margin:8px 0 4px;font:italic 12px Georgia,Times,serif;color:var(--muted)}
  .evcal .evcal-legend .ev-mi{padding:2px 8px;border-radius:2px;background:var(--paper);border:1px solid var(--rule2);font-style:normal;font-size:11.5px;color:var(--ink2);text-align:left;display:inline-block}
  body[data-theme="ink"] .evcal .evcal-legend .ev-mi,body[data-theme="midnight"] .evcal .evcal-legend .ev-mi{background:var(--paper2)}
  .evcal .evcal-legend b{font-weight:700;font-style:normal}
  .evcal .evcal-empty{margin:8px 0 4px;font:italic 13px Georgia,Times,serif;color:var(--muted);text-align:center}
  .evcal-empty[hidden],.evcal .evcal-bar button[hidden]{display:none}
  @media (max-width:700px){
    .evcal{padding:12px 10px 6px}
    .evcal .ev-day{min-height:44px;font-size:12px}
  }
  /* ---------- The Flank button (Financials): the one public door into
     the ledger, dressed so it reads as the page's main action. Selector
     keys on the href, so no data-file change is involved. The swirls are
     masked to var(--ink) so they follow the theme; they spin gold on
     hover. Reduced motion keeps the lift/color but drops the spin. */
  .canvas a.btn[href^="flank/"]{
    position:relative;padding:10px 44px;border-width:2px;font-weight:700;
    box-shadow:0 0 0 3px var(--paper),0 0 0 5px var(--gold),0 8px 18px rgba(0,0,0,.14);
    transition:transform .18s ease,box-shadow .18s ease;
  }
  .canvas a.btn[href^="flank/"]::before,
  .canvas a.btn[href^="flank/"]::after{
    content:"";position:absolute;top:50%;width:20px;height:20px;margin-top:-10px;
    background-color:var(--ink);pointer-events:none;
    -webkit-mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 12a1 1 0 0 1 1 1a2 2 0 0 1-2 2a3 3 0 0 1-3-3a4 4 0 0 1 4-4a5 5 0 0 1 5 5a6 6 0 0 1-6 6a7 7 0 0 1-7-7a8 8 0 0 1 8-8' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round'/></svg>") center/contain no-repeat;
            mask:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 12a1 1 0 0 1 1 1a2 2 0 0 1-2 2a3 3 0 0 1-3-3a4 4 0 0 1 4-4a5 5 0 0 1 5 5a6 6 0 0 1-6 6a7 7 0 0 1-7-7a8 8 0 0 1 8-8' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round'/></svg>") center/contain no-repeat;
  }
  .canvas a.btn[href^="flank/"]::before{left:13px}
  .canvas a.btn[href^="flank/"]::after{right:13px;transform:scaleX(-1)}
  .canvas a.btn[href^="flank/"]:hover{transform:translateY(-1px)}
  .canvas a.btn[href^="flank/"]:hover::before,
  .canvas a.btn[href^="flank/"]:hover::after{background-color:var(--gold)}
  @media (prefers-reduced-motion:no-preference){
    .canvas a.btn[href^="flank/"]:hover::before{transform:rotate(200deg)}
    .canvas a.btn[href^="flank/"]:hover::after{transform:scaleX(-1) rotate(200deg)}
  }

  /* ---------- Settings page: visitor-side site dynamics (theme only for
     now). Choices ride the same fc-theme localStorage key the star script
     already reads, so a pick here follows the visitor across every page.
     The Flank ledger keeps its own look and never follows it. ---------- */
  .setpanel{
    border:1px solid var(--rule);border-radius:6px;background:var(--paper2);
    padding:18px 20px;margin:18px 0 16px;
  }
  body[data-theme="ink"] .setpanel{background:#332e28;border-color:#6b6152}
  body[data-theme="midnight"] .setpanel{background:#2b2336;border-color:#4d3c6b}
  .setpanel-title{margin:0 0 2px;font:700 17px Georgia,Times,serif;color:var(--ink)}
  .setpanel-sub{margin:0 0 14px;font:italic 13px Georgia,Times,serif;color:var(--muted)}
  .setgrid{display:flex;flex-wrap:wrap;gap:10px}
  .settheme{
    display:flex;flex-direction:column;align-items:center;gap:7px;width:118px;
    padding:12px 8px 10px;border:1px solid var(--rule);border-radius:6px;
    background:var(--paper);cursor:pointer;color:var(--ink2);
    font:13px Georgia,Times,serif;
  }
  .settheme:hover{border-color:var(--ink)}
  .settheme.set-cur{outline:2px solid var(--ink);outline-offset:2px}
  .setchip{position:relative;display:block;width:56px;height:40px;border:1px solid var(--rule);border-radius:4px;overflow:hidden}
  .setchip-dot{position:absolute;width:9px;height:9px;border-radius:50%}
  .setchip-reset{display:flex;align-items:center;justify-content:center;font-size:22px;color:var(--ink);font-style:normal}
  .setname{font-weight:700;font-size:13.5px;color:var(--ink)}
  .setdesc{font-size:11px;line-height:1.35;color:var(--muted);text-align:center}
  .setsaved{margin:12px 0 0;font:italic 13px Georgia,Times,serif;color:var(--ink2)}
  .setsaved[hidden]{display:none}

  footer{
    margin-top:34px;padding-top:14px;border-top:1px solid var(--rule);
    font:12.5px "Helvetica Neue",Arial,sans-serif;color:var(--faint);text-align:center;
  }
  footer p{margin:0 0 2px}
  footer .footlinks{margin:0}
  footer .footlinks a{
    color:var(--muted);text-decoration:none;margin:0 4px;
  }
  footer .footlinks a:hover{color:var(--ink);text-decoration:underline}
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
    body.has-badges .masthead{padding-top:112px}
    .masthead img{width:72px;height:72px}
    h1,.masthead .sitetitle{font-size:22px;letter-spacing:1px;overflow-wrap:break-word}
    .tagline{font-size:13px}
    nav.sitenav{
      display:flex;flex-wrap:wrap;justify-content:center;gap:6px 8px;
      font-size:11px;line-height:1.4;
    }
    nav.sitenav a,nav.sitenav .cur{
      margin:0;white-space:nowrap;
      padding:6px 12px;min-height:34px;display:inline-flex;align-items:center;
      border:1px solid var(--rule);border-radius:999px;
      background:var(--paper2);color:var(--ink2);
    }
    nav.sitenav a:hover{color:var(--ink);border-color:var(--ink)}
    nav.sitenav .cur{background:var(--ink);color:var(--paper);border-color:var(--ink)}
    .star-badge{top:8px;left:8px;right:auto;width:104px;height:104px}
    .star-badge .star-title{font-size:8.5px;max-width:72%;line-height:1.2;letter-spacing:.2px}
    .star-badge .star-sub{display:none}
    .gem-badge{top:30px;left:auto;right:8px;width:118px;height:64px;border-radius:10px;border-width:2.5px;box-shadow:4px 5px 0 #111}
    .gem-badge .gem-title{font-size:9px;letter-spacing:1.2px}
    .canvas{overflow-wrap:break-word}
    .canvas *{overflow-wrap:anywhere}
    /* A dragged button keeps its desktop pixel spot on wide screens, but
       pixels mean nothing once the canvas narrows: left:286px of an 804px
       canvas is mid-page, of a ~365px phone canvas it hangs off the right
       edge and the text wraps one word per line over the nearest image.
       On small screens a placed button reflows into the text as a centred
       block, exactly like an undragged one, and keeps its inline colours.
       !important is what beats the dragged inline style. */
    .canvas a.btn[style],.canvas a.bigbtn[style]{
      position:static !important;
      left:auto !important;right:auto !important;top:auto !important;bottom:auto !important;
      transform:none !important;
    }
    .canvas a.btn[style]{
      display:block !important;width:fit-content !important;height:auto !important;
      margin:12px auto !important;padding:12px 24px !important;text-align:center;
    }
    .canvas a.bigbtn[style]{
      width:fit-content !important;height:auto !important;max-width:100% !important;
      margin:20px auto !important;
    }
    /* giant inline titles (40px toolbar sizes and up) stay dramatic on
       phones instead of running off the screen. The t-big class is stamped
       by the sanitizer when the inline size is really 40px or more, so
       small text is never inflated by mistake. Modern-phone reference:
       393px viewport (iPhone 16e) — 320px still fits because of the vw leg. */
    .canvas .t-big{font-size:min(12vw,44px) !important;line-height:1.15}
    .canvas img.small{width:140px}
    .canvas img.medium{width:100%}
    .canvas table{display:block;overflow-x:auto}
    .canvas th,.canvas td{font-size:13.5px;padding:6px 8px}
    .canvas .t-giant{font-size:26px;line-height:1.25}
    .canvas .t-huge{font-size:22px}
    .canvas .gallery{flex-direction:column}
    .canvas .gallery img{width:100%}
    .canvas .card{padding:14px 14px}
    .cf-disclaimer{margin:16px 0 0;padding:12px 14px}
    .contactform{margin:14px 0 0;padding:16px 14px}
    .cf-done,.cf-error{margin:14px 0 0;padding:16px 14px}
    .canvas a.btn[href^="flank/"]{padding:9px 30px}
    .canvas a.btn[href^="flank/"]::before{left:9px}
    .canvas a.btn[href^="flank/"]::after{right:9px}
    .settheme{width:104px}
  }
  @media (hover:none){.contactform .dropzone{padding:18px 14px}}
'''


# Structured data: who we are, for search engines. The facts here are the
# ones already printed on the site (name, contact page, People page, GitHub).
# Built with json.dumps so quotes and ampersands can never break the block.
LD_ORG = (
    '<script type="application/ld+json">'
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Organization",
                    "@id": f"https://{CUSTOM_DOMAIN}/#organization",
                    "name": "Flamingo & Cactus",
                    "alternateName": [
                        "Flamingo and Cactus",
                        "Flamingo & Cactus Group",
                    ],
                    "url": f"https://{CUSTOM_DOMAIN}/",
                    "logo": "https://flamingoandcactus.com/apple-touch-icon.png",
                    "description": "The website dedicated to the Flamingo and Cactus group. Everything Flamingo and Cactus can be found here.",
                    "email": "flamingoncactusgroup@gmail.com",
                    "telephone": "+1-607-205-6663",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "Owego",
                        "addressRegion": "NY",
                        "addressCountry": "US",
                    },
                    "knowsAbout": [
                        "open-source hardware",
                        "PCB design",
                        "electrical unit construction",
                        "inverters",
                    ],
                    "member": {
                        "@type": "Person",
                        "name": "Bear J. Hartjen",
                        "jobTitle": "Manager",
                        "url": f"https://{CUSTOM_DOMAIN}/people.html",
                        "sameAs": ["https://github.com/bearjhartjen"],
                    },
                },
                {
                    "@type": "WebSite",
                    "@id": f"https://{CUSTOM_DOMAIN}/#website",
                    "url": f"https://{CUSTOM_DOMAIN}/",
                    "name": "Flamingo & Cactus",
                    "alternateName": "Flamingo and Cactus",
                    "publisher": {"@id": f"https://{CUSTOM_DOMAIN}/#organization"},
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    + "</script>"
)


STAR_TITLE = "Latest and greatest"
STAR_SUB = "Click me"

# Links that live in the footer rather than the menu, so the menu stays
# short on a phone. Each is rendered only when its page exists.
FOOTER_LINKS = (("latest", "News"), ("feedback", "Feedback"),
                ("promotional", "Promotional"), ("settings", "Settings"))

# Inbox for the contact form. Submissions POST here and FormSubmit forwards
# them to this address (first submission needs a one-time activation click
# in the email FormSubmit sends). The address is already printed in plain
# text on the contact page, so the form exposes nothing new.
CONTACT_EMAIL = "flamingoncactusgroup@gmail.com"

# The contact form's own words live in data/form.json so the builder can
# edit them; the plumbing (where it posts, the honeypot, the behaviour
# script) stays here where it cannot be broken by accident.
FORM_DEFAULTS = {
    "subject": "New message from the Flamingo and Cactus contact form",
    "name_label": "Your name",
    "name_sub": 'You can put "Random" if you would rather not say who you are.',
    "name_placeholder": "e.g. Random",
    "contact_label": "Email or phone",
    "contact_sub": "Optional — only if you want a reply.",
    "contact_placeholder": "Optional",
    "note_label": "Note",
    "note_placeholder": "Anything you want us to know…",
    "files_label": "Files",
    "files_sub": "Optional — multiple files on supported browsers, 10 MB total.",
    "button": "Send it our way",
    "show_contact": True,
    "show_files": True,
    # One send, one service (October 31, 2026). Earlier this was a chain
    # (FormSubmit -> Web3Forms -> FormInit) and several branches re-sent
    # after an ambiguous answer, so one submit could land twice. The page
    # script now posts once, to FormInit. forminit_form_id is the PUBLIC
    # form ID from forminit.com (dashboard -> new form -> Settings -> Public
    # mode); it is public by design and baked into the page - never paste an
    # X-API-KEY here, those are server-side only and must never touch a
    # public page. The failover keys below are kept so old data/form.json
    # files still load; the page no longer uses them. With JavaScript off,
    # the plain <form> posts once to FormSubmit.
    "primary_service": "FormInit (forminit.com)",
    "failover_service": "",
    "web3forms_access_key": "",
    "second_failover_service": "",
    "forminit_form_id": "",
    "disclaimer_title": "About this form",
    "disclaimer_body": "This form sends each note once, through FormInit (forminit.com), a third-party form service, and FormInit emails it to flamingoncactusgroup@gmail.com. The page never retries a send automatically, so one note cannot become two emails. If FormInit does not confirm, the page says so and points you at the direct email address instead. Files are limited to 10 MB in total. With JavaScript turned off, the plain form posts once through FormSubmit (formsubmit.co) and lands on a thank-you page. Your text stays in your browser while you type, so a failed send does not lose your draft.",
}

FORM_TEXT_KEYS = ("subject", "name_label", "name_sub", "name_placeholder",
                  "contact_label", "contact_sub", "contact_placeholder",
                  "note_label", "note_placeholder", "files_label",
                  "files_sub", "button",
                  "primary_service", "failover_service",
                  "second_failover_service",
                  "disclaimer_title", "disclaimer_body")
FORM_SECRET_KEYS = ("web3forms_access_key",)
# Public endpoint IDs: baked into the page on purpose (the browser must
# know where to POST). Same charset rule as secrets, own tuple so the
# two can never be confused.
FORM_ID_KEYS = ("forminit_form_id",)
FORM_BOOL_KEYS = ("show_contact", "show_files")
# Per-key length caps. The disclaimer body is the only one allowed to
# breathe, because that is the visitor-facing explanation. Everything
# else stays compact so the dialog cannot be tricked into a 20 KB button.
FORM_TEXT_LIMITS = {
    "subject": 200,
    "name_label": 80, "name_sub": 200, "name_placeholder": 80,
    "contact_label": 80, "contact_sub": 200, "contact_placeholder": 80,
    "note_label": 80, "note_placeholder": 200,
    "files_label": 80, "files_sub": 200,
    "button": 80,
    "primary_service": 120, "failover_service": 120,
    "second_failover_service": 120,
    "disclaimer_title": 120, "disclaimer_body": 1200,
    "web3forms_access_key": 256,
    "forminit_form_id": 128,
}


def load_form_config(data_dir) -> dict:
    """The contact form's words: data/form.json over the built-in defaults."""
    cfg = dict(FORM_DEFAULTS)
    try:
        doc = json.loads((Path(data_dir) / "form.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(doc, dict):
        return cfg
    return sanitize_form_config(doc, cfg)


def sanitize_form_config(doc, base: dict | None = None) -> dict:
    """Keep only the known form keys, with sane types and lengths.

    Text keys are capped at FORM_TEXT_LIMITS[key] (default 400). The
    web3forms_access_key is a "secret" — capped longer (it is a UUID-like
    URL-safe token) and never echoed back through the builder dialog; the
    forminit_form_id is a public endpoint ID (same charset, own tuple).
    Absent-from-doc keys keep their current value, which lets the dialog
    open them as blank without overwriting the user's stored value.
    """
    cfg = dict(FORM_DEFAULTS if base is None else base)
    if not isinstance(doc, dict):
        return cfg
    for key, val in doc.items():
        if key in FORM_TEXT_KEYS and isinstance(val, str):
            cfg[key] = val[:FORM_TEXT_LIMITS.get(key, 400)]
        elif key in FORM_SECRET_KEYS and isinstance(val, str):
            k = re.sub(r"[^A-Za-z0-9_-]", "", val)[:FORM_TEXT_LIMITS.get(key, 256)]
            # The dialog never shows stored keys, so it always sends blank.
            # Blank therefore means "leave it alone", never "erase it".
            if k or not cfg.get(key):
                cfg[key] = k
        elif key in FORM_ID_KEYS and isinstance(val, str):
            k = re.sub(r"[^A-Za-z0-9_-]", "", val)[:FORM_TEXT_LIMITS.get(key, 128)]
            if k or not cfg.get(key):
                cfg[key] = k
        elif key in FORM_BOOL_KEYS:
            cfg[key] = bool(val)
    return cfg


def contact_form_html(cfg: dict | None = None) -> str:
    """The contact form, with its words taken from the config."""
    cfg = dict(FORM_DEFAULTS, **(cfg or {}))

    def esc(text, attr=False):
        return html.escape(str(text), quote=True)

    contact_block = (
        f'  <label for="cf-contact">{esc(cfg["contact_label"])}'
        f'<span class="sub">{esc(cfg["contact_sub"])}</span></label>\n'
        f'  <input type="text" id="cf-contact" name="contact" maxlength="160" '
        f'autocomplete="email" placeholder="{esc(cfg["contact_placeholder"], True)}">'
    )
    if not cfg["show_contact"]:
        contact_block = '  <div style="display:none">' + contact_block + "</div>\n"
    files_block = (
        f'  <label id="cf-files-label">{esc(cfg["files_label"])}'
        f'<span class="sub">{esc(cfg["files_sub"])}</span></label>\n'
        '  <input type="file" id="cf-photo" name="attachment" aria-labelledby="cf-files-label">\n'
        '  <p class="filehint" id="cf-filehint">No files yet.</p>'
    )
    if not cfg["show_files"]:
        files_block = '  <div style="display:none">' + files_block + "</div>\n"
    # Disclaimer card: tells visitors who runs the form (a third-party
    # service), that a send is one request with no automatic retry, and
    # what to do if the send is not confirmed (email directly). Lives
    # under the form, where it is read after a send and before anyone
    # gives up on a flaky connection.
    services = ""
    if cfg.get("primary_service"):
        services += f'<span class="cf-svc">{esc(cfg["primary_service"])}</span>'
    if cfg.get("failover_service"):
        services += (
            f'<span class="cf-svc cf-svc-sep"><span class="cf-svc-or">or, if that&rsquo;s down,</span> '
            f'{esc(cfg["failover_service"])}</span>'
        )
    if cfg.get("second_failover_service"):
        services += (
            f'<span class="cf-svc cf-svc-sep"><span class="cf-svc-or">or, as a last resort,</span> '
            f'{esc(cfg["second_failover_service"])}</span>'
        )
    cover = (
        '  <aside class="cf-disclaimer" role="note">\n'
        f'    <p class="cf-disclaimer-h">{esc(cfg["disclaimer_title"])}</p>\n'
        f'    <p class="cf-disclaimer-p">{esc(cfg["disclaimer_body"])}</p>\n'
    )
    if services:
        cover += f'    <p class="cf-disclaimer-p">{services}</p>\n'
    cover += (
        '  </aside>\n'
    )
    markup = (
        '<form class="contactform" action="https://formsubmit.co/' + CONTACT_EMAIL + '" method="POST" enctype="multipart/form-data">\n'
        f'  <input type="hidden" name="_subject" value="{esc(cfg["subject"], True)}">\n'
        '  <input type="hidden" name="_template" value="table">\n'
        '  <input type="hidden" name="_captcha" value="false">\n'
        # _next is FormSubmit's documented redirect field (formsubmit.co/
        # documentation). It is used only by the no-JavaScript road: the
        # whole window lands on our contact-ack.html after FormSubmit
        # accepts the post. With JavaScript on, the form never navigates.
        f'  <input type="hidden" name="_next" value="https://{CUSTOM_DOMAIN}/contact-ack.html">\n'
        '  <input type="text" name="_honey" style="display:none">\n'
        '  <input type="hidden" id="cf-sentat" name="sent_at" value="">\n'
        f'  <label for="cf-name">{esc(cfg["name_label"])}'
        f'<span class="sub">{esc(cfg["name_sub"])}</span></label>\n'
        f'  <input type="text" id="cf-name" name="name" required maxlength="120" '
        f'autocomplete="name" placeholder="{esc(cfg["name_placeholder"], True)}">\n'
        + contact_block +
        f'  <label for="cf-note">{esc(cfg["note_label"])}</label>\n'
        f'  <textarea id="cf-note" name="note" required maxlength="5000" '
        f'placeholder="{esc(cfg["note_placeholder"], True)}"></textarea>\n'
        + files_block + '\n'
        '  <p class="cf-status" id="cf-status" hidden></p>\n'
        '  <noscript><p class="filehint">With JavaScript off, send one file per message.</p></noscript>\n'
        f'  <button type="submit">{esc(cfg["button"])}</button>\n'
        "</form>\n"
        # The "About this form" card rides directly under the form.
        + cover
    )
    script = (CONTACT_FORM_SCRIPT
              .replace("__BUTTON_JSON__", json.dumps(str(cfg["button"])))
              .replace("__SUBJECT_JSON__", json.dumps(str(cfg["subject"])))
              .replace("__FORM_EMAIL_JSON__", json.dumps(CONTACT_EMAIL))
              .replace("__DOMAIN_JSON__", json.dumps(CUSTOM_DOMAIN))
              .replace("__FORMINIT_ID_JSON__", json.dumps(str(cfg.get("forminit_form_id", ""))))
              .replace("__DRAFT_KEY_JSON__", json.dumps("flamingo-cf-draft")))
    return markup + script


CONTACT_FORM_SCRIPT = (
  '<script>(function(){try{'
  # Submission handling with three guarantees:
  #   1. The visitor stays on the page; success / error are explicit panels.
  #   2. A failed send never loses what the visitor typed: text fields are
  #      saved to localStorage on every keystroke and restored on reload.
  #   3. One send, one service. A send makes exactly one request, to
  #      FormInit, which answers over fetch with a JSON verdict for text
  #      notes and files alike. There is no chain and no automatic re-send:
  #      a service that may have received the note is never followed by a
  #      second service, because that is how one submit became two emails.
  #      With JavaScript off the plain form posts once to FormSubmit.
  "var FORM_EMAIL=" + "__FORM_EMAIL_JSON__" + ";"
  "var FORMINIT_ID=" + "__FORMINIT_ID_JSON__" + ";"
  "var FORMINIT_URL='https://forminit.com/f/';"
  "var FORMINIT_MAX_FILES=20;"
  "var FORMINIT_MAX_BYTES=26214400;"
  "var DRAFT_KEY=" + "__DRAFT_KEY_JSON__" + ";"
  'var baseMsg="",tick=null,t0=0;'
  'var inlineStatus=function(t,err){var st=document.getElementById("cf-status");if(!st)return;'
  'baseMsg=t;st.hidden=false;st.textContent=t;if(err)st.classList.add("is-err");else st.classList.remove("is-err");'
  'if(busy&&!err)st.classList.add("is-busy");};'
  'var form=document.querySelector("form.contactform"),hint=document.getElementById("cf-filehint");'
  'var inp=document.getElementById("cf-photo");'
  'if(!form||!hint||!inp||!inp.form)return;'
  'var all=[],MAX_BYTES=10000000,busy=false,picker=null,lastStamp="",settled=false,inflight=[],sid="";'
  'function getBtn(){return form.querySelector("button[type=submit]");}function trackCtrl(c){if(c)inflight.push(c);return c;}function abortInflight(){for(var i=0;i<inflight.length;i++){try{inflight[i].abort();}catch(_){}}inflight=[];}function newSid(){try{if(window.crypto&&crypto.randomUUID)return crypto.randomUUID().slice(0,8);}catch(_){}return String(Date.now()).slice(-8)+Math.floor(Math.random()*1e4);}'
  'function restoreBtn(){try{form.target="";}catch(_){}var b=getBtn();if(b){b.disabled=false;b.textContent=__BUTTON_JSON__;}setSending(false);}'
  # While a send is in flight: the form is marked busy, the status line
  # shows a moving bar plus elapsed seconds, and the browser asks before
  # the visitor can close or leave the tab (a note and its files are only
  # safe once the "It went through" panel is up).
  'function setSending(on){var st=document.getElementById("cf-status");'
  'try{form.classList.toggle("is-sending",!!on);form.setAttribute("aria-busy",on?"true":"false");}catch(_){}'
  'if(tick){clearInterval(tick);tick=null;}'
  'if(on){t0=Date.now();tick=setInterval(function(){if(!busy||!st||st.hidden||st.classList.contains("is-err"))return;'
  'var n=Math.round((Date.now()-t0)/1000);st.textContent=baseMsg+(n>=5?" ("+n+"s)":"");},1000);}'
  'else if(st){st.classList.remove("is-busy");}}'
  'window.addEventListener("beforeunload",function(e){if(!busy)return;'
  'try{e.preventDefault();e.returnValue="";}catch(_){}return"";});'
  # Browser-side draft: text fields saved to localStorage on every
  # keystroke, restored on load, cleared after a successful send. Files
  # are not safe to keep in storage, but their per-row state survives
  # because render() reads from the same `all` array.
  'function saveDraft(){try{var n=form.querySelector("[name=name]"),c=form.querySelector("[name=contact]"),t=form.querySelector("[name=note]");'
  'localStorage.setItem(DRAFT_KEY,JSON.stringify({n:n?n.value:"",c:c?c.value:"",t:t?t.value:"",s:new Date().toISOString()}));}'
  'catch(_){}}'
  'function clearDraft(){try{localStorage.removeItem(DRAFT_KEY);}catch(_){}'
  'try{var nf=form.querySelector("[name=name]"),cf=form.querySelector("[name=contact]"),tf=form.querySelector("[name=note]");'
  'if(nf)nf.value="";if(cf)cf.value="";if(tf)tf.value="";}catch(_){}}'
  'function restoreDraft(){try{var raw=localStorage.getItem(DRAFT_KEY);if(!raw)return;'
  'var d=JSON.parse(raw);if(!d||typeof d!=="object")return;'
  'var nf=form.querySelector("[name=name]"),cf=form.querySelector("[name=contact]"),tf=form.querySelector("[name=note]");'
  'if(nf&&d.n)nf.value=String(d.n);if(cf&&d.c)cf.value=String(d.c);if(tf&&d.t)tf.value=String(d.t);'
  'if((d.n||d.c||d.t)){inlineStatus("Draft restored from your browser — it will clear when the note goes through.",false);}'
  '}catch(_){}}'
  '["name","contact","note"].forEach(function(n){var el=form.querySelector("[name="+n+"]");if(el)el.addEventListener("input",saveDraft);});'
  'restoreDraft();'
  # The two big panels are built up front and sit just before the form.
  # showDone() swaps the form out for the success panel; showError() keeps
  # the form (with everything the visitor typed) and explains the way out.
  'var done=document.createElement("div");done.className="cf-done";done.setAttribute("role","status");'
  'var doneHead=document.createElement("p");doneHead.className="cf-done-head";doneHead.textContent="It went through.";'
  'var doneSub=document.createElement("p");doneSub.className="cf-done-sub";'
  'var doneStamp=document.createElement("p");doneStamp.className="cf-done-stamp";'
  'var again=document.createElement("button");again.type="button";again.className="cf-again";again.textContent="Send another note";'
  'done.appendChild(doneHead);done.appendChild(doneSub);done.appendChild(doneStamp);done.appendChild(again);'
  'done.hidden=true;form.parentNode.insertBefore(done,form);'
  'var errbox=document.createElement("div");errbox.className="cf-error";errbox.setAttribute("role","alert");'
  'var errHead=document.createElement("p");errHead.className="cf-error-head";errHead.textContent="It did not go through.";'
  'var errBody=document.createElement("p");errBody.className="cf-error-body";'
  'var errMail=document.createElement("p");errMail.className="cf-error-body";errMail.textContent="You can email us directly instead: ";'
  'var emLink=document.createElement("a");emLink.href="mailto:"+FORM_EMAIL;emLink.textContent=FORM_EMAIL;'
  'errMail.appendChild(emLink);'
  'errbox.appendChild(errHead);errbox.appendChild(errBody);errbox.appendChild(errMail);'
  'errbox.hidden=true;form.parentNode.insertBefore(errbox,form);'
  'function scrollUp(el){try{el.scrollIntoView({behavior:"smooth",block:"center"});}catch(_){try{el.scrollIntoView();}catch(_2){}}}'
  'function showDone(u,ref){if(settled)return;settled=true;abortInflight();busy=false;window.cfInflight=false;restoreBtn();errbox.hidden=true;form.hidden=true;done.hidden=false;'
  'doneSub.textContent=u||"Your note is in our inbox. We read everything — thank you!";'
  'var stamp=lastStamp?("Stamped "+lastStamp):"";if(ref)stamp+=stamp?(" · ref "+ref):("Ref "+ref);doneStamp.textContent=stamp;'
  'clearDraft();scrollUp(done);}'
  'function showError(msg,retry){busy=false;window.cfInflight=false;restoreBtn();done.hidden=true;errbox.hidden=false;'
  'errBody.textContent=(msg||"The note did not send.")+(retry===false?"":" Press Send again to retry.");'
  'scrollUp(errbox);}'
  'function hideStatus(){var st=document.getElementById("cf-status");if(st){st.hidden=true;}}'
  'again.addEventListener("click",function(){try{settled=false;sid="";done.hidden=true;errbox.hidden=true;form.hidden=false;'
  'try{form.reset();}catch(_){}all=[];'
  'try{if(picker)picker.value="";}catch(_){}try{inp.value="";}catch(_){}inp.multiple=true;'
  'render();stampTime();hideStatus();}catch(_){}});'
  'function stampTime(){try{var st=document.getElementById("cf-sentat");if(!st)return;'
  'var d=new Date();var s="";'
  'try{s=d.toLocaleString("en-US",{timeZone:"America/New_York",month:"numeric",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit",second:"2-digit",timeZoneName:"short"})+" (Eastern)";}'
  'catch(_){s=d.toString()+" | UTC offset "+(d.getTimezoneOffset()/-60)+" hours";}'
  'st.value=s;lastStamp=s;}catch(_){}}'
  'stampTime();'
  # ?sent=1 (the files road redirects back here with it): the note went
  # through - show the same big panel instead of the form.
  'try{var q=new URLSearchParams(location.search);'
  'if(q.get("sent")==="1"){showDone();'
  'try{history.replaceState(null,"",location.pathname);}catch(_){}}}catch(_){}'
  'function setFiles(target,files){try{var dt=new DataTransfer();for(var i=0;i<files.length;i++)dt.items.add(files[i]);target.files=dt.files;'
  'if(!target.files||target.files.length!==files.length)return false;'
  'for(var j=0;j<files.length;j++)if(target.files[j].name!==files[j].name||target.files[j].size!==files[j].size)return false;'
  'return true;}catch(_){return false;}}'
  'if(typeof DataTransfer==="undefined"||typeof File==="undefined"||!setFiles(inp,[new File(["check"],"fc-upload-check.txt",{type:"text/plain"})])){'
  'try{inp.value="";}catch(_){}hint.textContent="This browser can send one file per message. Update it to enable multi-file upload.";return;}'
  'try{inp.value="";}catch(_){}'
  'inp.className="filepick";inp.multiple=true;inp.setAttribute("tabindex","-1");inp.setAttribute("aria-hidden","true");'
  'picker=document.createElement("input");picker.type="file";picker.id="cf-file-picker";picker.multiple=true;picker.className="filepick";picker.tabIndex=-1;picker.setAttribute("aria-hidden","true");'
  'var zone=document.createElement("button");zone.type="button";zone.className="dropzone";zone.id="cf-drop";'
  'zone.setAttribute("aria-labelledby","cf-files-label cf-drop-instructions");'
  'var instructions=document.createElement("span");instructions.id="cf-drop-instructions";'
  'instructions.textContent=window.matchMedia&&window.matchMedia("(hover:none)").matches?"Tap to browse and attach files.":"Drop files here or click to browse.";zone.appendChild(instructions);'
  'var lst=document.createElement("ul");lst.className="filelist";lst.id="cf-files";'
  'inp.parentNode.insertBefore(zone,inp.nextSibling);zone.parentNode.insertBefore(lst,zone.nextSibling);'
  'form.appendChild(picker);'
  'function fmt(n){return n>1048576?(n/1048576).toFixed(1)+" MB":Math.round(n/1024).toLocaleString("en-US")+" KB";}'
  'function sync(){return setFiles(inp,all);}'
  'function failToNative(msg){all=[];'
  'try{inp.value="";picker.value="";}catch(_){}inp.multiple=false;inp.className="";inp.removeAttribute("tabindex");inp.removeAttribute("aria-hidden");'
  'picker.disabled=true;zone.hidden=true;lst.hidden=true;hint.textContent=msg;hint.classList.add("over");}'
  'function render(){var t=0,i;lst.innerHTML="";'
  'for(i=0;i<all.length;i++)(function(f,ix){t+=f.size;'
  'var li=document.createElement("li"),nm=document.createElement("span"),b=document.createElement("button");'
  'nm.textContent=f.name+" ("+fmt(f.size)+")";li.appendChild(nm);'
  'b.type="button";b.textContent="Remove";b.setAttribute("aria-label","Remove "+f.name);'
  'b.onclick=function(){if(busy)return;all.splice(ix,1);sync();render();};li.appendChild(b);lst.appendChild(li);})(all[i],i);'
  'var over=t>MAX_BYTES;'
  'hint.textContent=!all.length?"No files yet.":all.length+(all.length===1?" file, ":" files, ")+fmt(t)+" of 10 MB.";'
  'hint.classList.toggle("over",over);return !over;}'
  'function add(fs){if(busy)return false;for(var i=0;i<fs.length;i++)all.push(fs[i]);if(!sync()){failToNative("This browser could not safely combine files. Use the file picker and send one file per message.");return false;}render();return true;}'
  'zone.addEventListener("click",function(){picker.click();});'
  'zone.addEventListener("dragover",function(e){if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.indexOf("Files")>=0){e.preventDefault();zone.classList.add("over");}});'
  'zone.addEventListener("dragleave",function(){zone.classList.remove("over");});'
  'zone.addEventListener("drop",function(e){e.preventDefault();zone.classList.remove("over");var d=e.dataTransfer;if(d&&d.files&&d.files.length)add(d.files);});'
  'form.addEventListener("paste",function(e){var d=e.clipboardData;if(d&&d.files&&d.files.length){e.preventDefault();add(d.files);}});'
  'picker.addEventListener("change",function(){if(picker.files&&picker.files.length)add(picker.files);try{picker.value="";}catch(_){}});'
  # One send, one service (October 31, 2026). Outcomes of the single request:
  #   delivered (JSON success:true,   -> "It went through" panel
  #              or the thank-you
  #              redirect FormInit
  #              sends a non-JSON post)
  #   refused   (JSON success:false)     -> error panel; nothing was stored,
  #                                         so pressing Send again is safe
  #   unknown   (timeout, network error, -> the note MAY have arrived. It is
  #              or a reply that is not     never re-sent automatically; the
  #              JSON)                      visitor is pointed at the email
  #                                         address instead.
  'function noteFooter(payload){return (payload.note||"")+"\\n\\n---\\nSent from https://"+__DOMAIN_JSON__+"/contact.html on "+(payload.sent_at||lastStamp||"")+(sid?(" · ref "+sid):"");}'
  'function readJson(r,who){return r.text().then(function(t){var j=null;try{j=JSON.parse(t);}catch(_){}'
  'return{json:j,status:r.status,url:r.url||"",redirected:!!r.redirected,'
  'message:j?null:(who+" answered HTTP "+r.status+" with a page, not a result")};});}'
  'function totalBytes(){var t=0,i;for(i=0;i<all.length;i++)t+=all[i].size;return t;}'
  'function fileTimeout(){return Math.min(120000,30000+Math.ceil(totalBytes()/1048576)*8000);}'
  'function fitsFormInit(){return all.length>0&&all.length<=FORMINIT_MAX_FILES&&totalBytes()<=FORMINIT_MAX_BYTES;}'
  'function mailOf(payload){var s=String(payload.contact||"").replace(/^\\s+|\\s+$/g,"");'
  'return /^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(s)?s:"";}'
  # FormInit, public mode: no API key on the page, the form ID in the URL is
  # the whole credential. Text goes as JSON blocks; files go as FormData with
  # fi- prefixed names. Empty optional fields are left out, and the sender
  # email is only included when it parses as an email (FormInit validates it).
  'function formInitSender(payload){var m=mailOf(payload);'
  'if(!m)return null;'
  'return {type:"sender",properties:{email:m,fullName:payload.name||"Visitor"}};}'
  # Reads the fields fresh at send time.
  'function filePayload(){var g=function(n){var el=form.querySelector("[name="+n+"]");return el?el.value:"";};'
  'return {_subject:g("_subject")||__SUBJECT_JSON__,_template:"table",_captcha:"false",'
  'sent_at:(document.getElementById("cf-sentat")||{}).value||lastStamp,'
  'name:g("name"),contact:g("contact"),note:g("note")};}'
  # FormInit answers a JSON post with {"success":true,...}. A multipart
  # post that does not ask for JSON gets a 302 to its thank-you page
  # instead - the upload has still been accepted, so a redirect to that
  # page is a delivery confirmation, not a failure. Every fetch below
  # also sends Accept: application/json so the normal path stays JSON.
  'function fiResult(res){var j=res.json;'
  'if(j&&j.success===true)return{ok:true,raw:j,used:"forminit",ref:(j.submission&&j.submission.hashId)||""};'
  'if(!j&&res.redirected&&/^https?:\\/\\/forminit\\.com\\/thank-you/.test(res.url||""))return{ok:true,raw:res,used:"forminit",ref:"",viaRedirect:true};'
  'return{ok:false,raw:j||res,used:"forminit",reason:(j?"":"unreadable")};}'
  'function fiFail(er){return{ok:false,used:"forminit",reason:(er&&er.name==="AbortError")?"timeout":"network"};}'
  'function postFormInit(payload){'
  'if(settled)return Promise.resolve({ok:false,reason:"settled"});'
  'if(!FORMINIT_ID)return Promise.resolve({ok:false,reason:"no-id"});'
  'var blocks=[];var snd=formInitSender(payload);if(snd)blocks.push(snd);'
  'blocks.push({type:"text",name:"name",value:payload.name||"Visitor"});'
  'if(payload.contact)blocks.push({type:"text",name:"contact",value:payload.contact});'
  'blocks.push({type:"text",name:"note",value:noteFooter(payload)});'
  'blocks.push({type:"text",name:"sent_at",value:payload.sent_at||lastStamp||""});'
  'var ctrl=(typeof AbortController!=="undefined")?new AbortController():null;'
  'var to=ctrl?setTimeout(function(){try{ctrl.abort();}catch(_){}},30000):null;'
  'if(ctrl)trackCtrl(ctrl);'
  'return fetch(FORMINIT_URL+FORMINIT_ID,{method:"POST",headers:{Accept:"application/json","Content-Type":"application/json"},body:JSON.stringify({blocks:blocks}),signal:ctrl?ctrl.signal:undefined})'
  '.then(function(r){return readJson(r,"FormInit");})'
  '.then(function(res){if(to)clearTimeout(to);return fiResult(res);})'
  '.catch(function(er){if(to)clearTimeout(to);return fiFail(er);});'
  '}'
  'function postFormInitFiles(payload){'
  'if(settled)return Promise.resolve({ok:false,reason:"settled"});'
  'if(!FORMINIT_ID)return Promise.resolve({ok:false,reason:"no-id"});'
  'if(!fitsFormInit())return Promise.resolve({ok:false,reason:"too-big"});'
  'var fd=new FormData();var m=mailOf(payload);'
  'if(m)fd.append("fi-sender-email",m);'
  'fd.append("fi-sender-fullName",payload.name||"Visitor");'
  'if(payload.contact)fd.append("fi-text-contact",payload.contact);'
  'fd.append("fi-text-note",noteFooter(payload));'
  'fd.append("fi-text-sent_at",payload.sent_at||lastStamp||"");'
  'for(var i=0;i<all.length;i++)fd.append("fi-file-attachment"+(i+1),all[i],all[i].name);'
  'var ctrl=(typeof AbortController!=="undefined")?new AbortController():null;'
  'var to=ctrl?setTimeout(function(){try{ctrl.abort();}catch(_){}},fileTimeout()):null;'
  'if(ctrl)trackCtrl(ctrl);'
  'return fetch(FORMINIT_URL+FORMINIT_ID,{method:"POST",headers:{Accept:"application/json"},body:fd,signal:ctrl?ctrl.signal:undefined})'
  '.then(function(r){return readJson(r,"FormInit");})'
  '.then(function(res){if(to)clearTimeout(to);return fiResult(res);})'
  '.catch(function(er){if(to)clearTimeout(to);return fiFail(er);});'
  '}'
  'function finish(r,withFiles){'
  'if(settled)return true;'
  'if(r.ok){showDone(withFiles?"Your note and "+(all.length===1?"its file":"its files")+" went through. We read everything \u2014 thank you!":"Your note went through. We read everything \u2014 thank you!",r.ref);return true;}'
  'if(r.reason==="timeout"||r.reason==="network"||r.reason==="unreadable"){'
  'inlineStatus("FormInit did not confirm the send \u2014 the request ended without a readable answer (timeout or network drop). Your note may still have arrived. It was sent once and is not retried automatically, so you will not get duplicates. If nothing shows up, email "+FORM_EMAIL+" directly.",true);'
  'busy=false;window.cfInflight=false;restoreBtn();return false;}'
  'var noId=r.reason==="no-id";var why=noId?"this form has no sending service configured":((r.raw&&r.raw.message)?String(r.raw.message):"FormInit refused the note");'
  'showError("The note was not sent ("+why+").",!noId);return false;}'
  'form.addEventListener("submit",function(e){var pd=false;'
  'try{e.preventDefault();pd=true;'
  'if(busy)return;'
  'stampTime();'
  'if(!render()){hint.textContent="Those files are over 10 MB in total \u2014 remove one and try again.";return;}'
  'if(all.length>FORMINIT_MAX_FILES){hint.textContent="That is more than "+FORMINIT_MAX_FILES+" files \u2014 remove some and try again.";return;}'
  'var honey=form.querySelector("[name=_honey]");'
  'if(honey&&honey.value){showDone();return;}'
  'var b=getBtn();if(b){b.disabled=true;b.textContent="Sending\u2026";}'
  'busy=true;window.cfInflight=true;setSending(true);sid=newSid();'
  'var payload=filePayload();var withFiles=all.length>0;'
  'inlineStatus(withFiles?"Uploading "+(all.length===1?"1 file":all.length+" files")+" \u2014 a big upload can take a moment\u2026":"Sending your note\u2026",false);'
  'var run=withFiles?postFormInitFiles(payload):postFormInit(payload);'
  'run.then(function(r){finish(r,withFiles);}).catch(function(){finish({ok:false,reason:"network"},withFiles);});'
  '}catch(er){if(pd){try{busy=false;window.cfInflight=false;restoreBtn();}catch(_2){}}}});'
  'window.addEventListener("pageshow",function(){if(window.cfInflight)return;busy=false;window.cfInflight=false;restoreBtn();});'
  'render();}catch(_){}})();</script>'
)

# The form as published when nothing has been customised.
CONTACT_FORM_HTML = contact_form_html()


def calendar_cards_html(events: list, for_builder: bool = False) -> str:
    """Render chronological event cards with a collapsible past section."""
    today = datetime.date.today()
    upcoming, past = [], []
    for ev in events:
        try:
            date = datetime.date(int(ev.get("y", 1970)), int(ev.get("m", 1)), int(ev.get("d", 1)))
        except (TypeError, ValueError):
            continue
        (upcoming if date >= today else past).append(ev)
    key = lambda e: (int(e.get("y", 0)), int(e.get("m", 0)), int(e.get("d", 0)))
    upcoming.sort(key=key)
    past.sort(key=key, reverse=True)

    def render(ev, is_past=False):
        badge = (
            '<div class="ev-date-badge">'
            f'<span class="ev-badge-month">{html.escape(str(ev.get("ms", "EVENT")))}</span>'
            f'<span class="ev-badge-day">{int(ev.get("d", 0))}</span>'
            f'<span class="ev-badge-wd">{html.escape(str(ev.get("wd", "")))}</span></div>'
        )
        title = html.escape(str(ev.get("title") or ev.get("label") or "Event"))
        tags = []
        if ev.get("times"):
            tags.append(f'<span class="ev-card-tag">Time: {html.escape(str(ev["times"]))}</span>')
        if ev.get("venue"):
            tags.append(f'<span class="ev-card-tag">Venue: {html.escape(str(ev["venue"]))}</span>')
        tags_html = '<div class="ev-card-tags">' + ''.join(tags) + '</div>' if tags else ''
        details = str(ev.get("details") or "")
        if ev.get("link") and ev["link"].get("text"):
            details = re.sub(re.escape(str(ev["link"]["text"])), '', details, count=1).strip(' .,:;')
        desc = f'<p class="ev-card-desc">{html.escape(details)}</p>' if details else ''
        link = ev.get("link") or {}
        action = ''
        if link.get("url"):
            text = str(link.get("text") or "Event details")
            if text.lower() in ("here", "link"):
                text = "Event details"
            action = (f'<div class="ev-card-actions"><a class="ev-card-btn" href="{html.escape(str(link["url"]), quote=True)}" '
                      f'target="_blank" rel="noopener">{html.escape(text)} &rarr;</a></div>')
        edit = ''
        if for_builder:
            edit = (f'<div class="ev-card-actions"><button type="button" class="ev-edit-btn" '
                    f'data-ev-edit="{int(ev.get("idx", 0))}">Edit event</button></div>')
        past_class = ' past-card' if is_past else ''
        return (f'<article class="ev-card{past_class}" data-ev-idx="{int(ev.get("idx", 0))}">{badge}'
                f'<div class="ev-card-content"><h3 class="ev-card-title">{title}</h3>{tags_html}{desc}{action}{edit}</div></article>')

    if upcoming:
        current = '<div class="ev-cards-list">' + ''.join(render(e) for e in upcoming) + '</div>'
    else:
        current = ('<div class="ev-empty-card"><h3>No upcoming events scheduled right now</h3>'
                   '<p>Check back soon for new craft fairs and pop-ups.</p></div>')
    if past:
        current += (f'<details class="ev-past-drawer"><summary>Past Events ({len(past)})</summary>'
                    '<div class="ev-cards-list">' + ''.join(render(e, True) for e in past) + '</div></details>')
    return current


def calendar_static_html(events: list, year: int, month: int,
                         nav: bool = False, cfg: dict | None = None) -> str:
    """Render cards by default plus an optional month grid for the builder toggle."""
    import calendar as _calendar
    cfg = dict(CAL_DEFAULTS, **(cfg or {}))
    symbol = html.escape(cfg.get("symbol") or "●")
    color = cfg.get("color") or "#1a7f37"
    size = int(cfg.get("size") or 17)
    month = max(1, min(12, int(month)))
    year = int(year)
    month_name = _MONTH_SHORT[month - 1]
    first_dow = _calendar.monthrange(year, month)[0]
    days = _calendar.monthrange(year, month)[1]
    today = datetime.date.today()
    by_day: dict = {}
    for ev in events:
        if ev.get("y") == year and ev.get("m") == month:
            by_day.setdefault(int(ev.get("d", 0)), []).append(ev)
    cells = ['<span class="ev-day empty"></span>'] * first_dow
    for day in range(1, days + 1):
        mine = by_day.get(day, [])
        cls = "ev-day" + (" ev-set" if mine else "")
        if day == today.day and year == today.year and month == today.month:
            cls += " today"
        tip = " | ".join(str(ev.get("day", "")) + (" · " + str(ev["times"]) if ev.get("times") else "") + " — " + str(ev.get("label", "")) for ev in mine)
        attrs = (f' data-cal-day="{year:04d}-{month:02d}-{day:02d}" data-d="{day}"'
                 f' title="{html.escape(tip, quote=True)}" aria-label="{html.escape(tip, quote=True)}"')
        dot = f'<span class="ev-dot" style="color:{color}">{symbol}</span>' if mine else ""
        cells.append(f'<span class="{cls}"{attrs}>{day}{dot}</span>')
    bar = ''
    if nav:
        bar = ('<div class="evcal-bar"><button type="button" data-cal-prev aria-label="Previous month">&lsaquo;</button>'
               f'<span class="evcal-label">{html.escape(month_name)} {year}</span>'
               '<button type="button" data-cal-next aria-label="Next month">&rsaquo;</button></div>')
    empty = '<div class="evcal-empty" hidden>No events found for this month.</div>' if not by_day else ''
    builder_add = '<div class="ev-builder-actions"><button type="button" id="builderAddEvent" data-cal-add>+ Add Event</button></div>' if nav else ''
    return (f'<div class="evcal" data-cal-block style="display:block;--ev-mark:{color};--ev-mark-size:{size}px">'
            f'{builder_add}<div class="evcal-nav-tabs">'
            '<button type="button" class="ev-tab-btn active" data-cal-tab="cards">Upcoming Events</button>'
            '<button type="button" class="ev-tab-btn" data-cal-tab="grid">Month Calendar</button></div>'
            f'<div class="ev-cards-wrap" id="ev-cards-static">{calendar_cards_html(events, for_builder=nav)}</div>'
            '<div class="ev-cal-wrap" id="ev-cal-wrap-static" hidden>'
            f'{bar}<div class="evcal-head" aria-hidden="true"><span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span></div>'
            f'<div class="evcal-grid">{"".join(cells)}</div>{empty}</div></div>')


def page_events(body_html: str) -> list[dict]:
    """Every dated event in the given markup (the public wrapper)."""
    return _parse_list_events(body_html)


def place_calendar_static(body_html: str, year: int | None = None,
                          month: int | None = None,
                          cfg: dict | None = None) -> str:
    """Swap an `[event calendar]` marker for a static month grid.

    The live page draws its own interactive calendar; this is the same
    grid rendered on the server, used by the builder so the owner can see
    the month they are editing."""
    events = _parse_list_events(body_html)
    marker_re = re.compile(r"<(?:p|li)[^>]*>\s*\[event calendar\]\s*</(?:p|li)>",
                           flags=re.IGNORECASE)
    if not marker_re.search(body_html):
        return body_html
    if year is None or month is None:
        today = datetime.date.today()
        y, m = today.year, today.month
        if events:
            near = min(events, key=lambda e: (e["y"], e["m"], e["d"]))
            y, m = near["y"], near["m"]
        year, month = y, m
    grid = calendar_static_html(events, year, month, nav=True, cfg=cfg)
    return marker_re.sub(lambda _m: grid, body_html, count=1)


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


# ---------------------------------------------------------------------------
# Events page: `[event calendar]` woven from the plain list already on the
# page. The list stays the one editable source; the calendar is derived from
# it, so the calendar only ever shows what the list says.
# ---------------------------------------------------------------------------

_EVENT_MONTH_NAMES = (
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sept", "sep"), ("october", "oct"),
    ("november", "nov"), ("december", "dec"),
)
_MONTH_ALTS = "|".join(alt for group in _EVENT_MONTH_NAMES for alt in group)
_EVENT_DATE_RE = re.compile(
    r"\b(" + _MONTH_ALTS + r")\.?[,]?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)", re.I)
_EVENT_SPAN_RE = re.compile(
    r"(?:\bfrom\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*"
    r"(?:to|until|till|through|thru|&ndash;|&mdash;|[&#8211;]|&#8212;|[\u2013\u2014]|\u2014|-)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)
_EVENT_AT_RE = re.compile(
    r"(?:\bat\b|from)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)
_EVENT_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_MONTH_LOOKUP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _event_block_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _event_unit(h: int, m: str | None, ap: str) -> str:
    ap = ap.replace(".", "").lower() or "am"
    h12, daypart = h, "am"
    if h == 12:
        daypart = "pm" if ap.startswith("p") else "am"
    elif h > 12:
        h12, daypart = h - 12, "pm"
    else:
        daypart = "am" if ap.startswith("a") else "pm"
    return f"{h12}{':' + m if m else ''} {daypart}"


def _parse_list_events(body_html: str) -> list[dict]:
    """Every dated event mentioned in the page's paragraphs and list items.

    The events source (typically a hidden `.evsrc` block) is what the owner
    edits; the calendar is derived from it and always reflects it."""
    spans = []
    for m in re.finditer(r"<(p|li)(?:[^>]*)>(.*?)</\1\s*>", body_html, re.S | re.I):
        spans.append((m.start(), m.group(2)))
    today = datetime.date.today()
    events: list[dict] = []
    for i, (_pos, frag) in enumerate(spans):
        text = _event_block_text(frag)
        if "[event calendar]" in text:
            continue
        if not text or "[contact form]" in text:
            continue
        dm = _EVENT_DATE_RE.search(text)
        if not dm:
            continue
        month = _MONTH_LOOKUP.get(dm.group(1)[:3].lower())
        day = int(dm.group(2))
        if not month or not 1 <= day <= 31:
            continue
        ym = _EVENT_YEAR_RE.search(text)
        year = int(ym.group(1)) if ym else (
            today.year if datetime.date(today.year, month, day) >= today
            else today.year + 1)
        try:
            stamp = datetime.date(year, month, day)
        except ValueError:
            continue
        tm = _EVENT_SPAN_RE.search(text)
        unit1 = unit2 = ""
        single = ""
        if tm:
            unit1 = _event_unit(int(tm.group(1)), tm.group(2), tm.group(3))
            unit2 = _event_unit(int(tm.group(4)), tm.group(5), tm.group(6))
        else:
            am = _EVENT_AT_RE.search(text)
            if am:
                single = _event_unit(int(am.group(1)), am.group(2), am.group(3))
        label = text
        for pat in _EVENT_SPAN_RE, _EVENT_AT_RE, _EVENT_DATE_RE, _EVENT_YEAR_RE:
            label = pat.sub(" ", label)
        label = re.sub(r"\s+", " ", label)
        label = re.sub(r"^[\s,.:;\-\u2013\u2014]+|[\s,.:;\-\u2013\u2014]+$", "", label)
        if len(label) > 84:
            cut = label[:84]
            sp = cut.rfind(" ")
            label = (label[:sp] if sp > 40 else cut).rstrip(" ,.;:-") + "…"
        link = None
        hit = re.search(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", frag, re.I | re.S)
        if hit:
            link = {"url": html.unescape(hit.group(1)),
                    "text": re.sub(r"<[^>]*>", "", hit.group(2)).strip()}
        clean_desc = text
        for pat in (_EVENT_SPAN_RE, _EVENT_AT_RE, _EVENT_DATE_RE, _EVENT_YEAR_RE):
            clean_desc = pat.sub(" ", clean_desc)
        clean_desc = re.sub(r"\s+", " ", clean_desc)
        clean_desc = re.sub(r"^[\s,.:;\-–—]+|[\s,.:;\-–—]+$", "", clean_desc)
        if link and link.get("text"):
            clean_desc = re.sub(r"\b" + re.escape(link["text"]) + r"\b", "", clean_desc, count=1, flags=re.I)
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip(" .,:;–—")
        dot_split = clean_desc.split(". ", 1)
        headline = dot_split[0].strip()
        ev_details = dot_split[1].strip() if len(dot_split) > 1 else ""
        if " · " in headline:
            hparts = headline.split(" · ", 1)
            ev_title, ev_venue = hparts[0].strip(), hparts[1].strip()
        elif " — " in headline:
            hparts = headline.split(" — ", 1)
            ev_title, ev_venue = hparts[0].strip(), hparts[1].strip()
        elif ", " in headline:
            hparts = headline.split(", ", 1)
            ev_title, ev_venue = hparts[0].strip(), hparts[1].strip()
        else:
            ev_title, ev_venue = headline, ""

        events.append({
            "y": stamp.year, "m": stamp.month, "d": stamp.day,
            "wd": _WEEKDAYS[stamp.weekday()],
            "ms": _MONTH_SHORT[stamp.month - 1],
            "key": f"{stamp.month}/{stamp.day}",
            "idx": i,
            "html": frag,
            "match": stamp.strftime("%B %d, %Y ") + text,
            "full": text,
            "link": link,
            "times": (f"{unit1} – {unit2}" if unit1 and unit2
                      else single if single else ""),
            "day": f"{_WEEKDAYS[stamp.weekday()]}, {_MONTH_SHORT[stamp.month - 1]} {stamp.day}, {stamp.year}",
            "label": label,
            "title": ev_title,
            "venue": ev_venue,
            "details": ev_details,
        })
        if len(events) >= 80:
            break
    return events


# How the calendar marks the days that have events: the symbol and its
# colour. The owner picks these in the builder; they live in
# data/calendar.json so the published calendar and the editor's drawing of
# it always match.
CAL_DEFAULTS = {
    "symbol": "●",
    "color": "#1a7f37",
    "size": 17,
}

CAL_SYMBOLS = ("●", "•", "■", "▲", "▼", "★", "◆", "✦", "✚", "♦")


def sanitize_calendar_config(doc, base: dict | None = None) -> dict:
    """Keep only the known calendar keys, with sane values."""
    cfg = dict(CAL_DEFAULTS if base is None else base)
    if not isinstance(doc, dict):
        return cfg
    symbol = str(doc.get("symbol", "") or "").strip()
    if symbol:
        cfg["symbol"] = symbol[:4]
    color = str(doc.get("color", "") or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        cfg["color"] = color
    try:
        size = int(doc.get("size", cfg.get("size", 17)))
    except (TypeError, ValueError):
        size = int(cfg.get("size", 17))
    cfg["size"] = max(8, min(48, size))
    return cfg


def load_calendar_config(data_dir) -> dict:
    """The calendar's marker: data/calendar.json over the defaults."""
    cfg = dict(CAL_DEFAULTS)
    try:
        doc = json.loads((Path(data_dir) / "calendar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    return sanitize_calendar_config(doc, cfg)


EVENTS_CALENDAR_MARKUP = (
    '<div class="evcal" id="evcal">'
    '<div class="evcal-nav-tabs" role="tablist" aria-label="Events view">'
    '<button type="button" class="ev-tab-btn active" data-cal-tab="cards" role="tab" aria-selected="true">Upcoming Events</button>'
    '<button type="button" class="ev-tab-btn" data-cal-tab="grid" role="tab" aria-selected="false">Month Calendar</button>'
    '</div>'
    '<div class="ev-cards-wrap" id="ev-cards"></div>'
    '<div class="ev-cal-wrap" id="ev-cal-wrap" hidden>'
    '<div class="evcal-bar">'
    '<button type="button" id="ev-prev" aria-label="Previous month">&lsaquo;</button>'
    '<select id="ev-month" aria-label="Month"></select>'
    '<select id="ev-year" aria-label="Year"></select>'
    '<button type="button" id="ev-next" aria-label="Next month">&rsaquo;</button>'
    '<input type="text" id="ev-search" class="ev-search" placeholder="Search events by name or date" aria-label="Search events">'
    '<button type="button" id="ev-reset" class="ev-reset" hidden>Show all</button>'
    '</div>'
    '<div class="evcal-head" aria-hidden="true"><span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span></div>'
    '<div class="evcal-grid" id="ev-grid"></div>'
    '<div class="evcal-empty" id="ev-empty" hidden>No events found for this month.</div>'
    '<div class="ev-detail" id="ev-detail" hidden></div>'
    '</div></div>'
)


def place_calendar(body_html: str, cfg: dict | None = None) -> tuple[str, str]:
    """Swap the event marker for the agenda/cards view and a compact calendar toggle."""
    import json as _json
    cfg = dict(CAL_DEFAULTS, **(cfg or {}))
    events = _parse_list_events(body_html)
    marker_re = re.compile(r"<(?:p|li)[^>]*>\s*\[event calendar\]\s*</(?:p|li)>", re.I)
    if not marker_re.search(body_html):
        return body_html, ""
    data = _json.dumps([{k: v for k, v in ev.items() if k not in ("idx", "html")}
                        for ev in events], ensure_ascii=True).replace("</", "<\\/")
    script = r"""<script>(function(){
var EV=__EVENTS__,cal=document.getElementById("evcal");if(!cal)return;
var MARK=__MARK__,MARKCOLOR=__COLOR__,MARKSIZE=__SIZE__;
cal.style.display="block";cal.style.setProperty("--ev-mark",MARKCOLOR);cal.style.setProperty("--ev-mark-size",MARKSIZE+"px");
var cards=document.getElementById("ev-cards"),grid=document.getElementById("ev-grid"),monthSel=document.getElementById("ev-month"),yearSel=document.getElementById("ev-year"),empty=document.getElementById("ev-empty"),detail=document.getElementById("ev-detail"),search=document.getElementById("ev-search"),reset=document.getElementById("ev-reset"),calWrap=document.getElementById("ev-cal-wrap");
var months=["January","February","March","April","May","June","July","August","September","October","November","December"],today=new Date(),ty=today.getFullYear(),cm=today.getMonth()+1,cy=ty;
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function dateKey(e){return e.y*10000+e.m*100+e.d;}
function renderCards(){var now=today.getFullYear()*10000+(today.getMonth()+1)*100+today.getDate(),up=[],past=[];EV.forEach(function(e){(dateKey(e)>=now?up:past).push(e);});up.sort(function(a,b){return dateKey(a)-dateKey(b);});past.sort(function(a,b){return dateKey(b)-dateKey(a);});
function card(e,isPast){var title=esc(e.title||e.label||"Event"),tags="";if(e.times)tags+='<span class="ev-card-tag">Time: '+esc(e.times)+'</span>';if(e.venue)tags+='<span class="ev-card-tag">Venue: '+esc(e.venue)+'</span>';var desc=e.details?'<p class="ev-card-desc">'+esc(e.details)+'</p>':'';var action="";if(e.link&&e.link.url){var txt=e.link.text||"Event details";if(/^(here|link)$/i.test(txt))txt="Event details";action='<div class="ev-card-actions"><a class="ev-card-btn" href="'+esc(e.link.url)+'" target="_blank" rel="noopener">'+esc(txt)+' &rarr;</a></div>';}
return '<article class="ev-card'+(isPast?' past-card':'')+'"><div class="ev-date-badge"><span class="ev-badge-month">'+esc(e.ms)+'</span><span class="ev-badge-day">'+esc(e.d)+'</span><span class="ev-badge-wd">'+esc(e.wd)+'</span></div><div class="ev-card-content"><h3 class="ev-card-title">'+title+'</h3>'+(tags?'<div class="ev-card-tags">'+tags+'</div>':'')+desc+action+'</div></article>';}
var output=up.length?'<div class="ev-cards-list">'+up.map(function(e){return card(e,false);}).join("")+'</div>':'<div class="ev-empty-card"><h3>No upcoming events scheduled right now</h3><p>Check back soon for new craft fairs and pop-ups.</p></div>';
if(past.length)output+='<details class="ev-past-drawer"><summary>Past Events ('+past.length+')</summary><div class="ev-cards-list">'+past.map(function(e){return card(e,true);}).join("")+'</div></details>';cards.innerHTML=output;}
var ys={};var ymin=ty-1,ymax=ty+5;EV.forEach(function(e){if(e.y<ymin)ymin=e.y;if(e.y>ymax)ymax=e.y;});for(var y=ymin;y<=ymax;y++)ys[y]=1;Object.keys(ys).map(Number).sort(function(a,b){return a-b;}).forEach(function(y){var o=document.createElement("option");o.value=y;o.textContent=y;yearSel.appendChild(o);});months.forEach(function(m,i){var o=document.createElement("option");o.value=i+1;o.textContent=m;monthSel.appendChild(o);});
var near=null,tk=ty*10000+(today.getMonth()+1)*100+today.getDate();EV.forEach(function(e){if(dateKey(e)>=tk&&(!near||dateKey(e)<dateKey(near)))near=e;});if(near){cm=near.m;cy=near.y;}monthSel.value=cm;yearSel.value=cy;
function q(){return(search.value||"").trim().toLowerCase();}function evOn(e){return !q()||String(e.match||e.label||"").toLowerCase().indexOf(q())>=0;}
function closeDay(){detail.hidden=true;detail.innerHTML="";grid.querySelectorAll(".ev-open").forEach(function(n){n.classList.remove("ev-open");});}
function openDay(d){var mine=EV.filter(function(e){return e.y===cy&&e.m===cm&&e.d===d;});if(!mine.length)return;detail.innerHTML="";var head=document.createElement("p");head.className="ev-detail-head";head.textContent=new Date(cy,cm-1,d).toLocaleDateString("en-US",{weekday:"long",month:"long",day:"numeric",year:"numeric"});detail.appendChild(head);mine.forEach(function(e){var item=document.createElement("p");item.className="ev-detail-item";if(e.times){var t=document.createElement("span");t.className="ev-detail-time";t.textContent=e.times;item.appendChild(t);}item.appendChild(document.createTextNode(e.full||e.label||""));detail.appendChild(item);});detail.hidden=false;grid.querySelectorAll(".ev-day").forEach(function(n){if(n.getAttribute("data-d")==String(d))n.classList.add("ev-open");});}
function renderGrid(){var y=cy,mo=cm,first=new Date(y,mo-1,1),days=new Date(y,mo,0).getDate(),dow=first.getDay();grid.innerHTML="";for(var i=0;i<dow;i++){var blank=document.createElement("span");blank.className="ev-day empty";grid.appendChild(blank);}var count=0;for(var d=1;d<=days;d++){var mine=EV.filter(function(e){return e.y===y&&e.m===mo&&e.d===d&&evOn(e);}),cell=document.createElement("button");cell.type="button";cell.className="ev-day";cell.textContent=d;cell.setAttribute("data-d",d);if(d===today.getDate()&&y===ty&&mo===today.getMonth()+1)cell.classList.add("today");if(mine.length){count++;cell.classList.add("ev-set");var dot=document.createElement("span");dot.className="ev-dot";dot.textContent=MARK;cell.appendChild(dot);cell.setAttribute("aria-label",mine.map(function(e){return e.day+" — "+e.label;}).join(" | "));}else if(q()){cell.hidden=true;}grid.appendChild(cell);}empty.hidden=!!count;empty.textContent=q()?"No events match your search.":"No events found for this month.";closeDay();}
renderCards();renderGrid();
cal.addEventListener("click",function(e){var tab=e.target.closest("[data-cal-tab]");if(tab){cal.querySelectorAll("[data-cal-tab]").forEach(function(b){b.classList.toggle("active",b===tab);b.setAttribute("aria-selected",b===tab?"true":"false");});var gridMode=tab.getAttribute("data-cal-tab")==="grid";calWrap.hidden=!gridMode;cards.hidden=gridMode;return;}var day=e.target.closest("[data-d]");if(day){var d=+day.getAttribute("data-d");if(day.classList.contains("ev-open"))closeDay();else{closeDay();openDay(d);}}});
function sync(){monthSel.value=cm;var found=false;for(var i=0;i<yearSel.options.length;i++)if(+yearSel.options[i].value===cy)found=true;if(!found){var o=document.createElement("option");o.value=cy;o.textContent=cy;yearSel.appendChild(o);}yearSel.value=cy;}
monthSel.addEventListener("change",function(){cm=+monthSel.value;renderGrid();});yearSel.addEventListener("change",function(){cy=+yearSel.value;renderGrid();});document.getElementById("ev-prev").addEventListener("click",function(){if(--cm<1){cm=12;cy--;}sync();renderGrid();});document.getElementById("ev-next").addEventListener("click",function(){if(++cm>12){cm=1;cy++;}sync();renderGrid();});search.addEventListener("input",function(){var s=q();reset.hidden=!s;if(s){var first=null;EV.forEach(function(e){if(evOn(e)&&(!first||dateKey(e)<dateKey(first)))first=e;});if(first){cm=first.m;cy=first.y;sync();}}renderGrid();});reset.addEventListener("click",function(){search.value="";reset.hidden=true;renderGrid();});
})();</script>"""
    script = (script.replace('__EVENTS__', data)
              .replace('__MARK__', json.dumps(cfg.get('symbol') or '●'))
              .replace('__COLOR__', json.dumps(cfg.get('color') or '#1a7f37'))
              .replace('__SIZE__', str(int(cfg.get('size') or 17))))
    placed = [0]
    def replace(_m):
        if placed[0]:
            return ''
        placed[0] = 1
        return EVENTS_CALENDAR_MARKUP
    return marker_re.sub(replace, body_html), script


POLL_DEFAULTS = {"intro": "", "polls": []}
POLL_TEXT_LIMITS = {"id": 64, "title": 160, "question": 400, "details": 2000,
                    "opens": 10, "closes": 10, "decision": 2000}
POLL_URL_FIELDS = ("anon_form", "signed_form", "anon_csv", "signed_csv",
                   "dev_csv", "finance_csv", "user_csv")


def sanitize_poll(raw) -> dict:
    """One poll, with only the known keys and safe values."""
    src = raw if isinstance(raw, dict) else {}
    out = {}
    for key, limit in POLL_TEXT_LIMITS.items():
        out[key] = str(src.get(key, "") or "").strip()[:limit]
    for key in POLL_URL_FIELDS:
        url = str(src.get(key, "") or "").strip()
        ok = bool(re.match(r"^https://[^\s<>\"']+$", url))
        # Local preview sheets are allowed so the bars can be tested without
        # publishing anything; the public site only ever uses https.
        if not ok and re.match(r"^http://(127\.0\.0\.1|localhost):\d+/[^\s<>\"']*$", url):
            ok = True
        out[key] = url if ok else ""
    out["closed"] = bool(src.get("closed"))
    return out


def load_polls(data_dir) -> dict:
    """data/polls.json, sanitised; an empty document when it is missing."""
    path = Path(data_dir) / "polls.json"
    doc = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    polls = [sanitize_poll(p) for p in (doc.get("polls") or []) if isinstance(p, dict)]
    return {"intro": str(doc.get("intro", "") or "")[:2000], "polls": polls}


def _poll_open(poll: dict, today: datetime.date) -> bool:
    if poll.get("closed"):
        return False
    opens, closes = poll.get("opens", ""), poll.get("closes", "")
    try:
        if opens and today < datetime.date.fromisoformat(opens):
            return False
        if closes and today > datetime.date.fromisoformat(closes):
            return False
    except ValueError:
        return True
    return True


def _poll_date_text(poll: dict) -> str:
    def fmt(value):
        try:
            return datetime.date.fromisoformat(value).strftime("%B %-d, %Y")
        except ValueError:
            return value
    opens, closes = poll.get("opens", ""), poll.get("closes", "")
    if opens and closes:
        return f"Open {fmt(opens)} to {fmt(closes)}"
    if opens:
        return f"Opened {fmt(opens)}"
    if closes:
        return f"Closes {fmt(closes)}"
    return ""


POLLS_CSS = """<style>
/* Every colour here comes from the site's theme variables, so Polls looks
   right in Classic, Parchment, Ink and Midnight alike. */
.poll{border:1px solid var(--rule);border-radius:6px;padding:18px 20px;margin:16px 0;background:var(--paper2);color:var(--ink2)}
.poll h3{margin:0 0 4px;font-size:22px;color:var(--ink)}
.poll-q{margin:0 0 8px;font-size:17px;color:var(--ink)}
.poll-details{color:var(--ink2);margin:0 0 8px}
.poll-when{font:12px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin:0 0 10px;letter-spacing:.3px}
.poll-note{font:12px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin:0 0 12px}
.poll-tabs{display:flex;gap:6px;margin:0 0 12px}
.poll-tabs button{border:1px solid var(--rule);background:var(--paper);border-radius:4px;padding:6px 14px;font:12px "Helvetica Neue",Arial,sans-serif;cursor:pointer;color:var(--ink2)}
.poll-tabs button.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.poll-pane[hidden]{display:none}
.poll-counts{font:13px "Helvetica Neue",Arial,sans-serif;color:var(--ink2);margin:12px 0 0}
.poll-bars{margin:0 0 10px}
.pb-row{display:flex;align-items:center;gap:10px;margin:0 0 6px}
.pb-label{flex:0 0 96px;font:11px "Helvetica Neue",Arial,sans-serif;letter-spacing:.5px;text-transform:uppercase;color:var(--muted)}
.pb-bar{flex:1;display:flex;height:15px;border-radius:3px;overflow:hidden;background:var(--rule2);min-width:80px}
.pb-bar i{display:block;height:100%}
.pb-yes{background:#2e8b57}
.pb-no{background:#b0463c}
.pb-num{flex:0 0 auto;font:11px "Helvetica Neue",Arial,sans-serif;color:var(--ink2);white-space:nowrap}
.poll-total{font:12px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin:0}
.poll-counts b{font-size:15px}
.poll-form iframe{width:100%;max-width:640px;height:840px;border:1px solid var(--rule);border-radius:6px;background:var(--paper)}
@media (max-width:640px){.poll-form iframe{height:980px}}
.poll-links{margin:8px 0 0;font:13px "Helvetica Neue",Arial,sans-serif}
.poll-links a{display:inline-block;margin:0 10px 6px 0}
.pc-item{padding:8px 0;border-bottom:1px solid var(--rule2);margin:0;font-size:15px}
.pc-vote{font:700 11px "Helvetica Neue",Arial,sans-serif;letter-spacing:.6px;text-transform:uppercase;color:#1a7f37;margin-right:8px}
.pc-vote.no{color:#a1261e}
.pc-signed{font:11px "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin-left:8px}
.pc-empty{color:var(--muted);font-style:italic}
body[data-theme="ink"] .pb-yes,body[data-theme="midnight"] .pb-yes{background:#4fbf82}
body[data-theme="ink"] .pb-no,body[data-theme="midnight"] .pb-no{background:#e07a6f}
body[data-theme="ink"] .pc-vote,body[data-theme="midnight"] .pc-vote{color:#7ee2a8}
body[data-theme="ink"] .pc-vote.no,body[data-theme="midnight"] .pc-vote.no{color:#ff9d9d}
</style>"""


def place_polls(body_html: str, polls_doc: dict | None = None) -> tuple[str, str]:
    """Swap `[poll list]` for the poll sections and their live-count script."""
    marker_re = re.compile(r"<(?:p|li)[^>]*>\s*\[poll list\]\s*</(?:p|li)>", re.I)
    if not marker_re.search(body_html):
        return body_html, ""
    doc = polls_doc or POLL_DEFAULTS
    today = datetime.date.today()
    parts = []
    form_count = 0
    for poll in doc.get("polls", []):
        pid = html.escape(poll.get("id") or "poll", quote=True)
        title = html.escape(poll.get("title") or "Poll")
        question = html.escape(poll.get("question") or "")
        details = html.escape(poll.get("details") or "")
        is_open = _poll_open(poll, today)
        status = "Voting is open" if is_open else "Voting is closed"
        when = _poll_date_text(poll)
        decision = html.escape(poll.get("decision") or "")
        form = poll.get("anon_form") or ""
        signed = poll.get("signed_form") or ""
        if form:
            embed = html.escape(form + ("&" if "?" in form else "?") + "embedded=true", quote=True)
            loading = ' loading="lazy"' if form_count else ""
            form_count += 1
            form_html = (f'<div class="poll-form"><iframe src="{embed}"{loading} '
                         'title="Vote form" allowfullscreen></iframe></div>')
        else:
            form_html = '<p class="pc-empty">The voting form is not connected yet.</p>'
        links = []
        if signed:
            links.append(f'<a href="{html.escape(signed, quote=True)}" target="_blank" rel="noopener">'
                         'Vote with a Google sign-in</a>')
        if form:
            links.append(f'<a href="{html.escape(form, quote=True)}" target="_blank" rel="noopener">'
                         'Open the anonymous form in its own tab</a>')
        links_html = f'<p class="poll-links">{" · ".join(links)}</p>' if links else ""
        decision_html = f'<p class="poll-details"><b>Decision:</b> {decision}</p>' if decision else ""
        parts.append(
            f'<section class="poll" data-poll="{pid}" data-anon="{html.escape(poll.get("anon_csv") or "", quote=True)}"'
            f' data-signed="{html.escape(poll.get("signed_csv") or "", quote=True)}"'
            f' data-dev="{html.escape(poll.get("dev_csv") or "", quote=True)}"'
            f' data-fin="{html.escape(poll.get("finance_csv") or "", quote=True)}"'
            f' data-user="{html.escape(poll.get("user_csv") or "", quote=True)}">'
            f'<h3>{title}</h3>'
            f'<p class="poll-details">{details}</p>'
            f'<p class="poll-q">{question}</p>'
            f'<p class="poll-when">{html.escape(status)}{(" · " + html.escape(when)) if when else ""}</p>'
            '<p class="poll-note">All votes and comments are public.</p>'
            '<div class="poll-tabs"><button type="button" class="on" data-ptab="vote">Vote</button>'
            '<button type="button" data-ptab="comments">Comments</button></div>'
            f'<div class="poll-pane" data-ppane="vote">{form_html}{links_html}</div>'
            '<div class="poll-pane" data-ppane="comments" hidden><p class="pc-empty">No comments yet.</p></div>'
            f'{decision_html}'
            '<p class="poll-counts" data-pcounts>Counts load once the form is connected.</p>'
            '</section>')
    if not parts:
        parts.append('<p class="pc-empty">No polls are up right now.</p>')
    intro = f'<p>{html.escape(doc.get("intro"))}</p>' if doc.get("intro") else ""
    markup = POLLS_CSS + intro + "".join(parts)
    script = r"""<script>(function(){
function csv(t){var r=[],row=[],f="",i=0;for(;i<t.length;i++){var c=t[i];
if(c=='"'){if(t[i+1]=='"'){f+='"';i++;}else{var q=true;i++;while(i<t.length&&q){if(t[i]=='"'){if(t[i+1]=='"'){f+='"';i++;}else{q=false;}i++;}else{f+=t[i];i++;}}i--;}}
else if(c==","){row.push(f);f="";}else if(c=="\n"){row.push(f);r.push(row);row=[];f="";}else if(c!="\r"){f+=c;}}
if(f.length||row.length){row.push(f);r.push(row);}return r;}
function pick(rows){var out=[];for(var i=1;i<rows.length;i++){var R=rows[i];if(!R||!R.length)continue;
var vote=(R[1]||"").trim().toLowerCase();var comment=(R[2]||"").trim();out.push({vote:vote,comment:comment});}return out;}
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
var secs=document.querySelectorAll("section.poll");
function bar(label,yes,no){
  var total=yes+no||1,py=Math.round(yes*100/total),pn=100-py;
  return '<div class="pb-row"><span class="pb-label">'+esc(label)+'</span>'+
    '<span class="pb-bar"><i class="pb-yes" style="width:'+py+'%"></i>'+
    '<i class="pb-no" style="width:'+pn+'%"></i></span>'+
    '<span class="pb-num">'+yes+' yes · '+no+' no</span></div>';
}
function tally(rows){
  var yes=0,no=0;
  rows.forEach(function(r){
    if(r.vote.indexOf("yes")===0)yes++;
    else if(r.vote.indexOf("no")===0)no++;
  });
  return {yes:yes,no:no};
}
secs.forEach(function(sec){
  var counts=sec.querySelector("[data-pcounts]"),paneC=sec.querySelector('[data-ppane="comments"]');
  var pools=[{k:"dev",label:"Developers",u:sec.getAttribute("data-dev")},
             {k:"fin",label:"Financial",u:sec.getAttribute("data-fin")},
             {k:"usr",label:"Users",u:sec.getAttribute("data-user")}];
  var extra=[{k:"anon",label:"Anonymous",u:sec.getAttribute("data-anon"),signed:false},
             {k:"signed",label:"Signed in",u:sec.getAttribute("data-signed"),signed:true}];
  var anyPool=pools.some(function(x){return x.u;});
  var sources=(anyPool?pools:extra).filter(function(x){return x.u;});
  if(!sources.length)return;
  Promise.all(sources.map(function(x){
    return fetch(x.u,{cache:"no-store"})
      .then(function(r){if(!r.ok)throw new Error("bad");return r.text();})
      .then(function(t){return pick(csv(t)).map(function(v){v.pool=x.k;v.signed=!!x.signed;return v;});})
      .catch(function(){return null;});
  })).then(function(all){
    if(all.some(function(a){return a===null;})){
      counts.textContent="Counts could not load. Open the form or sheet directly to see them.";
      return;
    }
    var rows=[].concat.apply([],all),t=tally(rows),signed=0;
    rows.forEach(function(r){if(r.signed)signed++;});
    var onlyAnon=!anyPool&&sources.length===1&&sources[0].k==="anon";
    var html="";
    if(anyPool){
      pools.forEach(function(p){
        var mine=rows.filter(function(r){return r.pool===p.k;});
        if(!mine.length)return;
        var pt=tally(mine);
        html+=bar(p.label,pt.yes,pt.no);
      });
    }
    html+=bar(onlyAnon?"Anonymous":(anyPool?"Everyone":"Result"),t.yes,t.no);
    counts.innerHTML='<div class="poll-bars">'+html+'</div><p class="poll-total">'+
      (onlyAnon ? rows.length+' total'
                : t.yes+' yes · '+t.no+' no · '+rows.length+' total'+
                  ' · '+signed+' signed in, '+(rows.length-signed)+' not signed in')+'</p>';
    var withC=rows.filter(function(r){return r.comment;});
    if(withC.length){
      paneC.innerHTML=withC.map(function(r){
        var no=r.vote.indexOf("no")===0;
        return '<p class="pc-item"><span class="pc-vote'+(no?' no':'')+'">'+(no?"No":"Yes")+'</span>'+
          esc(r.comment)+(onlyAnon?'':(r.signed?'<span class="pc-signed">signed in</span>':'<span class="pc-signed">not signed in</span>'))+'</p>';
      }).join("");
    }
  });
});
document.addEventListener("click",function(e){var b=e.target.closest("[data-ptab]");if(!b)return;var sec=b.closest("section.poll");sec.querySelectorAll("[data-ptab]").forEach(function(x){x.classList.toggle("on",x===b);});var want=b.getAttribute("data-ptab");sec.querySelectorAll("[data-ppane]").forEach(function(p){p.hidden=p.getAttribute("data-ppane")!==want;});});
})();</script>"""
    return marker_re.sub(lambda m: markup, body_html), script


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


def nav_html(pages, current: str, root: str = "") -> str:
    """The menu. `root` is "" for normal pages and "/" for the 404 page,
    which GitHub Pages serves at whatever path the visitor mistyped, so
    its links must be absolute to reach the site root."""
    if len([p for p in pages if not p.get("hidden")]) <= 1:
        return ""
    parts = []
    for p in [p for p in pages if not p.get("hidden")]:
        if p["slug"] == current:
            parts.append(f'<span class="cur">{html.escape(p["nav"])}</span>')
        else:
            parts.append(
                f'<a href="{root if root and p["slug"] == "index" else root + html.escape(p["slug"]) + ".html"}">'
                f'{html.escape(p["nav"])}</a>')
    return '  <nav class="sitenav">\n    ' + "\n    ".join(parts) + "\n  </nav>\n"


# ---------------------------------------------------------------------------
# FC-OPTIMIZE v1 -- shared, cacheable assets.
# The big stylesheet and the star-burst script used to be pasted into every
# page (about 40 KB each time) and the logo was embedded as base64 on every
# page. They now ship once as site/assets/site.css, site/assets/site.js and
# site/logo.png; browsers download them on the first visit and reuse them on
# every other page. The ?v= stamp changes whenever their content changes, so
# visitors never see a stale copy after a push.
# ---------------------------------------------------------------------------
import hashlib as _fc_hashlib

SITE_JS = r'''(function () {
  "use strict";

  // ---- Theme ---------------------------------------------------------------
  // The visitor's choice follows them across pages. Only the storage calls
  // are guarded (private mode can throw); any real script error reaches the
  // console instead of being swallowed.
  var THEMES = ["classic", "parchment", "ink", "midnight"];
  var body = document.body;
  var defaultTheme = body.dataset.theme || "classic";

  function savedTheme() {
    try { return localStorage.getItem("fc-theme"); } catch (err) { return null; }
  }
  function saveTheme(name) {
    try { localStorage.setItem("fc-theme", name); } catch (err) { /* not fatal */ }
  }

  var saved = savedTheme();
  if (THEMES.indexOf(saved) !== -1) { body.dataset.theme = saved; }

  var themeButton = document.getElementById("theme-toggle");
  function labelThemeButton() {
    if (themeButton) {
      themeButton.textContent = body.dataset.theme === "midnight" ? "Light mode" : "Dark mode";
    }
  }
  labelThemeButton();
  if (themeButton) {
    themeButton.addEventListener("click", function () {
      var next = body.dataset.theme === "midnight" ? defaultTheme : "midnight";
      body.dataset.theme = next;
      saveTheme(next);
      labelThemeButton();
    });
  }

  // ---- Arrival flash -------------------------------------------------------
  // The burst sets fcWarp just before it navigates; the next page plays one
  // soft ink flash so the arrival feels joined to the click.
  var warped = false;
  try {
    warped = sessionStorage.getItem("fcWarp") === "1";
    if (warped) { sessionStorage.removeItem("fcWarp"); }
  } catch (err) { /* storage unavailable: skip the flash */ }
  if (warped) {
    var arrive = document.createElement("div");
    arrive.id = "arriveflash";
    document.body.appendChild(arrive);
    document.body.classList.add("arrive");
    setTimeout(function () {
      document.body.classList.remove("arrive");
      if (arrive.parentNode) { arrive.parentNode.removeChild(arrive); }
    }, 700);
  }

  // ---- Live GitHub repository strips --------------------------------------
  // The build writes a .gh-repo shell wherever a page uses the marker
  // [github-stats:owner/repo]. The stats come from GitHub's public API here,
  // in the browser: no token, no server, no build-time network. Responses
  // are cached for half an hour so a reader moving between pages does not
  // spend the anonymous rate limit. All repository text is written with
  // textContent, never innerHTML.
  (function () {
    var strips = document.querySelectorAll("[data-gh-repo]");
    if (!strips.length) { return; }

    var API = "https://api.github.com";
    var TTL = 30 * 60 * 1000;

    function cacheKey(repo) { return "fc-gh:" + repo; }
    function readCache(repo) {
      try {
        var raw = localStorage.getItem(cacheKey(repo));
        if (!raw) { return null; }
        var doc = JSON.parse(raw);
        if (!doc || typeof doc.t !== "number" || !doc.data) { return null; }
        return doc;
      } catch (err) { return null; }
    }
    function writeCache(repo, data) {
      try {
        localStorage.setItem(cacheKey(repo), JSON.stringify({ t: Date.now(), data: data }));
      } catch (err) { /* private mode: live stats still work this visit */ }
    }
    function getJSON(url) {
      return fetch(url, { headers: { "Accept": "application/vnd.github+json" } })
        .then(function (response) {
          if (response.status === 404) { return null; }
          if (!response.ok) { throw new Error("GitHub " + response.status); }
          return response.json();
        });
    }
    function setText(root, name, text) {
      var el = root.querySelector('[data-gh="' + name + '"]');
      if (el) { el.textContent = text; }
    }
    function setTitle(root, name, text) {
      var el = root.querySelector('[data-gh="' + name + '"]');
      if (el) { el.title = text || ""; }
    }
    function relative(iso) {
      if (!iso) { return ""; }
      var then = Date.parse(iso);
      if (isNaN(then)) { return ""; }
      var seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
      if (seconds < 60) { return "just now"; }
      var minutes = Math.round(seconds / 60);
      if (minutes < 60) { return minutes + " min ago"; }
      var hours = Math.round(minutes / 60);
      if (hours < 24) { return hours + (hours === 1 ? " hour ago" : " hours ago"); }
      var days = Math.round(hours / 24);
      if (days < 30) { return days + (days === 1 ? " day ago" : " days ago"); }
      try {
        return new Date(then).toLocaleDateString(undefined, {
          month: "short", day: "numeric", year: "numeric"
        });
      } catch (err) { return ""; }
    }
    function shortLine(text, limit) {
      var line = String(text || "").split("\n")[0].trim();
      return line.length > limit ? line.slice(0, limit - 1).trim() + "\u2026" : line;
    }
    function safeGithub(url) {
      return /^https:\/\/github\.com\//.test(String(url || "")) ? url : "";
    }
    function paint(root, doc) {
      var data = doc.data || {};
      var repoData = data.repo || {};
      var commit = data.commits && data.commits[0];
      var release = data.release;
      var stars = typeof repoData.stargazers_count === "number" ? repoData.stargazers_count : null;
      var issues = typeof repoData.open_issues_count === "number" ? repoData.open_issues_count : null;

      setText(root, "stars", stars === null ? "\u2014" : String(stars));
      setText(root, "stars-unit", stars === 1 ? "star" : "stars");
      setText(root, "issues", issues === null ? "\u2014" : String(issues));
      setText(root, "issues-unit", issues === 1 ? "open issue" : "open issues");

      if (commit) {
        var commitData = commit.commit || {};
        var author = commitData.author || commitData.committer || {};
        var message = shortLine(commitData.message, 64) || "No commit message";
        setText(root, "commit", message);
        setTitle(root, "commit", String(commitData.message || "").split("\n")[0].trim());
        setText(root, "commit-when", relative(author.date));
        var commitLink = root.querySelector('[data-gh="commit-link"]');
        if (commitLink) {
          var commitHref = safeGithub(commit.html_url);
          if (commitHref) { commitLink.href = commitHref; }
        }
      } else {
        setText(root, "commit", "No commits yet");
        setText(root, "commit-when", "");
      }

      if (release) {
        var releaseName = shortLine(release.tag_name || release.name, 32) || "Latest release";
        setText(root, "release", releaseName);
        setTitle(root, "release", release.tag_name || release.name || "");
        setText(root, "release-when", relative(release.published_at));
        var releaseLink = root.querySelector('[data-gh="release-link"]');
        if (releaseLink) {
          var releaseHref = safeGithub(release.html_url);
          if (releaseHref) { releaseLink.href = releaseHref; }
        }
      } else {
        setText(root, "release", "No releases yet");
        setText(root, "release-when", "");
      }
      root.dataset.state = "ready";
    }
    function fallback(root) {
      root.dataset.state = "error";
      setText(root, "commit", "Live stats unavailable");
      setText(root, "commit-when", "");
      setText(root, "release", "View on GitHub");
      setText(root, "release-when", "");
    }
    function load(root) {
      var repo = root.getAttribute("data-gh-repo") || "";
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) { return; }
      if (!window.fetch) { fallback(root); return; }
      root.dataset.state = "loading";
      var cached = readCache(repo);
      if (cached) { paint(root, cached); }
      if (cached && Date.now() - cached.t < TTL) { return; }

      var base = API + "/repos/" + repo;
      Promise.all([
        getJSON(base),
        getJSON(base + "/commits?per_page=1"),
        getJSON(base + "/releases?per_page=1")
      ]).then(function (parts) {
        if (!parts[0]) { throw new Error("repository not found"); }
        var next = {
          repo: parts[0],
          commits: parts[1] || [],
          release: (parts[2] && parts[2][0]) || null
        };
        writeCache(repo, next);
        paint(root, { t: Date.now(), data: next });
      }).catch(function () {
        if (!cached) { fallback(root); }
      });
    }
    for (var i = 0; i < strips.length; i++) { load(strips[i]); }
  })();

  // ---- Star / Feedback burst -----------------------------------------------
  var badge = document.querySelector("a.star-badge, a.gem-badge");
  if (!badge || !badge.getAttribute("href")) { return; }

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  badge.addEventListener("click", function (event) {
    var href = badge.getAttribute("href");
    if (!href || href.charAt(0) === "#") { return; }
    event.preventDefault();
    if (reduceMotion) { window.location.href = href; return; }
    if (badge.classList.contains("warp")) { return; }

    var rect = badge.getBoundingClientRect();
    var x = rect.left + rect.width / 2;
    var y = rect.top + rect.height / 2;

    try { sessionStorage.setItem("fcWarp", "1"); } catch (err) { /* flash optional */ }

    badge.classList.add("warp");
    document.body.classList.add("warping");

    var left = false;
    function go() {
      if (left) { return; }
      left = true;
      window.location.href = href;
    }
    setTimeout(go, 560);
    setTimeout(go, 1100);
    setTimeout(function () {
      document.body.classList.remove("warping");
      badge.classList.remove("warp");
      var layer = document.getElementById("fx");
      if (layer && layer.parentNode) { layer.parentNode.removeChild(layer); }
    }, 2200);

    try {
      drawBurst(x, y);
    } catch (err) {
      // The burst is decoration; the navigation timers above still run.
      console.error("star burst failed", err);
    }
  });

  function drawBurst(x, y) {
    var layer = document.createElement("div");
    layer.id = "fx";
    var flash = document.createElement("div");
    flash.className = "comicflash";
    layer.appendChild(flash);
    var canvas = document.createElement("canvas");
    layer.appendChild(canvas);
    document.body.appendChild(layer);

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = window.innerWidth;
    var H = window.innerHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);

    var count = W < 700 ? 18 : 30;
    var shards = [];
    for (var i = 0; i < count; i++) {
      var angle = Math.random() * Math.PI * 2;
      var speed = 3 + Math.random() * 6;
      shards.push({
        x: x, y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        r: 4 + Math.random() * 7,
        rot: Math.random() * Math.PI,
        spin: (Math.random() - 0.5) * 0.4,
        age: 0,
        life: 26 + Math.random() * 22,
        ink: Math.random() < 0.5
      });
    }

    var rayCount = W < 700 ? 12 : 18;
    var raySeeds = [];
    for (var j = 0; j < rayCount; j++) { raySeeds.push(0.6 + Math.random() * 0.5); }

    var wax = { age: 0, life: 24 };
    var started = performance.now();
    requestAnimationFrame(frame);

    function frame() {
      var alive = false;
      ctx.clearRect(0, 0, W, H);

      // The sealed wax burst: a jagged white star with black-and-white rays.
      if (wax.age <= wax.life) {
        wax.age++;
        alive = true;
        var progress = Math.min(1, wax.age / wax.life);
        var radius = Math.min(W, H) * 0.08 + progress * 44;
        ctx.globalAlpha = 1 - progress * 0.3;
        ctx.fillStyle = "#fffdf7";
        ctx.strokeStyle = "#111";
        ctx.lineWidth = 5;
        ctx.beginPath();
        for (var k = 0; k < 16; k++) {
          var a = k * Math.PI / 8;
          var rr = radius * (1 + 0.22 * Math.sin(a * 3 + wax.age * 0.5));
          var px = x + Math.cos(a) * rr;
          var py = y + Math.sin(a) * rr;
          if (k) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); }
        }
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        for (var m = 0; m < rayCount; m++) {
          var ra = m * Math.PI * 2 / rayCount;
          var length = (0.3 + 0.16 * raySeeds[m]) * Math.max(W, H) * progress;
          ctx.beginPath();
          ctx.moveTo(x + Math.cos(ra) * radius * 0.7, y + Math.sin(ra) * radius * 0.7);
          ctx.lineTo(x + Math.cos(ra) * length, y + Math.sin(ra) * length);
          ctx.strokeStyle = m % 2 ? "#111" : "#fffdf7";
          ctx.lineWidth = m % 2 ? 9 : 4;
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }

      // Ink shards, half filled and half outlined, flying out from the click.
      for (var s = 0; s < shards.length; s++) {
        var shard = shards[s];
        shard.age++;
        if (shard.age > shard.life) { continue; }
        alive = true;
        shard.x += shard.vx;
        shard.y += shard.vy;
        shard.vx *= 0.97;
        shard.vy *= 0.97;
        shard.rot += shard.spin;
        ctx.globalAlpha = Math.max(0, 1 - shard.age / shard.life);
        ctx.save();
        ctx.translate(shard.x, shard.y);
        ctx.rotate(shard.rot);
        ctx.beginPath();
        ctx.moveTo(0, -shard.r);
        ctx.lineTo(shard.r * 0.7, shard.r);
        ctx.lineTo(-shard.r * 0.7, shard.r);
        ctx.closePath();
        if (shard.ink) {
          ctx.fillStyle = "#111";
          ctx.fill();
        } else {
          ctx.fillStyle = "#fffdf7";
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = "#111";
          ctx.stroke();
        }
        ctx.restore();
      }

      ctx.globalAlpha = 1;
      if (alive && performance.now() - started < 1200) {
        requestAnimationFrame(frame);
      } else if (layer.parentNode) {
        layer.parentNode.removeChild(layer);
      }
    }
  }
})();
'''

# Tiny inline snippet: applies the saved light/dark choice before the first
# paint so Midnight visitors never see a white flash while site.js loads.
THEME_BOOT_TAG = ("<script>try{var t=localStorage.getItem('fc-theme');"
                  "if(t==='midnight'||t==='classic'||t==='parchment'||t==='ink')"
                  "document.body.dataset.theme=t}catch(_){}</script>\n")


def minify_css(css: str) -> str:
    """Conservative minifier: drops comments, indentation and blank lines.
    Never touches anything inside a rule, so selectors/values stay intact."""
    out = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    lines = [ln.strip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln) + "\n"


SITE_CSS_MIN = minify_css(CSS)
ASSET_VERSION = _fc_hashlib.sha256(
    (SITE_CSS_MIN + "\0" + SITE_JS).encode("utf-8")).hexdigest()[:10]
FINANCE_VERSION = _fc_hashlib.sha256(
    page_extras.FINANCE_JS.encode("utf-8")).hexdigest()[:10]
SITE_JS_TAG = f'<script src="assets/site.js?v={ASSET_VERSION}"></script>'


def asset_uri(name: str, path: Path) -> str:
    """A local asset name with a content stamp, so a changed icon is not cached."""
    try:
        return f"{name}?v={_fc_hashlib.sha256(path.read_bytes()).hexdigest()[:10]}"
    except OSError:
        return name


FAVICON_URI = asset_uri("favicon.png", DEFAULT_FAVICON)
TOUCH_URI = asset_uri("apple-touch-icon.png", DEFAULT_TOUCH_ICON)


def css_link(prefix: str = "") -> str:
    return f'<link rel="stylesheet" href="{prefix}assets/site.css?v={ASSET_VERSION}">'


def custom_style_block(custom_css) -> str:
    extra = safe_custom_css(custom_css or "")
    return f"<style>\n{extra}\n</style>\n" if extra.strip() else ""


# FC-PICTURE-FALLBACK: older browsers (Safari on macOS 10.15 and earlier,
# old iPads) cannot show WebP. When uploads/ holds a same-named PNG/JPG/GIF
# next to a .webp, pages get <picture> so modern browsers take the small
# WebP and older ones take the fallback.
_UPLOAD_NAMES: set = set()
_FALLBACK_EXTS = (".png", ".jpg", ".jpeg", ".gif")


def picture_fallbacks(fragment: str, names=None) -> str:
    names = _UPLOAD_NAMES if names is None else names

    def wrap(m):
        tag = m.group(0)
        hit = re.search(r'\bsrc="uploads/([^"/]+)\.webp"', tag)
        if not hit:
            return tag
        stem = hit.group(1)
        fb = next((stem + e for e in _FALLBACK_EXTS if stem + e in names), None)
        if not fb:
            return tag
        img = tag.replace(f'src="uploads/{stem}.webp"', f'src="uploads/{fb}"', 1)
        return (f'<picture><source srcset="uploads/{stem}.webp" type="image/webp">'
                f'{img}</picture>')

    return re.sub(r"<img\b[^>]*>", wrap, fragment)


def lazy_images(fragment: str, eager_first: bool = False) -> str:
    """Page pictures load only when scrolled near and decode off the main
    thread. The first image on a page is the one most likely to be the LCP,
    so it loads eagerly with high priority instead of waiting for layout."""
    seen = [False]

    def sub(match):
        tag = match.group(0)
        if "loading=" in tag:
            return tag
        if eager_first and not seen[0]:
            seen[0] = True
            return tag.replace("<img ", '<img fetchpriority="high" decoding="async" ', 1)
        return tag.replace("<img ", '<img loading="lazy" decoding="async" ', 1)

    return re.sub(r"<img\b[^>]*>", sub, fragment)


_IMAGE_SIZES: dict = {}


def image_size(path: Path):
    """(width, height) from a PNG, GIF, JPEG or WebP header, else None.

    Stdlib only: the build reads the few header bytes it needs instead of
    pulling in an imaging library."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(262144)
    except OSError:
        return None
    if len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return (w, h) if w and h else None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return (w, h) if w and h else None
    if data[:2] == b"\xff\xd8":  # JPEG: walk to the frame header
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h) if w and h else None
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return (w, h)
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return (w, h)
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return (w, h) if w and h else None
    return None


def size_images(fragment: str, sizes=None) -> str:
    """Reserve each uploaded picture's space before it loads, so the page
    does not jump while the image arrives. Only adds the attributes when
    the author has not written them already."""
    sizes = _IMAGE_SIZES if sizes is None else sizes

    def add(m):
        tag = m.group(0)
        if re.search(r"\b(?:width|height)=", tag):
            return tag
        hit = re.search(r'\bsrc="uploads/([^"/]+)"', tag)
        if not hit:
            return tag
        dim = sizes.get(hit.group(1))
        if not dim:
            return tag
        w, h = dim
        return tag[:-1].rstrip() + f' width="{w}" height="{h}">'

    return re.sub(r"<img\b[^>]*>", add, fragment)


def write_assets(out_dir: Path, logo_path: Path) -> str:
    """Write the shared assets; return the logo src for page markup."""
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    write_text_atomic(out_dir / "assets" / "site.css", SITE_CSS_MIN)
    write_text_atomic(out_dir / "assets" / "site.js", SITE_JS)
    write_text_atomic(out_dir / "assets" / "finance.js", page_extras.FINANCE_JS)
    chart_src = ROOT / "assets" / "chart.umd.min.js"
    if chart_src.is_file():
        shutil.copyfile(chart_src, out_dir / "assets" / "chart.umd.min.js")
    else:
        print("warning: assets/chart.umd.min.js is missing; Financials charts will not draw",
              file=sys.stderr)
    if DEFAULT_TOUCH_ICON.is_file():
        shutil.copyfile(DEFAULT_TOUCH_ICON, out_dir / "apple-touch-icon.png")
    else:
        print("warning: assets/apple-touch-icon.png is missing; link previews fall back",
              file=sys.stderr)
    raw = logo_path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{logo_path} is not a PNG file")
    (out_dir / "logo.png").write_bytes(raw)
    return "logo.png?v=" + _fc_hashlib.sha256(raw).hexdigest()[:10]


def render_page(title: str, tagline: str, footer: str, theme: str,
                pages, slug: str, body_html: str, logo_uri: str,
                description: str = "", custom_css: str = "",
                star_animation: str = STAR_ANIMATION_DEFAULT,
                form_html: str = "",
                cal_cfg: dict | None = None,
                polls_doc: dict | None = None,
                finance_html: str = "",
                giscus_html: str = "",
                favicon_uri: str = "",
                touch_icon_uri: str = "") -> str:
    favicon_uri = favicon_uri or FAVICON_URI
    touch_icon_uri = touch_icon_uri or TOUCH_URI
    page_meta = next((p for p in pages if p["slug"] == slug), {})
    home_title = re.sub(r"\s+&\s+", " AND ", title) if " & " in title else title
    sub_title = re.sub(r"\s+(?:AND|and)\s+", " & ", title) if re.search(r"\b(?:AND|and)\b", title) else title
    masthead_title = home_title if slug == "index" else sub_title
    # The browser title and search description come from data/site.json.
    # They are used exactly as written: each page title already ends with
    # " | Flamingo & Cactus", so the site name never appears twice.
    page_title = page_meta.get("title", "").strip() or title
    desc = page_meta.get("description", "").strip() or description.strip() or tagline.strip() or title
    canon = f"https://{CUSTOM_DOMAIN}/" if slug == "index" else f"https://{CUSTOM_DOMAIN}/{slug}.html"
    canvas = ""
    form_block = ""
    placed = body_html
    if any(p["slug"] == slug and p.get("form") for p in pages):
        # `[contact form]` typed anywhere in the page body marks the spot;
        # without it the form lands after the whole page body.
        placed, leftover = place_form(body_html, form_html or CONTACT_FORM_HTML)
        if leftover:
            form_block = f"  {leftover}\n"
    else:
        # The marker is reserved for the generated contact form; never let
        # the literal implementation token leak onto a page without a form.
        placed = re.sub(r"<p[^>]*>\s*\[contact form\]\s*</p>", "", body_html)
    placed, cal_script = place_calendar(placed, cal_cfg)
    placed, poll_script = place_polls(placed, polls_doc)
    placed, set_script = place_settings(placed, theme)
    placed = page_extras.inject_blocks(placed, slug, finance_html, giscus_html)
    placed = picture_fallbacks(lazy_images(placed, eager_first=True))
    placed = size_images(placed)
    # The page's own name is the <h1>. The home page already carries the
    # site name as its heading in the masthead; every other page gets a
    # real page heading here, so screen readers and search engines see
    # one <h1> per page instead of the site name repeated sixteen times.
    if "heading" in page_meta:
        page_heading = str(page_meta.get("heading") or "").strip()
    else:
        page_heading = "" if slug == "index" else next(
            (p["nav"] for p in pages if p["slug"] == slug), "")
    heading_html = (f'    <h1>{html.escape(page_heading)}</h1>\n'
                    if page_heading else "")
    if placed or heading_html:
        canvas = (f'  <div class="canvas">\n{heading_html}{placed}'
                  f"{cal_script}{poll_script}{set_script}\n  </div>\n")

    # Badges (the star and the Feedback gem) belong to the home page. On
    # every other page they ate the top of a phone screen before the logo.
    badges = f"{star_html(pages)}{gem_html(pages)}" if slug == "index" else ""
    body_attrs = (f' class="has-badges"' if badges else "")

    # The footer is the small print plus the links that no longer fit in
    # the menu: News, Feedback, Polls and Settings. The theme button is
    # wired by site.js (the same fc-theme key the Settings page writes).
    footer_text = html.escape(footer).strip()
    footer_links = " &middot; ".join(
        f'<a href="{html.escape(s)}.html">{html.escape(label)}</a>'
        for s, label in FOOTER_LINKS
        if any(p["slug"] == s for p in pages))
    footer_block = ('  <footer>\n'
                    + (f'    <p>{footer_text}</p>\n' if footer_text else "")
                    + (f'    <p class="footlinks">{footer_links}</p>\n' if footer_links else "")
                    + '    <button type="button" class="themetoggle" id="theme-toggle">Dark mode</button>\n'
                    + "  </footer>\n")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(page_title)}</title>\n"
        f'<meta name="description" content="{html.escape(desc, quote=True)}">\n'
        f'<link rel="canonical" href="{canon}">\n'
        f'<link rel="icon" type="image/png" href="{favicon_uri}">\n'
        f'<link rel="apple-touch-icon" sizes="512x512" href="{touch_icon_uri}">\n'
        f'<meta property="og:title" content="{html.escape(page_title, quote=True)}">\n'
        f'<meta property="og:description" content="{html.escape(desc, quote=True)}">\n'
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:url" content="{canon}">\n'
        f'<meta property="og:image" content="https://{CUSTOM_DOMAIN}/{touch_icon_uri}">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        + LD_ORG + "\n"
        + css_link() + "\n"
        + custom_style_block(custom_css) +
        "</head>\n"
        f'<body{body_attrs} data-theme="{theme}" data-star-animation="{STAR_ANIMATION_DEFAULT}">\n'
        f"{THEME_BOOT_TAG}"
        '<div class="page">\n'
        f"{badges}"
        "\n"
        '  <div class="masthead">\n'
        f'    <a class="logolink" href="index.html" aria-label="Go to the main page"><img src="{logo_uri}" alt="Flamingo and Cactus" width="96" height="96" fetchpriority="high"></a>\n'
        + (f"    <h1>{html.escape(masthead_title)}</h1>\n" if slug == "index"
           else f'    <p class="sitetitle">{html.escape(masthead_title)}</p>\n')
        + f"    <p class=\"tagline\">{html.escape(tagline)}</p>\n"
        "</div>\n"
        f"{nav_html(pages, slug)}"
        '  <div class="hr"></div>\n'
        "\n"
        f"{canvas}"
        f"{form_block}"
        f"{footer_block}"
        "</div>\n"
        # Starburst: the comic ink-and-paper burst is the only effect on this
        # site. Click the star, badge pops, a black-and-white impact frame
        # flashes, ink shards fly out, then the page reroutes. Returning to
        # a visited page plays the soft arrival flash + rise -- no portal,
        # no soft fade, no "no animation" mode. All transform, opacity, and
        # canvas only -- respects prefers-reduced-motion (skips the canvas).
        f"{SITE_JS_TAG}\n"
        "</body>\n"
        "</html>\n"
    )


NOTFOUND = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found &mdash; Flamingo &amp; Cactus</title>
<meta name="description" content="That page is not here. Use the menu or the link to the home page.">
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/favicon.png">
__CSSLINK__
<style>
__CSS__
</style>
</head>
<body data-theme="classic">
<div class="page">
  <div class="masthead">
    <a class="logolink" href="/" aria-label="Go to the main page"><img src="__LOGO__" alt="Flamingo &amp; Cactus" width="96" height="96"></a>
    <h1>Flamingo &amp; Cactus</h1>
    <p class="tagline">__TAGLINE__</p>
  </div>
__NAV__
  <div class="hr"></div>
  <main class="canvas">
    <h2>Page not found</h2>
    <p>The page you asked for isn't here. It may have moved, or the address may have a typo.</p>
    <p><a href="/">Go to the home page</a> &middot; <a href="/contact.html">Contact us</a></p>
  </main>
  <footer>
  </footer>
</div>
</body>
</html>
"""


# FormSubmit redirect target (the _next field on the contact form): the
# thank-you page for visitors with JavaScript off, whose classic submit
# navigates straight here after FormSubmit accepts the post. It also keeps
# emitting the old "flamingo-cf-ack" postMessage so a cached copy of the
# earlier hidden-iframe version of the contact page still finishes cleanly.
# Error pages (Cloudflare 521 and friends) never redirect, so this page can
# only be reached on the happy path.
CONTACT_ACK = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Message sent &mdash; Flamingo &amp; Cactus</title>
<meta name="robots" content="noindex">
__CSSLINK__
<style>
__CSS__
</style>
</head>
<body data-theme="classic">
<div class="page">
  <div class="masthead">
    <img src="__LOGO__" alt="Flamingo and Cactus">
    <h1>Flamingo &amp; Cactus</h1>
  </div>
  <div class="hr"></div>
  <main>
    <h2>It went through.</h2>
    <p>Your message is in our inbox. We read everything &mdash; thank you!</p>
    <p>You can close this page, or <a href="/contact.html">go back to the contact page</a>.</p>
  </main>
  <div class="hr"></div>
  <footer>
  </footer>
</div>
<script>
try{if(window.parent&&window.parent!==window){window.parent.postMessage("flamingo-cf-ack","*");}}catch(_){}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Settings page: `[site settings]` marker paragraph swapped for the
# visitor-side panel. Themes ride the existing fc-theme localStorage key —
# the same one the star script already reads — so a pick here follows the
# visitor across every page (never onto the Flank ledger, which keeps its
# own look). "Site default" removes the
# key and falls back to the theme baked into every page.


SETTINGS_NAMES = {
    "classic": "Classic",
    "parchment": "Parchment",
    "ink": "Ink",
    "midnight": "Midnight",
}

SETTINGS_CHIPS = {
    "classic": ("#ffffff", "#d9d9d9", "#141414", "#0b2a8c", "#c9a227"),
    "parchment": ("#f7f1e1", "#b8ab88", "#2b2417", "#1a3a7c", "#c9a227"),
    "ink": ("#262220", "#6b6152", "#f5efe0", "#9db8ff", "#c9a227"),
    "midnight": ("#221c2c", "#4d3c6b", "#f0e9f7", "#d3b8ff", "#c9a227"),
}

SETTINGS_DESCS = {
    "classic": "White paper, ink black",
    "parchment": "Aged paper, soft brown",
    "ink": "Charcoal slate, cream text",
    "midnight": "Deep violet night",
}


def settings_chip(theme: str) -> str:
    """A little palette preview: paper field, ink/link/gold dots."""
    paper, rule, ink, link, gold = SETTINGS_CHIPS[theme]
    return (
        f'<span class="setchip" style="background:{paper};border-color:{rule}">'
        f'<span class="setchip-dot" style="left:7px;top:7px;background:{ink}"></span>'
        f'<span class="setchip-dot" style="left:24px;top:15px;background:{link}"></span>'
        f'<span class="setchip-dot" style="right:7px;bottom:7px;background:{gold}"></span>'
        "</span>"
    )


def settings_panel_html(default_theme: str) -> str:
    cards = []
    for theme in ("classic", "parchment", "ink", "midnight"):
        cards.append(
            f'<button type="button" class="settheme" data-theme-choice="{theme}">\n'
            f'  {settings_chip(theme)}\n'
            f'  <span class="setname">{SETTINGS_NAMES[theme]}</span>\n'
            f'  <span class="setdesc">{SETTINGS_DESCS[theme]}</span>\n'
            "</button>"
        )
    cards.append(
        '<button type="button" class="settheme" data-theme-choice="">\n'
        '  <span class="setchip setchip-reset">\u21ba</span>\n'
        '  <span class="setname">Site default</span>\n'
        f'  <span class="setdesc">The site\u2019s own look \u2014 '
        f'{SETTINGS_NAMES.get(default_theme, default_theme)} right now</span>\n'
        "</button>"
    )
    return (
        '<div class="setpanel" id="site-settings">\n'
        '  <p class="setpanel-title">Site dynamics</p>\n'
        '  <p class="setpanel-sub">How the site looks on this device. '
        'Your choice is saved in this browser and follows you across every '
        'page.</p>\n'
        f'  <div class="setgrid">\n' + "\n".join(cards) + "\n  </div>\n"
        '  <p class="setsaved" id="set-saved" hidden></p>\n'
        "</div>"
    )


SETTINGS_SCRIPT = r'''
<script>
(function(){
var THEMES=["classic","parchment","ink","midnight"];
var NAMES={classic:"Classic",parchment:"Parchment",ink:"Ink",midnight:"Midnight"};
var DEFAULT="__DEFAULT__";
var body=document.body;
var panel=document.querySelector(".setpanel");
if(!panel)return;
var cards=panel.querySelectorAll(".settheme");
var note=document.getElementById("set-saved");
function saved(){var t=null;try{t=localStorage.getItem("fc-theme")}catch(_){}return THEMES.indexOf(t)>-1?t:null;}
function mark(){
  var cur=saved()||"";
  for(var i=0;i<cards.length;i++){
    var want=cards[i].getAttribute("data-theme-choice")||"";
    if(want===cur){cards[i].classList.add("set-cur");}
    else{cards[i].classList.remove("set-cur");}
  }
}
function label(){var tb=document.getElementById("theme-toggle");
  if(tb)tb.textContent=(body.getAttribute("data-theme")==="midnight")?"Light mode":"Dark mode";}
function say(msg){if(note){note.textContent=msg;note.hidden=false;}}
function pick(t){
  var was=saved();
  try{
    if(t){localStorage.setItem("fc-theme",t);}
    else{localStorage.removeItem("fc-theme");}
  }catch(_){}
  body.setAttribute("data-theme",t||DEFAULT);
  label();mark();
  var name=t?NAMES[t]:"the site default";
  if(t&&t===was){say(name+" is already on for this device.");}
  else{say("Saved \u2014 "+name+" is now on for this device.");}
}
for(var i=0;i<cards.length;i++){(function(c){
  c.addEventListener("click",function(){pick(c.getAttribute("data-theme-choice")||"");});
})(cards[i]);}
mark();
})();
</script>
'''


def place_settings(body_html: str, default_theme: str) -> tuple:
    """Swap a `[site settings]` marker for the panel.

    Returns (placed, script): script is '' when the marker is absent, so
    pages without it carry no settings code."""
    panel = settings_panel_html(default_theme)
    placed, n = re.subn(r"<p[^>]*>\s*\[site settings\]\s*</p>",
                        lambda _m: panel, body_html, count=1,
                        flags=re.IGNORECASE)
    if not n:
        return body_html, ""
    return placed, SETTINGS_SCRIPT.replace("__DEFAULT__", default_theme)


def read_body(pages_dir: Path, slug: str) -> str:
    p = pages_dir / f"{slug}.html"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def build(data_path: Path, pages_dir: Path, logo_path: Path,
          uploads_dir: Path, out_dir: Path, favicon_path: Path = None,
          write_back: bool = True):
    """Build every page into out_dir.

    write_back=True lets the sanitizer write cleaned bodies back into
    data/pages/. A check or CI build passes False: a verification run must
    never edit the sources it is verifying."""
    if favicon_path is None:
        favicon_path = logo_path
    site = load_site(data_path)
    form_html = contact_form_html(load_form_config(data_path.parent))
    cal_cfg = load_calendar_config(data_path.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    logo_uri = write_assets(out_dir, logo_path)
    favicon_uri = asset_uri("favicon.png", favicon_path)
    touch_uri = asset_uri("apple-touch-icon.png", DEFAULT_TOUCH_ICON)

    # The charts and the forum are generated once per build, not stored in
    # data/pages/. The ledger stays the only source: add a transaction on
    # the Desktop, sync it, rebuild, and both charts move.
    ledger_path = data_path.parent / "The Flank.md"
    finance_html = ""
    if ledger_path.is_file():
        try:
            finance_html = page_extras.finance_section_html(
                page_extras.load_finance(ledger_path), FINANCE_VERSION)
        except Exception as exc:
            print(f"warning: Financials charts skipped: {exc}", file=sys.stderr)
    giscus_html = page_extras.giscus_block(site.get("giscus"))
    _UPLOAD_NAMES.clear()
    _IMAGE_SIZES.clear()
    if uploads_dir.is_dir():
        _UPLOAD_NAMES.update(f.name for f in uploads_dir.iterdir() if f.is_file())
        for n in sorted(_UPLOAD_NAMES):
            stem = n[:-5]
            if n.lower().endswith(".webp") and not any(stem + e in _UPLOAD_NAMES for e in _FALLBACK_EXTS):
                print(f"warning: uploads/{n} has no PNG/JPG copy, so older Macs and iPads will not show it",
                      file=sys.stderr)
        for n in _UPLOAD_NAMES:
            if Path(n).suffix.lower() != ".pdf":
                dim = image_size(uploads_dir / n)
                if dim:
                    _IMAGE_SIZES[n] = dim

    for p in site["pages"]:
        raw = read_body(pages_dir, p["slug"])
        clean = sanitize(raw)
        if write_back and clean != raw:
            write_text_atomic(pages_dir / f"{p['slug']}.html", clean)
        write_text_atomic(out_dir / f"{p['slug']}.html",
            render_page(site["title"], site["tagline"], site["footer"],
        site["theme"], site["pages"], p["slug"], clean, logo_uri,
                        site.get("description", ""), site.get("custom_css", ""),
                        site.get("star_animation", "comic"),
                        form_html=form_html, cal_cfg=cal_cfg,
                        polls_doc=load_polls(pages_dir.parent),
                        finance_html=finance_html,
                        giscus_html=giscus_html,
                        favicon_uri=favicon_uri,
                        touch_icon_uri=touch_uri))
    nf = (NOTFOUND.replace("__CSSLINK__", css_link("/"))
          .replace("__CSS__", safe_custom_css(site.get("custom_css", "")))
          .replace("__LOGO__", "/" + logo_uri)
          .replace("__NAV__", nav_html(site["pages"], "", "/"))
          .replace("__TAGLINE__", html.escape(site["tagline"]))
          .replace('data-theme="classic"', f'data-theme="{site["theme"]}"'))
    write_text_atomic(out_dir / "404.html", nf)
    ck = (CONTACT_ACK.replace("__CSSLINK__", css_link("/"))
          .replace("__CSS__", safe_custom_css(site.get("custom_css", "")))
          .replace("__LOGO__", "/" + logo_uri)
          .replace('data-theme="classic"', f'data-theme="{site["theme"]}"'))
    write_text_atomic(out_dir / "contact-ack.html", ck)
    # A page deleted from site.json must stop being published. Earlier
    # builds never took anything down, so a removed page stayed live.
    keep = {f"{p['slug']}.html" for p in site["pages"]} | {"404.html", "contact-ack.html"}
    for stale in sorted(out_dir.glob("*.html")):
        if stale.name not in keep:
            stale.unlink()
    shutil.copyfile(favicon_path, out_dir / "favicon.png")
    write_text_atomic(out_dir / ".nojekyll", "")
    write_text_atomic(out_dir / "CNAME", CUSTOM_DOMAIN + "\n")
    write_text_atomic(out_dir / "robots.txt",
        f"User-agent: *\nAllow: /\nDisallow: /flank/\nSitemap: https://{CUSTOM_DOMAIN}/sitemap.xml\n")
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
            # Only pictures and PDFs are publishable. Anything else dropped
            # into assets/uploads (a script, an .html file, a key) stays local.
            if f.suffix.lower() not in UPLOAD_EXTS:
                print(f"warning: uploads/{f.name} is not a picture or PDF, so it is not published",
                      file=sys.stderr)
                continue
            shutil.copyfile(f, site_uploads / f.name)

    # The hidden ledger: data/The Flank.md -> site/flank/. Unlisted (not in
    # the nav, not in site.json) — the Financials page button is the only
    # link to it. Skipped with a warning when the ledger is absent so the
    # main pages still build.
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


def check(data_path: Path, pages_dir: Path, logo_path: Path, uploads_dir: Path, favicon_path: Path = None):
    tmp = Path(tempfile.mkdtemp(prefix="flamingo-site-check-"))
    try:
        site = build(data_path, pages_dir, logo_path, uploads_dir, tmp, favicon_path,
                     write_back=False)
        ok = True

        def expect(cond, msg):
            nonlocal ok
            print(("PASS  " if cond else "FAIL  ") + msg)
            ok = ok and cond

        idx = (tmp / "index.html").read_text(encoding="utf-8")
        # FC-OPTIMIZE v1: shared assets ship once instead of on every page.
        _css = (tmp / "assets" / "site.css").read_text(encoding="utf-8") \
            if (tmp / "assets" / "site.css").is_file() else ""
        _js = (tmp / "assets" / "site.js").read_text(encoding="utf-8") \
            if (tmp / "assets" / "site.js").is_file() else ""
        expect(bool(_css) and bool(_js) and (tmp / "logo.png").is_file(),
               "shared site.css, site.js and logo.png are built")
        expect("data:image" not in idx and "assets/site.css?v=" in idx
               and "assets/site.js?v=" in idx,
               "pages link the shared assets instead of embedding them")
        expect(len(idx.encode("utf-8")) < 30000, "home page HTML stays small (<30 KB)")
        idx = idx + "\n" + _css + "\n" + _js
        expect(len(idx) > 0, "index.html non-empty")
        expect(html.escape(site["title"]) in idx, "title present")
        expect('data-star-animation="comic"' in idx,
               "every generated page carries the comic star burst marker")
        page_meta = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                [{"slug":"index", "nav":"Home", "title":"Page-specific title",
                                  "description":"Page-specific search text"}], "index", "", "x",
                                site.get("description", ""))
        expect("Page-specific title" in page_meta and 'content="Page-specific search text"' in page_meta,
               "page-level browser title and search description override site defaults")
        style_test = render_page(site["title"], site["tagline"], site["footer"], site["theme"],
                                 site["pages"], "index", "", "x", site.get("description", ""),
                                 ".mobile-test{font-size:20px}")
        expect('.mobile-test{font-size:20px}' in style_test and 'data-star-animation="comic"' in style_test,
               "advanced CSS reaches the generated page; comic star burst is hard-coded")
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
        expect("body.has-badges .masthead{padding-top:112px}" in idx and
               "flex-wrap:wrap" in idx,
               "phone header: badges only on home, nav wraps without touching")
        expect("overflow-wrap:anywhere" in idx,
               "unbreakable titles wrap on phones (no sideways cutoff)")
        expect("badgepop" in idx and "arrivefade" in idx and "comicko" in idx,
               "comic only: badge pop, ink burst, and soft arrival flash all present")
        warp_scripts = idx.lower().count("<script")
        expect(warp_scripts == 3 and "ld+json" in idx.lower(),
               "built pages carry only the theme, burst and structured-data scripts")
        expect('name="description"' in idx and html.escape(
                   next((p.get("description") or site["description"])
                        for p in site["pages"] if p["slug"] == "index"))[:40] in idx,
               "every page carries its own search snippet")
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
        contact_built = ((tmp / "contact.html").read_text(encoding="utf-8") if (tmp / "contact.html").is_file() else "") + "\n" + _css
        if any(p.get("form") for p in site["pages"]):
            expect('action="https://formsubmit.co/' in contact_built, "contact page posts to the form inbox")
            expect('name="_next" value="https://flamingoandcactus.com/contact-ack.html"' in contact_built,
                   "FormSubmit redirects its answer to our own contact-ack.html (via its real _next field)")
            expect('name="_url"' not in contact_built,
                   "the bogus _url field is gone (FormSubmit never read it)")
            expect('id="cf-frame"' not in contact_built and
                   'form.target="cf-frame"' not in contact_built and
                   'ackPending=true' not in contact_built,
                   "no hidden-iframe second road: with JavaScript on, nothing posts to FormSubmit")
            ack_page = (tmp / "contact-ack.html").read_text(encoding="utf-8") \
                if (tmp / "contact-ack.html").is_file() else ""
            expect("flamingo-cf-ack" in ack_page and
                   'window.parent.postMessage("flamingo-cf-ack","*")' in ack_page and
                   "noindex" in ack_page and
                   "go back to the contact page" in ack_page,
                   "contact-ack.html is built: noindex page + cross-origin ack signal")
            expect('name="name"' in contact_built and 'name="note"' in contact_built, "contact form asks for name + note")
            expect("&quot;Random&quot;" in contact_built, "contact form notes Random is fine")
            expect('name="contact"' in contact_built, "contact form has one optional email-or-phone field")
            expect('name="_honey"' in contact_built, "contact form carries a spam honeypot")
            expect('name="sent_at"' in contact_built and "stampTime" in contact_built,
                   "contact form stamps the sender's local time on every submit")
            expect('type="file"' in contact_built and 'name="attachment"' in contact_built
                   and "MAX_BYTES=10000000" in contact_built,
                   "contact form takes files, capped at 10 MB")
            expect("formsubmit.co" not in idx, "form appears only on the flagged page, not home")
            # Disclaimer card + service attribution land on the live page.
            expect('class="cf-disclaimer"' in contact_built and
                   "About this form" in contact_built and
                   "FormInit" in contact_built and "FormSubmit" in contact_built and
                   "sends each note once" in contact_built and
                   "Web3Forms" not in contact_built and "as a last resort" not in contact_built,
                   "contact page carries the third-party disclaimer: one service, sent once")
            expect('class="cf-disclaimer"' not in idx,
                   "the disclaimer lives only on the contact page, not the home page")
            expect(contact_built.find('<form class="contactform"') != -1 and
                   contact_built.find('<form class="contactform"') <
                   contact_built.find('class="cf-disclaimer"'),
                   "contact page order: form first, then the About this form card")
            _fc = load_form_config(data_path.parent)
            expect(bool(_fc.get("forminit_form_id")),
                   "the one sending service is armed (FormInit form ID in data/form.json)")
            # Browser-side draft (localStorage) is wired in the runtime:
            # key set on input, restored on load, cleared when the note
            # goes through. Saves a failed send from losing the message.
            expect('"flamingo-cf-draft"' in contact_built and
                   "saveDraft" in contact_built and "restoreDraft" in contact_built and
                   "clearDraft" in contact_built and
                   'localStorage.setItem(DRAFT_KEY' in contact_built and
                   'localStorage.getItem(DRAFT_KEY)' in contact_built and
                   'localStorage.removeItem(DRAFT_KEY)' in contact_built,
                   "contact form saves its text fields to localStorage so a failed send never loses the message")
            # One send, one service. The page used to walk FormSubmit ->
            # Web3Forms -> FormInit and re-sent after ambiguous answers, so
            # one submit could land twice. Now a send makes exactly one
            # request, to FormInit (JSON verdict for text and files alike).
            # Present even with no form ID so the wiring is config-only.
            # No X-API-KEY anywhere: server keys must never be baked into a
            # public page.
            expect("'https://forminit.com/f/'" in contact_built and
                   "FORMINIT_ID" in contact_built and
                   "postFormInit" in contact_built and
                   "postFormInitFiles" in contact_built and
                   "fitsFormInit" in contact_built and "filePayload" in contact_built and
                   "FORMINIT_MAX_FILES=20" in contact_built and
                   "FORMINIT_MAX_BYTES=26214400" in contact_built and
                   "fi-file-attachment" in contact_built and
                   "fi-sender-email" in contact_built,
                   "contact form's runtime sends text and files through FormInit")
            expect("X-API-KEY" not in contact_built,
                   "no server API key is ever embedded in the public page")
            expect('headers:{Accept:"application/json"},body:fd' in contact_built and
                   "viaRedirect" in contact_built and "forminit\\.com\\/thank-you" in contact_built,
                   "file uploads ask FormInit for JSON and accept its thank-you redirect as delivery")
            expect(contact_built.count("fetch(FORMINIT_URL") == 2 and
                   "fetch(AJAX_URL" not in contact_built and
                   "fetch(WEB3FORMS_URL" not in contact_built and
                   "api.web3forms.com" not in contact_built and
                   "formsubmit.co/ajax" not in contact_built,
                   "a send makes one request to one service (no second service anywhere in the script)")
            expect("tryFailover" not in contact_built and "tryFormInit" not in contact_built and
                   "postFailover" not in contact_built and "viaFrame" not in contact_built and
                   "rescueText" not in contact_built and "postFormSubmitText" not in contact_built,
                   "no chain walkers or automatic re-send paths remain in the contact form script")
            expect("did not confirm the send" in contact_built and
                   "not retried automatically" in contact_built and
                   "unreadable" in contact_built,
                   "an unconfirmed send is announced and never re-sent automatically")
            # Stay-on-page guarantees while a send is in flight.
            expect('addEventListener("beforeunload"' in contact_built and
                   "setSending(true)" in contact_built and
                   ".cf-status.is-busy" in contact_built,
                   "sending keeps the visitor on the page: unload guard, progress bar")
            # No access key in the live page (the build never echoes the
            # user's secret back into the rendered HTML).
            form_cfg = load_form_config(data_path.parent)
            expect(form_cfg.get("web3forms_access_key", "") or
                   "web3forms_access_key" not in contact_built.lower() or
                   "web3forms_access_key=" not in contact_built,
                   "web3forms_access_key is server-side only, never echoed into the build")
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
            page_titles = {}
            for p in site["pages"]:
                text = (tmp / f"{p['slug']}.html").read_text(encoding="utf-8")
                m = re.search(r"<title>(.*?)</title>", text, re.S)
                page_titles[p["slug"]] = html.unescape(m.group(1)) if m else ""
            expect(all(page_titles.values()) and
                   len(set(page_titles.values())) == len(page_titles),
                   "every page has its own browser title (no shared default)")
            expect(all(t.count("Flamingo") == 1 for t in page_titles.values()),
                   "the site name appears once in every browser title")
        # build artifacts
        expect((tmp / "404.html").is_file(), "404.html built")
        nf_html = (tmp / "404.html").read_text(encoding="utf-8") if (tmp / "404.html").is_file() else ""
        expect("Page not found" in nf_html and "isn't here" in nf_html,
               "404 page says what happened instead of showing a blank shell")
        expect('href="/"' in nf_html and 'href="/contact.html"' in nf_html,
               "404 page links back to the home page and contact")
        expect('class="sitenav"' in nf_html and 'href="/products.html"' in nf_html,
               "404 page carries the normal menu, with absolute links (it is served at any path)")
        expect((tmp / "favicon.png").is_file(), "favicon.png built")
        expect((tmp / "apple-touch-icon.png").is_file(), "apple-touch-icon.png built")
        expect(f'rel="icon" type="image/png" href="{FAVICON_URI}"' in idx,
               "pages use the transparent favicon")
        expect('rel="apple-touch-icon"' in idx and
               f'property="og:image" content="https://flamingoandcactus.com/{TOUCH_URI}"' in idx,
               "link previews use the orange apple-touch icon, not the tab favicon")
        expect((tmp / ".nojekyll").is_file(), ".nojekyll built")
        expect((tmp / "CNAME").is_file(), "CNAME built")
        expect((tmp / "uploads").is_dir(), "site/uploads/ exists in build")
        expect(picture_fallbacks('<img class="secimg" src="uploads/a.webp" alt="">', {"a.webp", "a.png"}) ==
               '<picture><source srcset="uploads/a.webp" type="image/webp"><img class="secimg" src="uploads/a.png" alt=""></picture>',
               "WebP pictures with a PNG copy get an older-browser fallback")
        expect(picture_fallbacks('<img src="uploads/b.webp" alt="">', {"b.webp"}) == '<img src="uploads/b.webp" alt="">',
               "WebP pictures without a copy are left alone")
        expect(size_images('<img src="uploads/a.png" alt="">', {"a.png": (640, 480)}) ==
               '<img src="uploads/a.png" alt="" width="640" height="480">',
               "uploaded pictures get width and height so the page does not jump while loading")
        expect(size_images('<img src="uploads/a.png" width="10" height="20" alt="">', {"a.png": (640, 480)}) ==
               '<img src="uploads/a.png" width="10" height="20" alt="">',
               "image sizes written by hand are never overwritten")

        # FIX 1: hostile hrefs dropped, safe ones survive
        for bad_href in ("//evil.com/x", "/../../etc/passwd", "javascript:alert(1)", "data:text/html,hi"):
            bad = sanitize(f'<a href="{bad_href}">Hi</a>')
            expect("href=" not in bad and "Hi" in bad, f"sanitizer drops hostile href {bad_href}")
        for good_href in ("https://example.com/x", "flank/index.html", "index.html", "#",
                           "mailto:a@b.com", "tel:+16072056663"):
            good = sanitize(f'<a href="{good_href}">Hi</a>')
            expect(f'href="{good_href}"' in good, f"sanitizer keeps safe href {good_href}")
        # FIX 2-ish handled in flank check; FIX 3: 150px capped in CSS
        expect(".canvas .t-big{font-size:min(12vw,44px) !important;line-height:1.15}" in idx,
               "40px-or-bigger inline titles carry a phone cap via the t-big class (tuned for ~393px modern phones)")
        expect('[style*="font-size:10"]' not in idx and '[style*="font-size:16"]' not in idx,
               "the old pattern-match cap is gone (no more inflating 10-19px text)")
        bigp = sanitize('<p style="font-size:96px">x</p>')
        smallp = sanitize('<p><span style="font-size:16px">x</span></p>')
        expect(bigp == '<p class="t-big" style="font-size:96px">x</p>'
               and sanitize(bigp) == bigp,
               "the sanitizer stamps t-big on real giant titles, idempotently")
        expect(smallp == '<p><span style="font-size:16px">x</span></p>',
               "small inline text is never inflated on phones")
        expect("font:16px Georgia,serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule)" in CSS,
               "every form field is 16px so iPhone never auto-zooms on focus")
        expect("font:16px Georgia,Times,serif;color:var(--ink);background:var(--paper);border:1px solid var(--rule);border-radius:2px}" in CSS or
               ".evcal .ev-search{flex:1 1 200px;max-width:260px;min-height:40px;padding:8px 10px;font:16px Georgia,Times,serif" in CSS,
               "calendar search box is 16px on mobile too")
        expect("font:16px Georgia,serif;color:var(--ink);background" in open("scripts/build_flank.py",encoding="utf-8").read(),
               "the ledger's search input is 16px (same iOS zoom rule)")
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
        # Theme toggle buttons were removed from the masthead (see commit
        # "remove theme toggle buttons"); theme is now chosen on the
        # Settings page. No toggle element is expected on built pages.
        expect("-webkit-text-size-adjust:100%" in idx and "text-size-adjust:100%" in idx,
               "mobile text autosizing is locked off (no font boosting) on every page")
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
        # big button box: class, safe href, validated geometry survive
        bb = sanitize('<a class="bigbtn" href="contact.html" '
                      'style="position:absolute;left:120px;top:40px;width:300px;height:90px;'
                      'margin:0px;background-color:#1c4fd8;color:#fff">Go</a>')
        expect(bb == '<a href="contact.html" class="bigbtn" '
                     'style="position:absolute;margin:0px;left:120px;top:40px;width:300px;'
                     'height:90px;color:#fff;background-color:#1c4fd8">Go</a>',
               "bigbtn keeps href, class and canonical geometry+color style")
        expect(sanitize(bb) == bb, "bigbtn survives a second sanitize pass")
        bbflow = sanitize('<a class="bigbtn" href="contact.html">Contact us</a>')
        expect('class="bigbtn"' in bbflow and "style=" not in bbflow,
               "bigbtn without a style keeps no style attribute (flows centered)")
        for bad_geom in ("left:url(x)", "top:expression(1)", "width:99999px",
                         "height:-10px", "position:relative", "margin:10px"):
            dropped = sanitize('<a class="bigbtn" href="x.html" style="%s">Go</a>' % bad_geom)
            expect("style=" not in dropped and "Go" in dropped,
                   f"bigbtn drops hostile geometry {bad_geom}")
        bigbtn_css_ok = ".canvas a.bigbtn" in CSS and "position:relative" in CSS and ".canvas a.btn" in CSS
        expect(bigbtn_css_ok, "button CSS present and canvas anchors absolute children")
        abtn = sanitize('<a class="btn" href="contact.html" '
                        'style="position:absolute;left:10px;top:20px;width:160px;height:48px;'
                        'margin:0px;background-color:#121212;color:#faf3e6">Go</a>')
        expect(abtn == '<a href="contact.html" class="btn" '
                       'style="position:absolute;margin:0px;left:10px;top:20px;width:160px;'
                       'height:48px;color:#faf3e6;background-color:#121212">Go</a>',
               "a.btn keeps href, class and canonical geometry+color style")
        expect(sanitize(abtn) == abtn, "a.btn survives a second sanitize pass")
        expect(sanitize('<a class="btn bigbtn" href="x.html">Go</a>') == '<a href="x.html" class="bigbtn">Go</a>',
               "a pair of button classes canonicalises to the big one")
        expect("class=\"bigbtn btn\"" not in sanitize('<a class="btn bigbtn" href="x.html" style="background-color:#111">Go</a>'),
               "no combined button class can reach _style_attr_box and drop styles")
        expect(sanitize('<a class="btn" href="x.html">Go</a>') == '<a href="x.html" class="btn">Go</a>',
               "a.btn without a style keeps no style attribute")
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
        # Feedback session 2026-10-26 (emails 883/893/906/907/908/909/910/913):
        # comic B&W badges, deliverable form, localized stamp, comic burst,
        # logo -> home, calendar on events, simplified products + deep page.
        expect('class="logolink" href="index.html"' in idx,
               "clicking the logo returns to the main page")
        expect("Arial Black" in idx and "#fffdf7" in idx and ".gem-badge" in idx,
               "star and feedback badges render in the comic black-and-white style")
        expect(".star-badge::before,.star-badge::after{content:\"\";position:absolute;"
               "clip-path:polygon" in CSS,
               "comic star outline comes from layered pseudos (clip-path-safe)")
        anim = site.get("star_animation", "comic")
        expect(anim in STAR_ANIMATIONS and ('data-star-animation="' + anim + '"') in idx,
               "star animation is a known mode and the pages carry it")
        expect("comicko" in idx and "comicflash" in idx,
               "comic burst draws an ink starburst; no portal/fade branches left behind")
        # No mode-switching left in the JS. SVG attribute fill='none' is fine.
        script_blocks = re.findall(r"<script[^>]*>(.*?)</script>", idx, flags=re.S)
        joined = "\n".join(script_blocks)
        expect("'portal'" not in joined and "'fade'" not in joined
               and "mode==='" not in joined and 'mode||"portal"' not in joined,
               "no leftover mode-switching code in the runtime scripts")
        cf = ((tmp / "contact.html").read_text(encoding="utf-8") if (tmp / "contact.html").is_file() else "") + "\n" + _css
        expect('name="_captcha" value="false"' in cf and
               'name="_next" value="https://flamingoandcactus.com/contact-ack.html"' in cf,
               "contact posts captcha-free and lands on our noindex ack page")
        expect('id="cf-status" hidden' in cf and "function finish(" in cf and
               "watch=setTimeout" not in cf,
               "a send gets a status line and one explicit verdict; no hang watchdog re-sends it")
        expect("formsubmit.co/ajax/" not in cf and "fetch(FORMINIT_URL" in cf,
               "text-only notes make one FormInit request and get an explicit verdict")
        expect('textContent="It went through."' in cf and '"cf-done"' in cf,
               "success swaps the form for an unmissable panel")
        expect('textContent="It did not go through."' in cf,
               "a refused note gets an explicit error panel")
        expect("America/New_York" in cf and "Eastern" in cf,
               "sent_at is stamped in clearly-labeled Eastern time")
        expect('body[data-theme="midnight"] .contactform input[type=text]' in cf,
               "dark mode gives the form fields a visible background")
        expect(".cf-status[hidden]{display:none}" in CSS and ".cf-done[hidden]" in CSS and ".cf-error[hidden]" in CSS,
               "the status line and panels stay hidden until used")
        evc = ((tmp / "events.html").read_text(encoding="utf-8") if (tmp / "events.html").is_file() else "") + "\n" + _css
        if "[event calendar]" in (pages_dir / "events.html").read_text(encoding="utf-8"):
            expect('id="evcal"' in evc and 'var EV=' in evc,
                   "events page swaps the calendar in where the marker was")
            expect("[event calendar]" not in evc,
                   "the calendar marker never leaks onto the public page")
            expect(".evcal-empty[hidden]" in evc and ".evcal .evcal-bar button[hidden]" in evc,
                   "calendar hidden/empty states obey the hidden attribute")
            expect('data-cal-tab="cards"' in evc and 'No upcoming events scheduled right now' in evc,
                   "events page defaults to a useful agenda with an empty state")
            expect('"match"' in evc and "ymin=ty-1,ymax=ty+5" in evc,
                   "search runs on the full untruncated text; year list covers every event year")
            expect(".canvas .evsrc{display:none}" in evc,
                   "the event source block stays hidden; the calendar is the face")
            expect(".ev-day .ev-dot{position:absolute;left:50%;top:50%" in evc and "#1a7f37" in evc,
                   "calendar event dots are centered green dots")
        prdb = (tmp / "products-deep.html").read_text(encoding="utf-8") if (tmp / "products-deep.html").is_file() else ""
        prd = (tmp / "products.html").read_text(encoding="utf-8") if (tmp / "products.html").is_file() else ""
        if (pages_dir / "products-deep.html").is_file():
            expect('href="products-deep.html"' in prd,
                   "products page links the hidden deep-explanations page")
            expect("VPT24-6670" in prdb and "VPT24-6670" not in prd,
                   "the full technical write-up lives only on the deep page")
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
        # Form config sanitization: web3forms_access_key accepts URL-safe
        # alphanumerics, dashes and underscores; everything else is dropped;
        # length is capped. Visitor-facing keys (titles, labels, body) are
        # also length-capped so a pasted 20 KB string cannot break layout.
        bad_key = sanitize_form_config({"web3forms_access_key": "abc!@# def-_-_ghi\n<script>"})
        # Both halves of the input survive; the ' ', '!', '@', '#', '\n',
        # '<' and '>' are stripped, leaving the URL-safe alphanumerics,
        # dashes and underscores intact.
        expect(bad_key["web3forms_access_key"] == "abcdef-_-_ghiscript",
               "web3forms_access_key is stripped to URL-safe characters only")
        long_key = sanitize_form_config({"web3forms_access_key": "x" * 600})
        expect(len(long_key["web3forms_access_key"]) == 256,
               "web3forms_access_key is capped at 256 characters")
        empty_key = sanitize_form_config({})
        expect(empty_key["web3forms_access_key"] == "",
               "omit-from-doc leaves web3forms_access_key empty (failover disabled)")
        # FormInit form ID: public endpoint ID, same URL-safe charset, own
        # cap. Hostile characters are stripped, never rejected with an
        # error (the dialog must not blow up on a paste).
        bad_id = sanitize_form_config({"forminit_form_id": "abc 123!@#\n<script>/f/x"})
        expect(bad_id["forminit_form_id"] == "abc123scriptfx",
               "forminit_form_id is stripped to URL-safe characters only")
        long_id = sanitize_form_config({"forminit_form_id": "y" * 600})
        expect(len(long_id["forminit_form_id"]) == 128,
               "forminit_form_id is capped at 128 characters")
        expect(empty_key["forminit_form_id"] == "",
               "omit-from-doc leaves forminit_form_id empty (last resort disabled)")
        long_svc = sanitize_form_config({"second_failover_service": "S" * 600})
        expect(len(long_svc["second_failover_service"]) == 120,
               "second failover service name capped at 120 characters")
        long_body = sanitize_form_config({"disclaimer_body": "A" * 5000})
        expect(len(long_body["disclaimer_body"]) == 1200,
               "disclaimer_body capped at 1200 characters")
        long_btn = sanitize_form_config({"button": "B" * 600})
        expect(len(long_btn["button"]) == 80,
               "form button text capped at 80 characters")
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
            expect('var k="fc-theme"' not in flank_html,
                   "ledger never follows the main site's theme (no shared key)")
            expect('data-theme' not in flank_html,
                   "ledger keeps its own look (no theme overrides)")
        if "sitenav" in idx:
            nav_block = idx.split('<nav class="sitenav">')[1].split("</nav>")[0]
            expect("flank" not in nav_block,
                   "ledger stays out of the nav (unlisted page)")
            expect("promotional" not in nav_block,
                   "Promotional moved out of the main menu")
        # Fail-safe check, no passphrase needed: a row flagged private (or
        # in the built-in private list) must carry either no note or an
        # encrypted token. Plain text there means the site is about to
        # publish something it promised to hide.
        ledger_file = data_path.parent / "The Flank.md"
        if ledger_file.is_file():
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import build_flank as _bf
            import flank_crypto as _fc
            plaintext_rows = []
            for num, fields in _fc.parse_ledger(ledger_file.read_text(encoding="utf-8")):
                flag = _fc.private_flag(fields.get("private"))
                if flag is None:
                    flag = num in _bf.PRIVATE_TX
                note = (fields.get("notes") or "").strip()
                if flag and note and _fc.parse_token(note) is None:
                    plaintext_rows.append(num)
            expect(not plaintext_rows,
                   f"no private note sits in the repo ledger in plain text (found {plaintext_rows})")
        settings_built = tmp / "settings.html"
        expect(settings_built.is_file(), "site/settings.html built")
        # the contact form's own wording (data/form.json) reaches the page
        form_cfg = load_form_config(data_path.parent)
        if (data_path.parent / "form.json").is_file():
            expect(form_cfg["button"] in contact_built,
                   "the contact form uses the wording from data/form.json")
            if not form_cfg["show_files"]:
                expect("cf-photo" in contact_built,
                       "a hidden file field still keeps its input for the script")
        expect('b.textContent=' in contact_built,
               "the contact script restores the configured button text")
        # the static month grid the builder draws
        static = place_calendar_static("<p>[event calendar]</p>"
                                       "<div class=\"evsrc\"><p>November 14th, 2026, "
                                       "9 am to 3 pm — Test Fair</p></div>")
        expect("[event calendar]" not in static,
               "the static calendar replaces its marker")
        expect(static.count("ev-day") >= 28,
               "the static calendar draws a month of days")
        expect('data-cal-day="2026-11-14"' in static,
               "the static calendar marks the day an event sits on")
        expect("ev-dot" in static,
               "the static calendar dots the days that have events")
        if settings_built.is_file():
            settings_html = settings_built.read_text(encoding="utf-8")
            expect("[site settings]" not in settings_html,
                   "settings marker was swapped for the panel")
            expect("settheme" in settings_html,
                   "settings page carries the theme cards")
            expect("fc-theme" in settings_html,
                   "settings page writes the shared fc-theme key")
            expect('class="setpanel"' in settings_html,
                   "settings page carries the panel")
        financials_built = tmp / "financials.html"
        if financials_built.is_file():
            fin_html = (financials_built.read_text(encoding="utf-8")) + "\n" + _css
            expect('a.btn[href^="flank/"]::before' in fin_html,
                   "financials carries the Flank button's swirl dressing")
            expect("fin-monthly" in fin_html and "fin-in" in fin_html and "fin-out" in fin_html
                   and "finance.js?v=" in fin_html and "chart.umd.min.js" in fin_html,
                   "financials carries the money-in/out chart and the local chart runtime")
            expect(fin_html.count('class="fin-tile"') == 2,
                   "the glance is only money in and money out")
            expect("fin-balance" not in fin_html,
                   "the balance chart stays in the full ledger, not the summary")
            expect("See every transaction in The Flank" in fin_html,
                   "the glance pushes visitors into the actual ledger")
            expect("[flank-finance]" not in fin_html,
                   "the finance marker never leaks onto the public page")
            fin_match = re.search(
                r'<script type="application/json" id="flank-finance-data">(.*?)</script>',
                fin_html, re.S)
            if fin_match:
                try:
                    fin_data = json.loads(fin_match.group(1))
                except ValueError:
                    fin_data = {}
                expect(bool(fin_data.get("months")) and "tx" not in fin_data,
                       "the summary embeds month totals, not the whole transaction list")
                expect("notes" not in json.dumps(fin_data),
                       "the summary carries no note text")
            else:
                expect(False, "the ledger data is embedded as JSON for the chart")
        giscus_built = tmp / "giscus.html"
        if giscus_built.is_file():
            giscus_html = giscus_built.read_text(encoding="utf-8")
            expect("giscus.app/client.js" in giscus_html and
                   "DIC_kwDOVWEZ0c4DMCDB" in giscus_html,
                   "Giscus-ting carries the GitHub Discussions embed")
            expect("[giscus]" not in giscus_html,
                   "the Giscus marker never leaks onto the public page")
            expect('href="giscus.html"' in idx and ">Giscus-ting</a>" in idx,
                   "the Giscus-ting tab sits in the main menu, not the footer")
            expect("giscus.html" in sm, "sitemap lists the Giscus-ting page")
        expect('href="settings.html"' in idx and 'class="footlinks"' in idx,
               "the footer carries the Settings link (and the other small links)")
        expect('href="promotional.html"' in idx and '>Promotional</a>' in idx,
               "Promotional sits in the footer with Settings")
        expect("promotional" not in sm, "sitemap keeps the footer pages unlisted")
        settings_meta = next((p for p in site["pages"] if p["slug"] == "settings"), {})
        if not settings_meta.get("hidden"):
            expect('>Settings</a>' in idx,
                   "nav carries the Settings link")
        sitemap = (tmp / "sitemap.xml").read_text(encoding="utf-8")
        if not settings_meta.get("hidden"):
            expect("settings.html" in sitemap, "sitemap lists Settings")
        # Real headings: one h1 per page, and it names the page (the home
        # page's h1 is the site name, which is its name).
        headed = {}
        for p in site["pages"]:
            text = (tmp / f"{p['slug']}.html").read_text(encoding="utf-8")
            h1s = re.findall(r"<h1[ >](.*?)</h1>", text, re.S)
            headed[p["slug"]] = [html.unescape(h) for h in h1s]
        expect(all(len(v) == 1 for v in headed.values()),
               "every page has exactly one h1")
        expect(headed.get("index") in ([site["title"]], ["Flamingo AND Cactus"], ["Flamingo and Cactus"]),
               "the home page h1 is the site name")
        expect(all(v[0] not in (site["title"], "Flamingo and Cactus", "Flamingo AND Cactus")
                   for s, v in headed.items() if s != "index"),
               "every other page h1 names the page, not the site")
        expect('class="canvas"' in (tmp / "contact.html").read_text(encoding="utf-8")
               and "<h1>Contact</h1>" in (tmp / "contact.html").read_text(encoding="utf-8"),
               "a page with no heading of its own gets its name as a real h1")
        home_html = (tmp / "index.html").read_text(encoding="utf-8")
        expect('class="star-badge"' in home_html and 'class="gem-badge"' in home_html
               and 'class="has-badges"' in home_html,
               "the star and gem badges live on the home page")
        products_html = (tmp / "products.html").read_text(encoding="utf-8")
        expect("star-badge" not in products_html and "gem-badge" not in products_html
               and "has-badges" not in products_html,
               "subpages carry no badges and no reserved top space for them")
        expect(products_html.count('class="gh-repo"') == 2
               and 'data-gh-repo="bearjhartjen/Lamoka1-half"' in products_html
               and 'data-gh-repo="bearjhartjen/Lamoka1-DEV-Brain"' in products_html,
               "each product carries its own live GitHub strip")
        expect("[github-stats:" not in products_html,
               "the GitHub strip marker never leaks onto the product page")
        expect('data-gh-repo="bearjhartjen/flamingo-site"' in home_html,
               "the home page ends with the website's own repository strip")
        expect("[github-stats:" not in home_html,
               "the GitHub strip marker never leaks onto the home page")
        expect('<button type="button" class="themetoggle" id="theme-toggle">' in home_html,
               "the footer theme button is wired for site.js")
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
    ap.add_argument("--favicon", type=Path, default=DEFAULT_FAVICON)
    ap.add_argument("--uploads", type=Path, default=DEFAULT_UPLOADS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-write-back", action="store_true",
                    help="do not clean data/pages/*.html in place (CI and checks)")
    args = ap.parse_args(argv)
    if args.check:
        check(args.data.expanduser().resolve(), args.pages.expanduser().resolve(),
              args.logo.expanduser().resolve(), args.uploads.expanduser().resolve(),
              args.favicon.expanduser().resolve())
    else:
        build(args.data.expanduser().resolve(), args.pages.expanduser().resolve(),
              args.logo.expanduser().resolve(), args.uploads.expanduser().resolve(),
              args.out.expanduser().resolve(), args.favicon.expanduser().resolve(),
              write_back=not args.no_write_back)


if __name__ == "__main__":
    main()

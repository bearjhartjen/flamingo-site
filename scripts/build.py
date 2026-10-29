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
# Star burst: the comic look is the only one this site ships. Earlier
# variants (Golden burst portal, Soft fade, No animation) were removed in
# October 2026 — the comic ink-and-paper starburst is the house style.
# site.json may carry the key but the build always renders comic.
STAR_ANIMATIONS = ("comic",)
STAR_ANIMATION_DEFAULT = "comic"
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
        # star_animation is locked to comic. Read site.json's value for
        # diagnostic logging only -- the builder, the renderer, and the
        # live HTML all hard-code comic. A hand-edit can no longer reach.
        "star_animation": STAR_ANIMATION_DEFAULT,
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
                    if c in TEXT_CLASSES and c not in keep:
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
    --paper:#faf3e6;
    --paper2:#f5ecd9;
    --ink:#141414;
    --ink2:#3d3d3d;
    --muted:#6b6b6b;
    --faint:#8c8c8c;
    --rule:#c9c2b4;
    --rule2:#e8e2d6;
    --link:#0b2a8c;
    --gold:#c9a227;
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
    .masthead{padding-top:150px}
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
  /* status line: success banner + the fallback when a submit hangs */
  .cf-status{display:block;padding:10px 12px;border:1px solid var(--rule);border-radius:2px;background:var(--paper);font:italic 13px/1.5 Georgia,Times,serif;color:var(--ink2);margin:10px 0 0}
  .cf-status[hidden]{display:none}
  .cf-status a{color:var(--link)}
  .cf-status.is-err{border-color:#b3261e;color:#b3261e}
  body[data-theme="ink"] .cf-status.is-err{border-color:#ff8a80;color:#ff8a80}
  body[data-theme="midnight"] .cf-status.is-err{border-color:#ff9d9d;color:#ff9d9d}
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
  .evcal{max-width:640px;margin:18px auto 8px;border:1px solid var(--rule);border-radius:6px;background:#f3e8d2;padding:16px 16px 8px;display:none}
  body[data-theme="ink"] .evcal{background:#332e28;border-color:#6b6152}
  body[data-theme="midnight"] .evcal{background:#2b2336;border-color:#4d3c6b}
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
      display:flex;flex-wrap:wrap;justify-content:center;gap:6px 8px;
      font-size:11px;line-height:2.1;
    }
    nav.sitenav a,nav.sitenav .cur{margin:0 10px;white-space:nowrap}
    .star-badge{top:10px;left:10px;right:auto;width:140px;height:140px}
    .star-badge .star-title{font-size:10px;max-width:70%;line-height:1.25;letter-spacing:.2px}
    .star-badge .star-sub{display:none}
    .gem-badge{top:42px;left:auto;right:10px;width:140px;height:76px;border-radius:11px;border-width:2.5px;box-shadow:5px 6px 0 #111}
    .gem-badge .gem-title{font-size:10px;letter-spacing:1.5px}
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
    .contactform{margin:22px 0 0;padding:16px 14px}
    .cf-done,.cf-error{margin:22px 0 0;padding:16px 14px}
    .canvas a.btn[href^="flank/"]{padding:9px 30px}
    .canvas a.btn[href^="flank/"]::before{left:9px}
    .canvas a.btn[href^="flank/"]::after{right:9px}
    .settheme{width:104px}
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
}

FORM_TEXT_KEYS = ("subject", "name_label", "name_sub", "name_placeholder",
                  "contact_label", "contact_sub", "contact_placeholder",
                  "note_label", "note_placeholder", "files_label",
                  "files_sub", "button")
FORM_BOOL_KEYS = ("show_contact", "show_files")


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
    """Keep only the known form keys, with sane types and lengths."""
    cfg = dict(FORM_DEFAULTS if base is None else base)
    if not isinstance(doc, dict):
        return cfg
    for key, val in doc.items():
        if key in FORM_TEXT_KEYS and isinstance(val, str):
            cfg[key] = val[:400]
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
    markup = (
        '<form class="contactform" action="https://formsubmit.co/' + CONTACT_EMAIL + '" method="POST" enctype="multipart/form-data">\n'
        f'  <input type="hidden" name="_subject" value="{esc(cfg["subject"], True)}">\n'
        '  <input type="hidden" name="_template" value="table">\n'
        '  <input type="hidden" name="_captcha" value="false">\n'
        '  <input type="hidden" name="_next" value="https://' + CUSTOM_DOMAIN + '/contact.html?sent=1">\n'
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
        "</form>"
    )
    script = (CONTACT_FORM_SCRIPT
              .replace("__BUTTON_JSON__", json.dumps(str(cfg["button"])))
              .replace("__SUBJECT_JSON__", json.dumps(str(cfg["subject"]))))
    return markup + script


CONTACT_FORM_SCRIPT = (
  '<script>(function(){try{'
  # Submission handling: stay on the page and get an explicit answer.
  # Text-only notes POST as JSON to FormSubmit's AJAX endpoint, and the page
  # shows a big success or error panel carrying the service own verdict —
  # no navigation away from the site, no silent "did it send?".
  # Notes with files still go by the classic multipart POST, because the
  # AJAX endpoint does not turn base64 payloads into real attachments;
  # that road keeps the ?sent=1 redirect and the 20 s hang watchdog.
  "var AJAX_URL='https://formsubmit.co/ajax/" + CONTACT_EMAIL + "';"
  'var inlineStatus=function(t,err){var st=document.getElementById("cf-status");if(!st)return;'
  'st.hidden=false;st.textContent=t;if(err)st.classList.add("is-err");else st.classList.remove("is-err");};'
  'var inp=document.getElementById("cf-photo"),hint=document.getElementById("cf-filehint"),form=inp&&inp.form;'
  'if(!inp||!hint||!form)return;'
  'var all=[],extras=[],MAX_BYTES=10000000,busy=false,watch=null,picker=null,lastStamp="";'
  'function getBtn(){return form.querySelector("button[type=submit]");}'
  'function restoreBtn(){var b=getBtn();if(b){b.disabled=false;b.textContent=__BUTTON_JSON__;}}'
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
  'var emLink=document.createElement("a");emLink.href="mailto:' + CONTACT_EMAIL + '";emLink.textContent="' + CONTACT_EMAIL + '";'
  'errMail.appendChild(emLink);'
  'errbox.appendChild(errHead);errbox.appendChild(errBody);errbox.appendChild(errMail);'
  'errbox.hidden=true;form.parentNode.insertBefore(errbox,form);'
  'function scrollUp(el){try{el.scrollIntoView({behavior:"smooth",block:"center"});}catch(_){try{el.scrollIntoView();}catch(_2){}}}'
  'function showDone(){busy=false;restoreBtn();errbox.hidden=true;form.hidden=true;done.hidden=false;'
  'doneSub.textContent="Your note is in our inbox. We read everything — thank you!";'
  'doneStamp.textContent=lastStamp?("Stamped "+lastStamp):"";'
  'scrollUp(done);}'
  'function showError(msg){busy=false;restoreBtn();done.hidden=true;errbox.hidden=false;'
  'errBody.textContent=(msg||"The note did not send.")+" Press Send again — or email us directly below.";'
  'scrollUp(errbox);}'
  'function hideStatus(){var st=document.getElementById("cf-status");if(st){st.hidden=true;}}'
  'again.addEventListener("click",function(){try{done.hidden=true;errbox.hidden=true;form.hidden=false;'
  'try{form.reset();}catch(_){}all=[];extras=[];'
  'try{if(picker)picker.value="";}catch(_){}try{inp.value="";}catch(_){}inp.multiple=true;'
  'render();stampTime();hideStatus();}catch(_){}});'
  'window.addEventListener("pagehide",function(){if(watch){clearTimeout(watch);watch=null;}});'
  'function stampTime(){try{var st=document.getElementById("cf-sentat");if(!st)return;'
  'var d=new Date();var s="";'
  'try{s=d.toLocaleString("en-US",{timeZone:"America/New_York",month:"numeric",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit",second:"2-digit",timeZoneName:"short"})+" (Eastern)";}'
  'catch(_){s=d.toString()+" | UTC offset "+(d.getTimezoneOffset()/-60)+" hours";}'
  'st.value=s;lastStamp=s;}catch(_){}}'
  'stampTime();'
  # ?sent=1 (the files road redirects back here with it): the note went
  # through — show the same big panel instead of the form.
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
  'function failToNative(msg){all=[];for(var i=0;i<extras.length;i++)if(extras[i].parentNode)extras[i].parentNode.removeChild(extras[i]);extras=[];'
  'try{inp.value="";picker.value="";}catch(_){}inp.multiple=false;inp.className="";inp.removeAttribute("tabindex");inp.removeAttribute("aria-hidden");'
  'picker.disabled=true;zone.hidden=true;lst.hidden=true;hint.textContent=msg;hint.classList.add("over");}'
  'function render(){var t=0,i;lst.innerHTML="";'
  'for(i=0;i<all.length;i++)(function(f,ix){t+=f.size;'
  'var li=document.createElement("li"),nm=document.createElement("span"),b=document.createElement("button");'
  'nm.textContent=f.name+" ("+fmt(f.size)+")";li.appendChild(nm);'
  'b.type="button";b.textContent="Remove";b.setAttribute("aria-label","Remove "+f.name);'
  'b.onclick=function(){all.splice(ix,1);sync();render();};li.appendChild(b);lst.appendChild(li);})(all[i],i);'
  'var over=t>MAX_BYTES;'
  'hint.textContent=!all.length?"No files yet.":all.length+(all.length===1?" file, ":" files, ")+fmt(t)+" of 10 MB.";'
  'hint.classList.toggle("over",over);return !over;}'
  'function add(fs){for(var i=0;i<fs.length;i++)all.push(fs[i]);if(!sync()){failToNative("This browser could not safely combine files. Use the file picker and send one file per message.");return false;}render();return true;}'
  'zone.addEventListener("click",function(){picker.click();});'
  'zone.addEventListener("dragover",function(e){if(e.dataTransfer&&e.dataTransfer.types&&e.dataTransfer.types.indexOf("Files")>=0){e.preventDefault();zone.classList.add("over");}});'
  'zone.addEventListener("dragleave",function(){zone.classList.remove("over");});'
  'zone.addEventListener("drop",function(e){e.preventDefault();zone.classList.remove("over");var d=e.dataTransfer;if(d&&d.files&&d.files.length)add(d.files);});'
  'form.addEventListener("paste",function(e){var d=e.clipboardData;if(d&&d.files&&d.files.length){e.preventDefault();add(d.files);}});'
  'picker.addEventListener("change",function(){if(picker.files&&picker.files.length)add(picker.files);try{picker.value="";}catch(_){}});'
  'function prepNative(){for(var x=0;x<extras.length;x++)if(extras[x].parentNode)extras[x].parentNode.removeChild(extras[x]);extras=[];'
  'if(!setFiles(inp,[all[0]]))return false;inp.multiple=false;'
  'for(var xi=1;xi<all.length;xi++){var ni=document.createElement("input");ni.type="file";ni.name="attachment_"+(xi+1);ni.className="filepick filepick-extra";ni.tabIndex=-1;ni.setAttribute("aria-hidden","true");'
  'if(!setFiles(ni,[all[xi]]))return false;form.appendChild(ni);extras.push(ni);}return true;}'
  'form.addEventListener("submit",function(e){var pd=false;'
  'try{e.preventDefault();pd=true;'
  'if(busy)return;'
  'stampTime();'
  'if(!render()){hint.textContent="Those files are over 10 MB in total — remove one and try again.";return;}'
  'var honey=form.querySelector("[name=_honey]");'
  'if(honey&&honey.value){showDone();return;}'
  'var b=getBtn();if(b){b.disabled=true;b.textContent="Sending…";}'
  # Files ride the classic multipart POST (FormSubmit turns real multipart
  # parts into email attachments; it does not do that for JSON base64).
  'if(all.length){'
  'if(!prepNative()){restoreBtn();failToNative("This browser could not prepare the files. Use the file picker and send one file per message.");return;}'
  'busy=true;'
  'inlineStatus("Sending "+(all.length===1?"1 file":all.length+" files")+" — a big upload can take a moment…",false);'
  # 20 s watchdog: a real submit unloads the page (pagehide clears this).
  # If the load is stuck, explain the way out instead of a gray button.
  'watch=setTimeout(function(){showError("The upload did not come back within 20 seconds. The files may still be going — only send again if nothing arrives.");},20000);'
  'form.submit();return;}'
  'busy=true;'
  'var f=function(n){var el=form.querySelector("[name="+n+"]");return el?el.value:"";};'
  'var payload={_subject:f("_subject")||__SUBJECT_JSON__,_template:"table",_captcha:"false",'
  'sent_at:(document.getElementById("cf-sentat")||{}).value||lastStamp,'
  'name:f("name"),contact:f("contact"),note:f("note")};'
  'var c=payload.contact.replace(/^\\s+|\\s+$/g,"");'
  'if(/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(c))payload._replyto=c;'
  'inlineStatus("Sending your note…",false);'
  'var ctrl=(typeof AbortController!=="undefined")?new AbortController():null;'
  'var to=ctrl?setTimeout(function(){try{ctrl.abort();}catch(_){}},30000):null;'
  'fetch(AJAX_URL,{method:"POST",headers:{Accept:"application/json","Content-Type":"application/json"},body:JSON.stringify(payload),signal:ctrl?ctrl.signal:undefined})'
  '.then(function(r){return r.json().catch(function(){return{success:"false",message:"the mail service answered with something unreadable"};});})'
  '.then(function(j){if(to)clearTimeout(to);'
  'if(j&&j.success==="true"){showDone();return;}'
  'var m=(j&&j.message)?String(j.message):"the mail service refused the note";'
  'if(/web server|HTML files/i.test(m))m="your browser or an extension hid the page address from the mail service, and it refused the note";'
  'showError("The mail service said: "+m+".");})'
  '.catch(function(er){if(to)clearTimeout(to);'
  'if(er&&er.name==="AbortError"){showError("The send took too long and was stopped. If nothing arrives, press Send again — only once.");return;}'
  'if(form.submit){inlineStatus("The direct send did not answer — trying the classic way…",false);'
  'watch=setTimeout(function(){showError("The classic send did not return either. Your connection may be offline — email us directly instead.");},20000);'
  'form.submit();return;}'
  'showError("The note could not be sent from here (network or mail service unreachable).");});'
  '}catch(er){if(pd){try{busy=false;restoreBtn();}catch(_2){}}}});'
  'window.addEventListener("pageshow",function(){busy=false;restoreBtn();});'
  'render();}catch(_){}})();</script>'
)

# The form as published when nothing has been customised.
CONTACT_FORM_HTML = contact_form_html()


def calendar_static_html(events: list, year: int, month: int,
                         nav: bool = False, cfg: dict | None = None) -> str:
    """A month grid for the events, rendered on the server.

    The live page draws the same grid with its own script; this static
    version is what the builder shows in the canvas while the owner edits
    the events, and it doubles as the no-JS fallback."""
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
        cls = "ev-day"
        if mine:
            cls += " ev-set"
        if (day == today.day and year == today.year and month == today.month):
            cls += " today"
        tip = " | ".join(ev.get("day", "") + ((" · " + ev["times"]) if ev.get("times") else "")
                         + " — " + ev.get("label", "") for ev in mine)
        attrs = (f' data-cal-day="{year:04d}-{month:02d}-{day:02d}" data-d="{day}"'
                 f' title="{html.escape(tip, quote=True)}"'
                 f' aria-label="{html.escape(tip, quote=True)}"')
        dot = (f'<span class="ev-dot" style="color:{color}">{symbol}</span>'
               if mine else "")
        cells.append(f'<span class="{cls}"{attrs}>{day}{dot}</span>')
    bar = ""
    if nav:
        bar = ('<div class="evcal-bar">'
               '<button type="button" data-cal-prev aria-label="Previous month">&lsaquo;</button>'
               f'<span class="evcal-label">{html.escape(month_name)} {year}</span>'
               '<button type="button" data-cal-next aria-label="Next month">&rsaquo;</button>'
               "</div>")
    empty = ('<div class="evcal-empty" hidden>No events found for this month.</div>'
             if not by_day else "")
    return (
        f'<div class="evcal" data-cal-block style="display:block;--ev-mark:{color};--ev-mark-size:{size}px">'
        f'{bar}'
        '<div class="evcal-head" aria-hidden="true">'
        "<span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span>"
        "<span>Thu</span><span>Fri</span><span>Sat</span></div>"
        f'<div class="evcal-grid">{"".join(cells)}</div>'
        f'{empty}</div>'
    )


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
        events.append({
            "y": stamp.year, "m": stamp.month, "d": stamp.day,
            "wd": _WEEKDAYS[stamp.weekday()],
            "ms": _MONTH_SHORT[stamp.month - 1],
            "key": f"{stamp.month}/{stamp.day}",
            # which paragraph the event came from (index among the page's
            # <p>/<li> blocks) so an editor can rewrite that one entry
            "idx": i,
            # the paragraph's own markup, so a link inside it survives an edit
            "html": frag,
            # search runs on the full untruncated text; label is display-only
            "match": stamp.strftime("%B %d, %Y ") + text,
            # the whole line, for the day detail that opens on a click
            "full": text,
            # a link inside the line, if there is one
            "link": link,
            "times": (f"{unit1} – {unit2}" if unit1 and unit2
                      else single if single else ""),
            "day": f"{_WEEKDAYS[stamp.weekday()]}, {_MONTH_SHORT[stamp.month - 1]} {stamp.day}, {stamp.year}",
            "label": label,
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
    '<div class="evcal-bar">'
    '<button type="button" id="ev-prev" aria-label="Previous month">&lsaquo;</button>'
    '<select id="ev-month" aria-label="Month"></select>'
    '<select id="ev-year" aria-label="Year"></select>'
    '<button type="button" id="ev-next" aria-label="Next month">&rsaquo;</button>'
    '<input type="text" id="ev-search" class="ev-search" '
    'placeholder="Search events by name or date" aria-label="Search events">'
    '<button type="button" id="ev-reset" class="ev-reset" hidden>Show all</button>'
    "</div>"
    '<div class="evcal-head" aria-hidden="true">'
    "<span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span>"
    "<span>Thu</span><span>Fri</span><span>Sat</span>"
    "</div>"
    '<div class="evcal-grid" id="ev-grid"></div>'
    '<div class="evcal-empty" id="ev-empty" hidden>No events found for this month.</div>'
    '<div class="ev-detail" id="ev-detail" hidden></div>'
    "</div>"
)


def place_calendar(body_html: str, cfg: dict | None = None) -> tuple[str, str]:
    """Swap an `[event calendar]` marker paragraph for the month calendar.

    The calendar is generated from the dated items already on the page — the
    list itself is untouched and still rendered keeps right below it. Returns
    (page_body, extra_script): extra_script is '' with no marker."""
    import json as _json
    cfg = dict(CAL_DEFAULTS, **(cfg or {}))
    events = _parse_list_events(body_html)
    marker_re = re.compile(r"<(?:p|li)[^>]*>\s*\[event calendar\]\s*</(?:p|li)>",
                           flags=re.IGNORECASE)
    if not marker_re.search(body_html):
        return body_html, ""
    # the source index and markup are for the editor only — the published
    # page does not need them
    data = _json.dumps([{k: v for k, v in ev.items() if k not in ("idx", "html")}
                        for ev in events], ensure_ascii=True).replace("</", "<\\/")
    script = ('<script>/* events calendar: derived from the list on this '
              'page; the list stays the source of truth */(function(){'
              'var EV=' + data + ";"
              'var MARK=' + json.dumps(cfg.get("symbol") or "●") + ',' +
              'MARKCOLOR=' + json.dumps(cfg.get("color") or "#1a7f37") + ';'
              'MARKSIZE=' + json.dumps(str(int(cfg.get("size") or 17))) + ';'
              'var cal=document.getElementById("evcal");if(!cal)return;'
              'cal.style.display="block";'
              'cal.style.setProperty("--ev-mark",MARKCOLOR);'
              'cal.style.setProperty("--ev-mark-size",MARKSIZE+"px");'
              'var grid=document.getElementById("ev-grid"),'
              'monthSel=document.getElementById("ev-month"),'
              'yearSel=document.getElementById("ev-year"),'
              'empty=document.getElementById("ev-empty"),'
              'detail=document.getElementById("ev-detail"),'
              'search=document.getElementById("ev-search"),'
              'reset=document.getElementById("ev-reset");'
              'var months=["January","February","March","April","May","June",'
              '"July","August","September","October","November","December"];'
              'var t0=new Date(),ty=t0.getFullYear();'
              'var ys={};var ymin=ty-1,ymax=ty+5;'
              'for(var i=0;i<EV.length;i++){var y=EV[i].y;if(y<ymin)ymin=y;if(y>ymax)ymax=y;}'
              'for(var y=ymin;y<=ymax;y++)ys[y]=1;'
              'var yyears=[];for(var k in ys)yyears.push(+k);yyears.sort(function(a,b){return a-b;});'
              'months.forEach(function(m,i){var o=document.createElement("option");o.value=i+1;o.textContent=m;monthSel.appendChild(o);});'
              'yyears.forEach(function(y){var o=document.createElement("option");o.value=y;o.textContent=y;yearSel.appendChild(o);});'
              'var cm=t0.getMonth()+1, cy=ty; '
              'var near=null,nf=null,nk=null,tk=t0.getFullYear()*10000+(t0.getMonth()+1)*100+t0.getDate();'
              'for(var i=0;i<EV.length;i++){var ek=EV[i].y*10000+EV[i].m*100+EV[i].d;'
              'if(!nk||ek<nk)nk=ek,near=EV[i];'
              'if(ek>=tk&&(!nf||ek<nf))nf=ek,near=EV[i];}'
              'if(near){cm=near.m;cy=near.y;}'
              'monthSel.value=cm; yearSel.value=cy;'
              'function q(){return (search.value||"").trim().toLowerCase();}'
              'function evOn(ev){var s=q();if(!s)return true;'
              'return (ev.match.toLowerCase().indexOf(s)>=0);}'
              'function render(){'
              'var y=cy, mo=cm;'
              'var first=new Date(y,mo-1,1), days=new Date(y,mo,0).getDate(), dow=first.getDay();'
              'grid.innerHTML="";'
              'for(var i=0;i<dow;i++){var blank=document.createElement("span");blank.className="ev-day empty";grid.appendChild(blank);}'
              'var nOn=0;'
              # clicking a day opens its events; clicking it again closes them
              'var openDay=0;'
              'function closeDay(){if(!detail)return;detail.hidden=true;detail.innerHTML="";openDay=0;'
              'var cs=grid.querySelectorAll(".ev-open");for(var i=0;i<cs.length;i++)cs[i].classList.remove("ev-open");}'
              'function openDayFor(d){if(!detail)return;var mine=[];'
              'for(var i=0;i<EV.length;i++){var e=EV[i];if(e.y===cy&&e.m===cm&&e.d===d)mine.push(e);}'
              'if(!mine.length)return;'
              'detail.innerHTML="";'
              'var head=document.createElement("p");head.className="ev-detail-head";'
              'try{head.textContent=new Date(cy,cm-1,d).toLocaleDateString("en-US",'
              '{weekday:"long",month:"long",day:"numeric",year:"numeric"});}catch(_){head.textContent="";}'
              'detail.appendChild(head);'
              'for(var k=0;k<mine.length;k++){var ev=mine[k];'
              'var item=document.createElement("p");item.className="ev-detail-item";'
              'if(ev.times){var tm=document.createElement("span");tm.className="ev-detail-time";tm.textContent=ev.times;item.appendChild(tm);}'
              'var txt=ev.full||ev.label||"";'
              'if(ev.link&&ev.link.url&&ev.link.text){'
              'var li=txt.lastIndexOf(ev.link.text);'
              'if(li>=0){item.appendChild(document.createTextNode(txt.slice(0,li)));'
              'var a=document.createElement("a");a.href=ev.link.url;a.textContent=ev.link.text;'
              'a.target="_blank";a.rel="noopener";item.appendChild(a);'
              'item.appendChild(document.createTextNode(txt.slice(li+ev.link.text.length)));}'
              'else{item.appendChild(document.createTextNode(txt));}}'
              'else{item.appendChild(document.createTextNode(txt));}'
              'detail.appendChild(item);}'
              'detail.hidden=false;'
              'var cs=grid.querySelectorAll(".ev-day");'
              'for(var i=0;i<cs.length;i++){if(cs[i].getAttribute("data-d")==String(d))cs[i].classList.add("ev-open");}'
              'openDay=d;}'
              'grid.addEventListener("click",function(e){'
              'var cell=(e.target.closest)?e.target.closest(".ev-day"):null;if(!cell)return;'
              'var dd=cell.getAttribute("data-d");if(!dd)return;var d=+dd;'
              'if(openDay===d){closeDay();return;}closeDay();openDayFor(d);});'
              'for(var d=1;d<=days;d++){(function(d){'
              'var mine=[];for(var i=0;i<EV.length;i++){var e=EV[i];if(e.y===y&&e.m===mo&&e.d===d&&evOn(e))mine.push(e);}'
              'var cell=document.createElement("span");cell.className="ev-day";cell.textContent=d;'
              'if(d===new Date().getDate()&&y===new Date().getFullYear()&&mo===new Date().getMonth()+1)cell.classList.add("today");'
              'if(mine.length){nOn++;cell.classList.add("ev-set");'
              'var dot=document.createElement("span");dot.className="ev-dot";dot.textContent=MARK;dot.style.color=MARKCOLOR;dot.style.fontSize=MARKSIZE+"px";cell.appendChild(dot);'
              'var tip=[];for(var j=0;j<mine.length;j++)tip.push(mine[j].day+(mine[j].times?" · "+mine[j].times:"")+" — "+mine[j].label);'
              'cell.setAttribute("role","img");'
              'cell.setAttribute("aria-label",tip.join(" | "));'
              'cell.setAttribute("data-d",d);'
              'cell.style.display="";}'
              'else{cell.style.display=q()?"none":"";}'
              'grid.appendChild(cell);})(d);}'
              'empty.hidden=!!nOn;'
              'empty.textContent=q()?"No events match your search.":"No events found for this month.";'
              'closeDay();'
              '}'
              'function setSearch(){var s=q();reset.hidden=!s;'
              'if(s){var first=null;'
              'for(var i=0;i<EV.length;i++){var e=EV[i];if(evOn(e)){if(first===null||e.y*10000+e.m*100+e.d<first.y*10000+first.m*100+first.d)first=e;}}'
              'if(first){cm=first.m;cy=first.y;}}'
              'monthSel.value=cm;yearSel.value=cy;render();}'
              'function syncSel(){var o=[];'
              'for(var i=0;i<yearSel.options.length;i++)o.push(+yearSel.options[i].value);'
              'if(o.indexOf(+cy)<0){var no=document.createElement("option");no.value=cy;no.textContent=cy;yearSel.appendChild(no);}'
              'yearSel.value=cy;monthSel.value=cm;}'
              'monthSel.addEventListener("change",function(){cm=+monthSel.value;render();});'
              'yearSel.addEventListener("change",function(){cy=+yearSel.value;render();});'
              'document.getElementById("ev-prev").addEventListener("click",function(){cm--;if(cm<1){cm=12;cy--;}syncSel();render();});'
              'document.getElementById("ev-next").addEventListener("click",function(){cm++;if(cm>12){cm=1;cy++;}syncSel();render();});'
              'search.addEventListener("input",setSearch);'
              'reset.addEventListener("click",function(){search.value="";setSearch();});'
              'render();})();</script>'
    )
    placed = [""]
    state = {"n": 0}
    def _swap(_m):
        if state["n"] == 0:
            state["n"] += 1
            return EVENTS_CALENDAR_MARKUP
        return ""
    body_new = marker_re.sub(_swap, body_html)
    return body_new, script


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
                star_animation: str = STAR_ANIMATION_DEFAULT,
                form_html: str = "",
                cal_cfg: dict | None = None) -> str:
    page_meta = next((p for p in pages if p["slug"] == slug), {})
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
    placed, set_script = place_settings(placed, theme)
    if placed:
        canvas = f'  <div class="canvas">\n{placed}{cal_script}{set_script}\n  </div>\n'
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
        f'<body data-theme="{theme}" data-star-animation="{STAR_ANIMATION_DEFAULT}">\n'
        '<div class="page">\n'
        f"{star_html(pages)}"
        f"{gem_html(pages)}"
        "\n"
        '  <div class="masthead">\n'
        f'    <a class="logolink" href="index.html" aria-label="Go to the main page"><img src="{logo_uri}" alt="Flamingo and Cactus"></a>\n'
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
        # Starburst: the comic ink-and-paper burst is the only effect on this
        # site. Click the star, badge pops, a black-and-white impact frame
        # flashes, ink shards fly out, then the page reroutes. Returning to
        # a visited page plays the soft arrival flash + rise -- no portal,
        # no soft fade, no "no animation" mode. All transform, opacity, and
        # canvas only -- respects prefers-reduced-motion (skips the canvas).
        "<script>(function(){"
        "try{"
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
        "if(sessionStorage.getItem('fcWarp')){sessionStorage.removeItem('fcWarp');"
        "var afl=document.createElement('div');afl.id='arriveflash';document.body.appendChild(afl);"
        "document.body.classList.add('arrive');"
        "setTimeout(function(){document.body.classList.remove('arrive');if(afl&&afl.parentNode)afl.parentNode.removeChild(afl);},700);}"
        "}catch(_){}"
        "try{"
        "var instant=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;"
        "var star=document.querySelector('a.star-badge, a.gem-badge');"
        "if(!star||!star.getAttribute('href'))return;"
        "star.addEventListener('click',function(e){"
        "var href=star.getAttribute('href');"
        "if(!href||href.charAt(0)==='#')return;"
        "e.preventDefault();"
        "if(instant){window.location.href=href;return;}"
        "if(star.classList.contains('warp'))return;"
        "var r=star.getBoundingClientRect();"
        "var x=r.left+r.width/2,y=r.top+r.height/2;"
        "try{sessionStorage.setItem('fcWarp','1')}catch(_){}"
        "star.classList.add('warp');document.body.classList.add('warping');"
        "var goc=false;function nav2(){if(goc)return;goc=true;window.location.href=href;}"
        "setTimeout(nav2,560);setTimeout(nav2,1100);"
        "setTimeout(function(){document.body.classList.remove('warping');star.classList.remove('warp');var f=document.getElementById('fx');if(f&&f.parentNode)f.parentNode.removeChild(f);},2200);"
        "try{"
        "var fx=document.createElement('div');fx.id='fx';"
        "var fl=document.createElement('div');fl.className='comicflash';fx.appendChild(fl);"
        "var cv=document.createElement('canvas');fx.appendChild(cv);document.body.appendChild(fx);"
        "var dpr=Math.min(window.devicePixelRatio||1,2);"
        "var W=window.innerWidth,H=window.innerHeight;cv.width=W*dpr;cv.height=H*dpr;"
        "var cx2=cv.getContext('2d');cx2.scale(dpr,dpr);"
        "var N=W<700?18:30,S=[],i,a,sp;"
        "for(i=0;i<N;i++){a=Math.random()*Math.PI*2;sp=3+Math.random()*6;"
        "S.push({x:x,y:y,vx:Math.cos(a)*sp,vy:Math.sin(a)*sp,r:4+Math.random()*7,rot:Math.random()*Math.PI,vr:(Math.random()-.5)*.4,age:0,life:26+Math.random()*22,ink:Math.random()<.5});}"
        "var rays=W<700?12:18,seed=[],j;"
        "for(j=0;j<rays;j++)seed.push(.6+Math.random()*.5);"
        "var wax={age:0,life:24};"
        "var t0=performance.now();"
        "(function frame(){"
        "var alive=false,i,s;"
        "cx2.clearRect(0,0,W,H);"
        "if(wax.age<=wax.life){wax.age++;alive=true;"
        "var wx=Math.min(1,wax.age/wax.life),R0=Math.min(W,H)*.08+wx*44;"
        "cx2.globalAlpha=1-wx*.3;"
        "cx2.fillStyle='#fffdf7';cx2.strokeStyle='#111';cx2.lineWidth=5;"
        "cx2.beginPath();"
        "for(i=0;i<16;i++){var aa=i*Math.PI/8,rr=R0*(1+.22*Math.sin(aa*3+wax.age*.5));"
        "var px=x+Math.cos(aa)*rr,py=y+Math.sin(aa)*rr;cx2[i?'lineTo':'moveTo'](px,py);}"
        "cx2.closePath();cx2.fill();cx2.stroke();"
        "for(i=0;i<rays;i++){var aa=i*Math.PI*2/rays,ln=(0.3+0.16*seed[i])*Math.max(W,H)*wx; "
        "cx2.beginPath();cx2.moveTo(x+Math.cos(aa)*R0*.7,y+Math.sin(aa)*R0*.7);"
        "cx2.lineTo(x+Math.cos(aa)*ln,y+Math.sin(aa)*ln);"
        "cx2.strokeStyle=i%2?'#111':'#fffdf7';cx2.lineWidth=i%2?9:4;cx2.stroke();}"
        "cx2.globalAlpha=1;}"
        "for(i=0;i<S.length;i++){s=S[i];s.age++;if(s.age>s.life)continue;alive=true;"
        "s.x+=s.vx;s.y+=s.vy;s.vx*=.97;s.vy=s.vy*.97;s.rot+=s.vr;"
        "cx2.globalAlpha=Math.max(0,1-s.age/s.life);"
        "cx2.save();cx2.translate(s.x,s.y);cx2.rotate(s.rot);"
        "cx2.beginPath();cx2.moveTo(0,-s.r);cx2.lineTo(s.r*.7,s.r);cx2.lineTo(-s.r*.7,s.r);cx2.closePath();"
        "if(s.ink){cx2.fillStyle='#111';cx2.fill();}else{cx2.fillStyle='#fffdf7';cx2.fill();cx2.lineWidth=2;cx2.strokeStyle='#111';cx2.stroke();}"
        "cx2.restore();}"
        "cx2.globalAlpha=1;"
        "if(alive&&performance.now()-t0<1200)requestAnimationFrame(frame);"
        "else if(fx.parentNode)fx.parentNode.removeChild(fx);})();"
        "}catch(_){}"
        "});"
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
    "classic": ("#faf3e6", "#c9c2b4", "#141414", "#0b2a8c", "#c9a227"),
    "parchment": ("#f7f1e1", "#b8ab88", "#2b2417", "#1a3a7c", "#c9a227"),
    "ink": ("#262220", "#6b6152", "#f5efe0", "#9db8ff", "#c9a227"),
    "midnight": ("#221c2c", "#4d3c6b", "#f0e9f7", "#d3b8ff", "#c9a227"),
}

SETTINGS_DESCS = {
    "classic": "Warm paper, ink black",
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
          uploads_dir: Path, out_dir: Path):
    site = load_site(data_path)
    form_html = contact_form_html(load_form_config(data_path.parent))
    cal_cfg = load_calendar_config(data_path.parent)
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
                        site.get("star_animation", "comic"),
                        form_html=form_html, cal_cfg=cal_cfg))
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
        expect("left:10px" in idx and "flex-wrap:wrap" in idx,
               "phone header: star top-left, nav wraps without touching")
        expect("overflow-wrap:anywhere" in idx,
               "unbreakable titles wrap on phones (no sideways cutoff)")
        expect("badgepop" in idx and "arrivefade" in idx and "comicko" in idx,
               "comic only: badge pop, ink burst, and soft arrival flash all present")
        warp_scripts = idx.lower().count("<script")
        expect(warp_scripts == 2 and "ld+json" in idx.lower(),
               "built pages carry only the burst + structured-data scripts")
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
        expect('id="theme-toggle"' in idx,
               "every built page carries the dark-mode toggle")
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
        cf = (tmp / "contact.html").read_text(encoding="utf-8") if (tmp / "contact.html").is_file() else ""
        expect('name="_captcha" value="false"' in cf and 'contact.html?sent=1' in cf,
               "contact posts captcha-free and returns to a flagged success URL")
        expect('id="cf-status" hidden' in cf and "watch=setTimeout" in cf,
               "stuck submits get a status line and a hang watchdog")
        expect("formsubmit.co/ajax/" in cf,
               "text-only notes take the AJAX endpoint and get an explicit verdict")
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
        evc = (tmp / "events.html").read_text(encoding="utf-8") if (tmp / "events.html").is_file() else ""
        if "[event calendar]" in (pages_dir / "events.html").read_text(encoding="utf-8"):
            expect('id="evcal"' in evc and 'var EV=' in evc,
                   "events page swaps the calendar in where the marker was")
            expect("[event calendar]" not in evc,
                   "the calendar marker never leaks onto the public page")
            expect(".evcal-empty[hidden]" in evc and ".evcal .evcal-bar button[hidden]" in evc,
                   "calendar hidden/empty states obey the hidden attribute")
            expect("t0.getFullYear()*10000+(t0.getMonth()+1)*100+t0.getDate()" in evc,
                   "calendar search matches by name, date and month")
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
            fin_html = financials_built.read_text(encoding="utf-8")
            expect('a.btn[href^="flank/"]::before' in fin_html,
                   "financials carries the Flank button's swirl dressing")
        expect('>Settings</a>' in idx,
               "nav carries the Settings link")
        settings_meta = next((p for p in site["pages"] if p["slug"] == "settings"), {})
        if not settings_meta.get("hidden"):
            expect('>Settings</a>' in idx,
                   "nav carries the Settings link")
        sitemap = (tmp / "sitemap.xml").read_text(encoding="utf-8")
        if not settings_meta.get("hidden"):
            expect("settings.html" in sitemap, "sitemap lists Settings")
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

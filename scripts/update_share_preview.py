#!/usr/bin/env python3
"""Update Flamingo & Cactus share-preview metadata and favicon artwork.

Run from any working directory after placing this file in scripts/ and the
new PNG in assets/:

    python3 scripts/update_share_preview.py
    python3 scripts/build.py

The script edits source files only. The regular masthead logo (assets/logo.png)
is intentionally left unchanged. Existing files are backed up before edits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_JSON = ROOT / "data" / "site.json"
BUILDER = ROOT / "scripts" / "build.py"
ICON_SOURCE = ROOT / "assets" / "flamingo-cactus-share-icon.png"
FAVICON = ROOT / "assets" / "favicon.png"

NEW_TITLE = "Flamingo & Cactus — The transparent group"
NEW_DESCRIPTION = (
    "The website dedicated to the Flamingo and Cactus group. "
    "Everything Flamingo and Cactus can be found here."
)
OG_IMAGE_ALT = "Flamingo and Cactus on a golden yellow-orange background"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def backup_once(path: Path, stamp: str) -> Path:
    backup = path.with_name(path.name + f".before-share-preview-{stamp}.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".share-preview-tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != PNG_SIGNATURE:
        raise ValueError(f"{path} is not a valid PNG image")
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if width < 1 or height < 1:
        raise ValueError(f"{path} has invalid PNG dimensions")
    return width, height


def update_site_json(stamp: str) -> bool:
    if not SITE_JSON.is_file():
        raise FileNotFoundError(f"Missing source file: {SITE_JSON}")
    doc = json.loads(SITE_JSON.read_text(encoding="utf-8"))
    doc["description"] = NEW_DESCRIPTION

    home = next((p for p in doc.get("pages", []) if p.get("slug") == "index"), None)
    if home is None:
        raise ValueError('Could not find the homepage entry (slug="index") in data/site.json')
    home["title"] = NEW_TITLE
    home["description"] = NEW_DESCRIPTION

    updated = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    current = SITE_JSON.read_text(encoding="utf-8")
    if current == updated:
        print("Already correct: data/site.json")
        return False
    backup_once(SITE_JSON, stamp)
    atomic_write(SITE_JSON, updated)
    print("Updated: data/site.json (homepage title + description)")
    return True


def patch_builder(stamp: str, image_width: int, image_height: int, cache_version: str) -> bool:
    if not BUILDER.is_file():
        raise FileNotFoundError(f"Missing build script: {BUILDER}")
    source = BUILDER.read_text(encoding="utf-8")
    original = source

    # Keep schema.org's organization description consistent with the page.
    ld_pattern = re.compile(
        r'("description":\s*)\(\s*'
        r'"Open-source hardware from the Flamingo & Cactus "\s*'
        r'"group: the Lamoka1/2 open inverter, the "\s*'
        r'"Lamoka1-DEV-Brain, and every transaction published "\s*'
        r'"for transparency\."\s*\),',
        re.MULTILINE,
    )
    source, ld_count = ld_pattern.subn(
        lambda match: match.group(1) + json.dumps(NEW_DESCRIPTION), source, count=1
    )
    if ld_count == 0 and NEW_DESCRIPTION not in source:
        raise RuntimeError(
            "Could not find the expected old Organization description in scripts/build.py. "
            "No builder changes were written; inspect the LD_ORG block before editing manually."
        )

    # Use the new icon as the social-preview image. The content hash in the
    # query string makes iMessage and other cached previews fetch the new image.
    og_image_url = f"https://flamingoandcactus.com/favicon.png?v={cache_version}"
    favicon_href = f"favicon.png?v={cache_version}"
    old_og_url = "https://{CUSTOM_DOMAIN}/logo.png"
    if old_og_url in source:
        source = source.replace(old_og_url, og_image_url, 1)
    elif og_image_url not in source:
        raise RuntimeError(
            "Could not find the expected og:image URL in scripts/build.py. "
            "No builder changes were written; inspect render_page() before editing manually."
        )

    # Cache-bust the browser favicon too. The build already copies
    # assets/favicon.png to site/favicon.png, so no build-system redesign is needed.
    old_icon_href = 'href="favicon.png"'
    if old_icon_href in source:
        source = source.replace(old_icon_href, f'href="{favicon_href}"', 1)
    elif favicon_href not in source:
        raise RuntimeError(
            "Could not find the expected favicon href in scripts/build.py. "
            "No builder changes were written; inspect render_page() before editing manually."
        )

    old_twitter = 'content="summary"'
    if old_twitter in source:
        source = source.replace(old_twitter, 'content="summary_large_image"', 1)

    if source == original:
        print("Already correct: scripts/build.py")
        return False

    backup_once(BUILDER, stamp)
    atomic_write(BUILDER, source)
    print("Updated: scripts/build.py (Open Graph image, favicon, Twitter card, schema description)")
    return True


def main() -> int:
    try:
        for required in (SITE_JSON, BUILDER, ICON_SOURCE):
            if not required.is_file():
                raise FileNotFoundError(f"Required project file is missing: {required}")

        width, height = read_png_size(ICON_SOURCE)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        image_bytes = ICON_SOURCE.read_bytes()
        cache_version = hashlib.sha256(image_bytes).hexdigest()[:12]

        # Update the HTML generator and JSON source, then install the image.
        # The regular masthead logo remains untouched.
        patch_builder(stamp, width, height, cache_version)
        update_site_json(stamp)
        if not FAVICON.is_file() or FAVICON.read_bytes() != image_bytes:
            if FAVICON.is_file():
                backup_once(FAVICON, stamp)
            shutil.copy2(ICON_SOURCE, FAVICON)
            print(f"Installed: assets/favicon.png ({width}x{height})")
        else:
            print("Already correct: assets/favicon.png")

        print("\nSource files updated. Rebuild the published site with:")
        print("  python3 scripts/build.py")
        print("Then review the generated site/ directory before committing and publishing.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

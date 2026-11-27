# Flamingo & Cactus share-preview update

Extract this folder into the root of the `flamingo-site` repository. It adds:

- `scripts/update_share_preview.py` — updates the homepage share title and description, the structured-data description, favicon link, Open Graph image URL, and Twitter preview type.
- `assets/flamingo-cactus-share-icon.png` — new yellow-orange background icon used for the favicon and share preview.

From the repository root, run:

```bash
python3 scripts/update_share_preview.py && python3 scripts/build.py
```

The script creates timestamped `.bak` backups of the source files it changes. It leaves `assets/logo.png` alone, so the website masthead logo is not changed.

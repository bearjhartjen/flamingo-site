#!/usr/bin/env python3
"""
Sync the Desktop ledger into the repository with its private notes encrypted.

    python3 scripts/encrypt_flank.py            # sync (default)
    python3 scripts/encrypt_flank.py --check    # roundtrip-verify, write nothing
    python3 scripts/encrypt_flank.py --decrypt  # print the private notes (local helper)

The ledger's source of truth is ~/Desktop/The Flank.md, which stays in
plaintext -- it is the only plaintext copy. Every transaction carries a
"- **Private:** yes/no" line; on sync, each "yes" row's Notes are replaced
with an encrypted [[FCEnc ...]] token (see scripts/flank_crypto.py). The
repo copy (data/The Flank.md) therefore never holds the private note text,
and neither does the built page.

Everything except the encrypted Notes values passes through byte-for-byte,
and the token IVs are deterministic, so the same input always produces the
same output file: repeated syncs are no-ops, not diffs.

The passphrase comes from the local keyfile (flank_crypto.KEYFILE). If it
is missing the sync FAILS -- it never falls back to copying plaintext,
because a repo copy without encryption is exactly what this script exists
to prevent.

--decrypt is the owner's local method to read the notes from the repo copy
without the Desktop file (and the method used to verify the roundtrip).
It prints to the terminal only; it never writes plaintext to the repo.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flank_crypto as fc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = Path.home() / "Desktop" / "The Flank.md"
REPO_DATA = ROOT / "data" / "The Flank.md"

# Splits the ledger into [before, "## Transaction N", body, ...] pieces so
# every byte outside a transformed Notes value survives untouched.
BLOCK_SPLIT = re.compile(r"(^##\s+Transaction\s+(\d+)\s*$)", re.M)
NOTES_LINE = re.compile(r"^(?P<prefix>-\s+\*\*Notes:\*\*\s*)(?P<value>.*)$", re.M)
PRIVATE_LINE = re.compile(r"^-\s+\*\*Private:\*\*\s*(.*)$", re.M)


def _fallback_private(num: int):
    """The built-in set, used only when a row has no Private: field at all."""
    try:
        import build_flank
        return num in build_flank.PRIVATE_TX
    except Exception:
        return False


def encrypt_ledger_text(text: str, key: bytes, source_name: str) -> str:
    """Return the ledger text with every private note replaced by a token."""
    parts = BLOCK_SPLIT.split(text)
    out = [parts[0]]
    encrypted = 0
    missing_flag = []
    for i in range(1, len(parts), 3):
        header, num, body = parts[i], int(parts[i + 1]), parts[i + 2]

        pm = PRIVATE_LINE.search(body)
        flag = fc.private_flag(pm.group(1) if pm else None)
        if flag is None:
            flag = _fallback_private(num)
            missing_flag.append(num)

        if not flag:
            out += [header, body]
            continue

        nm = NOTES_LINE.search(body)
        if not nm:
            raise SystemExit(
                f"transaction {num} is marked private but has no Notes line "
                f"in {source_name}; refusing to sync it"
            )
        if pm is None:
            # Defensive: make the repo copy explicit about the decision.
            body = body[: nm.start()] + "- **Private:** yes\n" + body[nm.start():]
            nm = NOTES_LINE.search(body)

        value = nm.group("value")
        plaintext = value.replace("\\$", "$")
        if not plaintext.strip():
            print(
                f"warning: transaction {num} is private with an empty note; "
                "nothing to encrypt",
                file=sys.stderr,
            )
            out += [header, body]
            continue

        nlen, iv, ct = fc.encrypt_note(key, num, plaintext)
        body = body[: nm.start("value")] + fc.token(nlen, iv, ct) + body[nm.end("value"):]
        encrypted += 1
        out += [header, body]

    if missing_flag:
        print(
            f"note: {len(missing_flag)} transaction(s) without a Private: field "
            f"in {source_name}: {missing_flag}",
            file=sys.stderr,
        )
    print(f"encrypted {encrypted} private note(s)")
    return "".join(out)


def write_if_changed(path: Path, text: str) -> bool:
    """Write via a temp file + rename; skip when content is unchanged."""
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def load_repo_records(path: Path):
    """{num: fields} from a ledger file, for --check / --decrypt."""
    return dict(fc.parse_ledger(path.read_text(encoding="utf-8")))


def cmd_sync(src: Path, dst: Path, key: bytes) -> int:
    if not src.is_file():
        print(f"ledger source not found: {src}", file=sys.stderr)
        return 1
    new_text = encrypt_ledger_text(src.read_text(encoding="utf-8"), key, src.name)
    if write_if_changed(dst, new_text):
        print(f"wrote {dst}")
    else:
        print(f"repo copy already up to date: {dst}")
    return 0


def cmd_check(src: Path, dst: Path, key: bytes) -> int:
    """Verify the repo copy is the correct encryption of the Desktop copy."""
    ok = True

    def expect(cond, msg):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        ok = ok and cond

    if not src.is_file():
        print(f"SKIP  ledger source not present: {src}")
        return 0
    if not dst.is_file():
        expect(False, f"repo copy exists: {dst}")
        return 1

    src_text = src.read_text(encoding="utf-8")
    dst_text = dst.read_text(encoding="utf-8")

    # 1. Determinism: re-encrypting the source reproduces the repo copy exactly.
    expect(
        encrypt_ledger_text(src_text, key, src.name) == dst_text,
        "repo copy is exactly the encrypted form of the Desktop copy",
    )

    # 2. Roundtrip: every token decrypts to the exact Desktop note.
    src_fields = dict(fc.parse_ledger(src_text))
    dst_fields = dict(fc.parse_ledger(dst_text))
    expect(set(src_fields) == set(dst_fields), "both copies carry the same transactions")

    roundtrip_failures = 0
    for num, fields in sorted(src_fields.items()):
        flag = fc.private_flag(fields.get("private"))
        if flag is None:
            flag = _fallback_private(num)
        if not flag:
            continue
        expected = fields.get("notes", "").replace("\\$", "$")
        got = dst_fields.get(num, {}).get("notes", "")
        tok = fc.parse_token(got)
        if tok is None:
            print(f"FAIL  transaction {num}: repo copy has no encrypted token")
            ok = False
            roundtrip_failures += 1
            continue
        nlen, iv_b64, ct_b64 = tok
        try:
            plain = fc.decrypt_note(
                key, num, fc.b64_bytes(iv_b64), fc.b64_bytes(ct_b64)
            )
        except fc.CryptoError as exc:
            print(f"FAIL  transaction {num}: decryption failed: {exc}")
            ok = False
            roundtrip_failures += 1
            continue
        if plain != expected or len(plain) != nlen:
            print(f"FAIL  transaction {num}: decrypted text does not match the Desktop note")
            ok = False
            roundtrip_failures += 1
    if roundtrip_failures == 0:
        n_priv = sum(
            1 for num, f in src_fields.items()
            if (fc.private_flag(f.get("private")) if fc.private_flag(f.get("private")) is not None else _fallback_private(num))
        )
        expect(True, f"all {n_priv} private notes decrypt to the exact Desktop text")

    # 3. No plaintext: no private note text may appear anywhere in the repo copy.
    leaks = 0
    for num, fields in sorted(src_fields.items()):
        flag = fc.private_flag(fields.get("private"))
        if flag is None:
            flag = _fallback_private(num)
        if not flag:
            continue
        expected = fields.get("notes", "").replace("\\$", "$")
        if expected and expected in dst_text:
            print(f"FAIL  transaction {num}: plaintext note text found in the repo copy")
            ok = False
            leaks += 1
    if leaks == 0:
        expect(True, "no private note text appears in the repo copy")

    if not ok:
        raise SystemExit("check FAILED")
    print("check passed: the repo ledger is the faithful encrypted form of the Desktop ledger")
    return 0


def cmd_decrypt(dst: Path, key: bytes) -> int:
    """Print the private notes from the repo copy (owner's local helper)."""
    if not dst.is_file():
        print(f"repo copy not found: {dst}", file=sys.stderr)
        return 1
    fields_by_num = load_repo_records(dst)
    count = 0
    for num, fields in sorted(fields_by_num.items()):
        tok = fc.parse_token(fields.get("notes", ""))
        if tok is None:
            continue
        nlen, iv_b64, ct_b64 = tok
        plain = fc.decrypt_note(key, num, fc.b64_bytes(iv_b64), fc.b64_bytes(ct_b64))
        print(f"Transaction {num}: {plain}")
        count += 1
    if not count:
        print("no encrypted tokens found in the repo copy")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Sync the Desktop ledger into the repo with private notes encrypted."
    )
    ap.add_argument("--src", type=Path, default=DESKTOP, help="plaintext ledger (default: %(default)s)")
    ap.add_argument("--dst", type=Path, default=REPO_DATA, help="repo ledger to write (default: %(default)s)")
    ap.add_argument("--keyfile", type=Path, default=fc.KEYFILE, help="passphrase keyfile (default: %(default)s)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="roundtrip-verify the repo copy; write nothing")
    mode.add_argument("--decrypt", action="store_true", help="print the private notes from the repo copy")
    args = ap.parse_args(argv)

    key = fc.derive_key(fc.load_passphrase(args.keyfile))
    if args.check:
        return cmd_check(args.src, args.dst, key)
    if args.decrypt:
        return cmd_decrypt(args.dst, key)
    return cmd_sync(args.src, args.dst, key)


if __name__ == "__main__":
    sys.exit(main())

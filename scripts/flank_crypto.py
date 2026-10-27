#!/usr/bin/env python3
"""
Crypto for the Flank ledger's private notes.

The Desktop ledger (~/Desktop/The Flank.md) is the ONLY plaintext copy of
the ledger. When it is synced into the repository, the notes of every
transaction marked "- **Private:** yes" are replaced with an [[FCEnc ...]]
token that carries ciphertext instead of note text:

    [[FCEnc n:<chars> iv:<base64> ct:<base64>]]

  n    the note's character length (public; it sizes the blurred
       placeholder on the locked page so the table keeps its shape)
  iv   the AES-CBC initialisation vector (public, one per note)
  ct   the ciphertext (base64, PKCS#7-padded AES-256-CBC)

The key is derived from a passphrase held only on this laptop, in a keyfile
outside the repository (default ~/.config/flamingo-site/flank-notes.key):

    key = PBKDF2-HMAC-SHA256(passphrase, salt, 600000 rounds, 32 bytes)

The salt below is public and lives in this file; a salt is not a secret.
The browser repeats exactly this derivation with the Web Crypto API (see
the lock section of build_flank.py), so one passphrase decrypts the notes
both here and on the published page.

Two honest properties:

  * The IV is derived deterministically from the key and the transaction
    number (HMAC-SHA256), not sampled at random. That makes the encryption
    idempotent -- the same ledger encrypts to byte-identical output, so
    repeated syncs produce no spurious diffs. An unchanged note also
    encrypts to the same ciphertext in every build, which reveals nothing
    that the public amounts and dates do not already show.
  * AES-CBC (not an authenticated mode) is used because the system
    `openssl enc` tool offers no AEAD mode and this project stays
    stdlib-only. The threat model is a reader of the repository or the
    page source, not an active tamperer. A wrong passphrase still fails
    loudly here and in the browser (PKCS#7 padding plus an exact length
    check), so it can never quietly "decode" to something wrong.

The AES work itself is never hand-rolled: this module shells out to the
system openssl binary for every encryption and decryption.

Stdlib only. No secrets are stored in the repository.
"""

import base64
import hashlib
import hmac
import re
import subprocess
import sys
from pathlib import Path

# Public salt for the PBKDF2 derivation. Changing it invalidates every
# published ciphertext at once.
SALT_HEX = "34fba5a2798673521591f9f11ff92619"

# The same salt as base64 -- the form the page script embeds, because the
# browser-side helpers decode base64.
SALT_B64 = base64.b64encode(bytes.fromhex(SALT_HEX)).decode("ascii")

# PBKDF2 round count. Must match the `iters` value in build_flank.py's
# page script; both must match for the browser to derive the same key.
PBKDF2_ITERS = 600_000

# Where the passphrase lives. Outside the repository; never committed.
KEYFILE = Path.home() / ".config" / "flamingo-site" / "flank-notes.key"

# [[FCEnc n:<len> iv:<b64> ct:<b64>]]
TOKEN_RE = re.compile(
    r"\[\[FCEnc n:(\d+) iv:([A-Za-z0-9+/=]+) ct:([A-Za-z0-9+/=]+)\]\]"
)


class CryptoError(Exception):
    """An encryption/decryption step failed."""


# ------------------------------------------------------------------- tokens
def token(nlen: int, iv: bytes, ct: bytes) -> str:
    """Render an encrypted-notes token for the ledger markdown."""
    return (
        "[[FCEnc n:" + str(nlen)
        + " iv:" + _b64(iv)
        + " ct:" + _b64(ct) + "]]"
    )


def parse_token(value: str):
    """Parse a token into (nlen, iv_b64, ct_b64), or None if not a token."""
    m = TOKEN_RE.fullmatch(value.strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64_bytes(b64: str) -> bytes:
    """Decode one base64 field of a token (an IV or a ciphertext)."""
    return base64.b64decode(b64)


# ------------------------------------------------------------------- key
def load_passphrase(keyfile: Path = KEYFILE) -> str:
    """Read the passphrase from the local keyfile (outside the repo)."""
    if not keyfile.is_file():
        raise SystemExit(
            f"passphrase keyfile not found: {keyfile}\n"
            "The private notes cannot be encrypted without it, and the repo\n"
            "copy of the ledger must never hold plaintext notes, so this is\n"
            "a hard stop -- not a fallback. Create the keyfile with:\n"
            "  mkdir -p ~/.config/flamingo-site\n"
            "  (umask 077 && printf 'YOURPASSPHRASE' > " + str(KEYFILE) + ")"
        )
    passphrase = keyfile.read_text(encoding="utf-8").strip()
    if not passphrase:
        raise SystemExit(f"passphrase keyfile is empty: {keyfile}")
    return passphrase


def derive_key(passphrase: str) -> bytes:
    """PBKDF2-HMAC-SHA256 -> 32-byte AES-256 key. Mirrored in the browser."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        bytes.fromhex(SALT_HEX),
        PBKDF2_ITERS,
        dklen=32,
    )


def note_iv(key: bytes, num: int) -> bytes:
    """Deterministic per-note IV: HMAC over the salt and transaction number.

    Deterministic, not random, so the same ledger encrypts to the same
    bytes on every sync (idempotent builds, no diff churn). Every note
    gets a distinct IV because the transaction number is distinct.
    """
    return hmac.new(
        key,
        bytes.fromhex(SALT_HEX) + b"FCEnc-iv:" + str(num).encode("ascii"),
        hashlib.sha256,
    ).digest()[:16]


# ------------------------------------------------------------------- AES
def _openssl_cbc(key: bytes, iv: bytes, data: bytes, decrypt: bool = False) -> bytes:
    """One AES-256-CBC round via the system openssl binary (PKCS#7 padding)."""
    cmd = ["openssl", "enc", "-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex()]
    if decrypt:
        cmd.append("-d")
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True)
    except FileNotFoundError:
        raise CryptoError(
            "the openssl binary was not found; it is required to encrypt "
            "the ledger notes"
        )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise CryptoError(f"openssl failed: {detail}")
    return proc.stdout


def encrypt_note(key: bytes, num: int, text: str):
    """Encrypt one note. Returns (nlen, iv, ct)."""
    iv = note_iv(key, num)
    ct = _openssl_cbc(key, iv, text.encode("utf-8"))
    return len(text), iv, ct


def decrypt_note(key: bytes, num: int, iv: bytes, ct: bytes) -> str:
    """Decrypt one note and validate it. Raises CryptoError on failure."""
    plain = _openssl_cbc(key, iv, ct, decrypt=True)
    try:
        text = plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CryptoError(
            f"decrypted bytes are not valid UTF-8 (wrong passphrase?): {exc}"
        )
    return text


# ------------------------------------------------------------------- parse
def parse_ledger(text: str):
    """Split the ledger markdown into (number, fields) pairs."""
    blocks = re.split(r"^##\s+Transaction\s+(\d+)\s*$", text, flags=re.M)
    txs = []
    for i in range(1, len(blocks), 2):
        num = int(blocks[i])
        fields = {}
        for line in blocks[i + 1].splitlines():
            m = re.match(r"^-\s+\*\*(.+?):\*\*\s*(.*)$", line.strip())
            if m:
                fields[m.group(1).strip().lower()] = m.group(2).strip()
        txs.append((num, fields))
    return txs


def private_flag(raw):
    """Parse a '**Private:**' field value.

    Returns True, False, or None when the field is absent (the caller
    decides the fallback). An unrecognised value fails CLOSED -- treated
    as private -- so a typo can never publish a note by accident.
    """
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if v in ("yes", "y", "true", "1"):
        return True
    if v in ("no", "n", "false", "0"):
        return False
    print(
        f"warning: unrecognised Private value {raw!r}; treating as private",
        file=sys.stderr,
    )
    return True

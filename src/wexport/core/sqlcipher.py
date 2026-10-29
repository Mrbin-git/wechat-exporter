"""SQLCipher 4 / WCDB page-level decryption for WeChat 4.x databases.

Verified against WeChat 4.1.15.51 (Windows) on real message_0.db:
  - key material: 32-byte passphrase, PBKDF2-HMAC-SHA512(iter=256000) -> enc key
  - mac key:      PBKDF2-HMAC-SHA512(enc_key, salt^0x3a, iter=2)
  - page 1:       16-byte salt prefix, AES-256-CBC, IV in page tail
  - HMAC:         SHA512(page[16 or 0 : 4096-80+16] + page_no LE32)
  - reserve:      80 bytes (IV 16 + HMAC 64)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE = IV_SIZE + HMAC_SIZE
KEY_SIZE = 32
SQLITE_HEADER = b"SQLite format 3\x00"
KDF_ITER = 256000


def derive_keys(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    enc = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=KEY_SIZE)
    mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(b ^ 0x3A for b in salt), 2, dklen=KEY_SIZE)
    return enc, mac


def page_hmac(mac_key: bytes, page: bytes, page_no: int) -> bytes:
    off = SALT_SIZE if page_no == 1 else 0
    end = PAGE_SIZE - RESERVE + IV_SIZE
    m = hmac.new(mac_key, digestmod=hashlib.sha512)
    m.update(page[off:end])
    m.update(struct.pack("<I", page_no))
    return m.digest()


def verify_key(passphrase: bytes, page1: bytes) -> bool:
    if len(page1) < PAGE_SIZE:
        return False
    if page1.startswith(SQLITE_HEADER):
        return True
    salt = page1[:SALT_SIZE]
    _enc, mac = derive_keys(passphrase, salt)
    return hmac.compare_digest(page_hmac(mac, page1, 1), page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE])


def is_plain_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == SQLITE_HEADER
    except OSError:
        return False


@dataclass
class DecryptResult:
    source: Path
    output: Path | None = None
    pages: int = 0
    bad_pages: int = 0
    copied: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error == "" and self.output is not None


def _decrypt_one_page(enc_key: bytes, page: bytes, page_no: int) -> bytes:
    iv = page[PAGE_SIZE - RESERVE: PAGE_SIZE - RESERVE + IV_SIZE]
    body = page[SALT_SIZE if page_no == 1 else 0: PAGE_SIZE - RESERVE]
    d = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).decryptor()
    plain = d.update(body) + d.finalize()
    if page_no == 1:
        return SQLITE_HEADER + plain + b"\x00" * RESERVE
    return plain + b"\x00" * RESERVE


def decrypt_database(
    source: Path,
    output: Path,
    passphrase: bytes,
    *,
    verify: bool = True,
    progress=None,
) -> DecryptResult:
    """Decrypt one WeChat 4.x SQLCipher db into a plain SQLite file.

    Streams page by page so multi-hundred-MB databases do not need to fit in RAM.
    """
    source = Path(source)
    output = Path(output)
    res = DecryptResult(source=source)
    if not source.is_file():
        res.error = "数据库文件不存在"
        return res
    head = source.read_bytes()[:PAGE_SIZE] if source.stat().st_size >= PAGE_SIZE else b""
    if not head:
        res.error = "数据库文件过小"
        return res
    if head.startswith(SQLITE_HEADER):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(source.read_bytes())
        res.copied = True
        res.output = output
        res.pages = len(head) // PAGE_SIZE
        return res

    salt = head[:SALT_SIZE]
    enc_key, mac_key = derive_keys(passphrase, salt)
    if verify and not hmac.compare_digest(page_hmac(mac_key, head, 1), head[PAGE_SIZE - HMAC_SIZE:]):
        res.error = "密钥校验失败（不匹配该数据库）"
        return res

    total = (source.stat().st_size + PAGE_SIZE - 1) // PAGE_SIZE
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    bad = 0
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            for i in range(total):
                page = src.read(PAGE_SIZE)
                if not page:
                    break
                if len(page) < PAGE_SIZE:
                    page = page + b"\x00" * (PAGE_SIZE - len(page))
                if i > 0 and not hmac.compare_digest(page_hmac(mac_key, page, i + 1), page[PAGE_SIZE - HMAC_SIZE:]):
                    bad += 1
                dst.write(_decrypt_one_page(enc_key, page, i + 1))
                if progress and (i % 512 == 0 or i == total - 1):
                    progress(i + 1, total)
        if bad:
            res.warnings.append(f"{bad} 页 HMAC 校验失败（可能为并发写入，已按密文解密）")
        os.replace(tmp, output)
        res.output = output
        res.pages = total
        res.bad_pages = bad
    except OSError as exc:
        res.error = f"写入失败: {exc}"
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return res


def decrypt_tree(db_storage: Path, out_dir: Path, passphrase: bytes, progress=None) -> list[DecryptResult]:
    results = []
    dbs = sorted(p for p in db_storage.rglob("*.db") if p.is_file())
    for idx, db in enumerate(dbs):
        rel = db.relative_to(db_storage)
        target = out_dir / rel
        r = decrypt_database(db, target, passphrase)
        results.append(r)
        if progress:
            progress(idx + 1, len(dbs), rel)
    return results

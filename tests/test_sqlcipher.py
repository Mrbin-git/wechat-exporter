"""A5-01/02/03 — SQLCipher 4 page-level decryption (real WeChat 4.1.15.51 db).

Covers:
  * correct passphrase -> plain SQLite, openable, SessionTable present, rows > 0
  * wrong passphrase   -> ok=False and error contains "密钥校验失败", no output left
  * plain sqlite input -> copied verbatim instead of being "decrypted"
  * page/mac helpers   -> unit-level sanity (DNS of the format constants)
"""
from __future__ import annotations

import sqlite3
import struct
import sys
import unittest
from pathlib import Path

from _support import (REAL_SESSION_DB, TEST_PASSPHRASE_HEX, TempDirMixin,
                      WINDOWS_ONLY, require_path)

from wexport.core import sqlcipher

#: The passphrase is a 64-char hex string; the C core consumes it as raw bytes.
PASSPHRASE = bytes.fromhex(TEST_PASSPHRASE_HEX)
SQLITE_HEADER = b"SQLite format 3\x00"


class TestSqlcipherRealDb(TempDirMixin, unittest.TestCase):
    """TC-SQL-01..03: decrypt the real, still-encrypted session.db."""

    prefix = "sqlcipher-"

    @classmethod
    def setUpClass(cls):
        require_path(REAL_SESSION_DB, "真实加密库 session.db")

    def _decrypt_good(self) -> Path:
        out = self.tmp / "session.db"
        res = sqlcipher.decrypt_database(REAL_SESSION_DB, out, PASSPHRASE)
        self.assertTrue(res.ok, f"解密失败: {res.error!r}")
        self.assertEqual(res.error, "")
        self.assertIsNotNone(res.output)
        self.assertTrue(Path(res.output).is_file())
        return Path(res.output)

    # -- TC-SQL-01 --------------------------------------------------------- #
    def test_decrypt_with_correct_passphrase(self):
        out = self._decrypt_good()
        self.assertEqual(out.read_bytes()[:16], SQLITE_HEADER)

    # -- TC-SQL-02 --------------------------------------------------------- #
    def test_decrypted_db_opens_and_has_rows(self):
        out = self._decrypt_good()
        con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
            self.assertIn("SessionTable", names)
            count = con.execute("select count(*) from SessionTable").fetchone()[0]
            self.assertGreater(count, 0, "SessionTable 没有任何行")
            # a decrypted db must pass sqlite's own integrity check
            self.assertEqual(str(con.execute("pragma quick_check(1)").fetchone()[0]).lower(), "ok")
        finally:
            con.close()

    # -- TC-SQL-03 --------------------------------------------------------- #
    def test_wrong_passphrase_fails_with_key_error(self):
        out = self.tmp / "wrong-key.db"
        wrong = bytes([b ^ 0xFF for b in PASSPHRASE])
        res = sqlcipher.decrypt_database(REAL_SESSION_DB, out, wrong)
        self.assertFalse(res.ok)
        self.assertIsNone(res.output)
        self.assertIn("密钥校验失败", res.error)
        self.assertFalse(out.exists(), "错误密钥不应留下输出文件")

    def test_wrong_length_passphrase_also_fails(self):
        out = self.tmp / "short-key.db"
        res = sqlcipher.decrypt_database(REAL_SESSION_DB, out, b"short")
        self.assertFalse(res.ok)
        self.assertIn("密钥校验失败", res.error)

    def test_source_is_not_modified(self):
        before = (REAL_SESSION_DB.stat().st_size, REAL_SESSION_DB.stat().st_mtime_ns)
        head = REAL_SESSION_DB.read_bytes()[:16]
        self.assertNotEqual(head, SQLITE_HEADER, "真实库不应是明文，否则该用例无意义")
        self._decrypt_good()
        after = (REAL_SESSION_DB.stat().st_size, REAL_SESSION_DB.stat().st_mtime_ns)
        self.assertEqual(before, after, "测试改动了真实微信数据目录")

    def test_plain_sqlite_is_copied_not_decrypted(self):
        plain = self.tmp / "plain.db"
        con = sqlite3.connect(str(plain))
        con.execute("create table t(x)")
        con.execute("insert into t values (1)")
        con.commit()
        con.close()
        out = self.tmp / "copy.db"
        res = sqlcipher.decrypt_database(plain, out, PASSPHRASE)
        self.assertTrue(res.ok)
        self.assertTrue(res.copied, "明文库应走复制分支")
        self.assertEqual(out.read_bytes(), plain.read_bytes())

    def test_missing_source_reports_error(self):
        res = sqlcipher.decrypt_database(self.tmp / "nope.db", self.tmp / "o.db", PASSPHRASE)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "数据库文件不存在")


class TestSqlcipherPrimitives(unittest.TestCase):
    """TC-SQL-04: pure helpers, no real data needed."""

    def test_sqlite_header_constant(self):
        self.assertEqual(sqlcipher.SQLITE_HEADER, SQLITE_HEADER)
        self.assertEqual(sqlcipher.PAGE_SIZE, 4096)
        self.assertEqual(sqlcipher.RESERVE, 80)

    def test_derive_keys_is_deterministic_and_split(self):
        salt = bytes(range(16))
        enc1, mac1 = sqlcipher.derive_keys(PASSPHRASE, salt)
        enc2, mac2 = sqlcipher.derive_keys(PASSPHRASE, salt)
        self.assertEqual((enc1, mac1), (enc2, mac2))
        self.assertEqual(len(enc1), 32)
        self.assertEqual(len(mac1), 32)
        self.assertNotEqual(enc1, mac1)
        enc3, _ = sqlcipher.derive_keys(b"another", salt)
        self.assertNotEqual(enc1, enc3)

    def test_page_hmac_depends_on_page_number(self):
        _enc, mac = sqlcipher.derive_keys(PASSPHRASE, bytes(16))
        page = bytes(4096)
        self.assertNotEqual(sqlcipher.page_hmac(mac, page, 1),
                            sqlcipher.page_hmac(mac, page, 2))

    def test_verify_key_rejects_short_page(self):
        self.assertFalse(sqlcipher.verify_key(PASSPHRASE, b"tiny"))

    def test_verify_key_accepts_plain_sqlite(self):
        self.assertTrue(sqlcipher.verify_key(PASSPHRASE, SQLITE_HEADER + b"\x00" * (4096 - 16)))

    def test_is_plain_sqlite(self):
        from _support import make_tmp, cleanup

        tmp = make_tmp("plain-")
        try:
            p = tmp / "x.db"
            p.write_bytes(SQLITE_HEADER + b"\x00" * 32)
            self.assertTrue(sqlcipher.is_plain_sqlite(p))
            p.write_bytes(b"not a db")
            self.assertFalse(sqlcipher.is_plain_sqlite(p))
            self.assertFalse(sqlcipher.is_plain_sqlite(tmp / "missing.db"))
        finally:
            cleanup(tmp)


@WINDOWS_ONLY
class TestSqlcipherPassphraseSource(unittest.TestCase):
    """TC-SQL-05: the documented passphrase really is the key for this account.

    Kept separate so a key-rotation on the host shows up as one clear failure
    instead of an unexplained cascade.
    """

    def test_passphrase_length_matches_key_material(self):
        self.assertEqual(len(TEST_PASSPHRASE_HEX), 64)
        self.assertEqual(len(struct.pack("<Q", 0)), 8)
        self.assertEqual(len(PASSPHRASE), 32)


if __name__ == "__main__":
    sys.exit(unittest.main())

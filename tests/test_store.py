"""持久层测试。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ptcheckin.secretsbox import SecretsBox
from ptcheckin.store import Store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptcheckin-test-"))
        self.secrets = SecretsBox(self.tmp / "secret.key")
        self.store = Store(self.tmp / "test.db", self.secrets)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAccounts(StoreTestCase):
    def test_create_and_get(self):
        acct = self.store.create_account(
            {
                "name": "主号",
                "base_url": "https://hhanclub.net",
                "cookie": "c_secure_uid=U; c_secure_pass=P",
            }
        )
        self.assertEqual(acct["name"], "主号")
        self.assertTrue(acct["enabled"])
        self.assertEqual(acct["cookie"], "c_secure_uid=U; c_secure_pass=P")

    def test_cookie_is_encrypted_at_rest(self):
        acct = self.store.create_account(
            {"name": "a", "base_url": "https://x.net", "cookie": "c_secure_pass=SECRET"}
        )
        row = self.store._one("SELECT cookie_enc FROM accounts WHERE id = ?", (acct["id"],))
        self.assertNotIn("SECRET", row["cookie_enc"])
        # 读回时应能解密
        self.assertEqual(self.store.get_account(acct["id"], decrypt=True)["cookie"],
                         "c_secure_pass=SECRET")

    def test_masked_cookie_hides_secrets(self):
        acct = self.store.create_account(
            {"name": "a", "base_url": "https://x.net", "cookie": "c_secure_uid=UID123; c_secure_pass=SECRET"}
        )
        listed = self.store.get_account(acct["id"])
        self.assertNotIn("SECRET", listed["cookie_masked"])
        self.assertIn("c_secure_uid=UID123", listed["cookie_masked"])

    def test_unique_name(self):
        self.store.create_account({"name": "dup", "base_url": "https://x.net", "cookie": "a=1"})
        with self.assertRaises(Exception):
            self.store.create_account({"name": "dup", "base_url": "https://x.net", "cookie": "a=1"})

    def test_update(self):
        acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        updated = self.store.update_account(acct["id"], {"enabled": False, "schedule_time": "06:15"})
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["schedule_time"], "06:15")

    def test_update_cookie(self):
        acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        self.store.update_account(acct["id"], {"cookie": "a=2"})
        self.assertEqual(self.store.get_account(acct["id"], decrypt=True)["cookie"], "a=2")

    def test_delete_cascades(self):
        acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        self.store.upsert_day(acct["id"], "2026-09-15", signed=True, points=5)
        self.assertTrue(self.store.delete_account(acct["id"]))
        self.assertEqual(self.store.list_days(acct["id"]), [])
        self.assertEqual(self.store.list_attempts(acct["id"])[1], 0)

    def test_only_enabled_filter(self):
        self.store.create_account({"name": "on", "base_url": "https://x.net", "cookie": "a=1", "enabled": True})
        self.store.create_account({"name": "off", "base_url": "https://x.net", "cookie": "a=1", "enabled": False})
        self.assertEqual(len(self.store.list_accounts()), 2)
        self.assertEqual(len(self.store.list_accounts(only_enabled=True)), 1)


class TestDays(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        self.aid = int(self.acct["id"])

    def test_upsert_and_get(self):
        self.store.upsert_day(self.aid, "2026-09-15", signed=True, points=125, streak=24)
        day = self.store.get_day(self.aid, "2026-09-15")
        self.assertEqual(day["points"], 125)
        self.assertEqual(day["streak"], 24)
        self.assertEqual(day["signed"], 1)

    def test_upsert_is_idempotent_and_keeps_signed(self):
        self.store.upsert_day(self.aid, "2026-09-15", signed=True, points=125)
        self.store.upsert_day(self.aid, "2026-09-15", signed=False)
        day = self.store.get_day(self.aid, "2026-09-15")
        self.assertEqual(day["signed"], 1, "已签到的记录不应被后续未签到写入覆盖")
        self.assertEqual(day["points"], 125)

    def test_month_days(self):
        self.store.upsert_day(self.aid, "2026-09-01", signed=True, points=1)
        self.store.upsert_day(self.aid, "2026-09-30", signed=True, points=2)
        self.store.upsert_day(self.aid, "2026-10-01", signed=True, points=3)
        days = self.store.month_days(self.aid, "2026-09")
        self.assertEqual({d["check_date"] for d in days}, {"2026-09-01", "2026-09-30"})

    def test_total_signed_and_stats(self):
        self.store.upsert_day(self.aid, "2026-09-15", signed=True, points=10)
        stats = self.store.stats("2026-09-15")
        self.assertEqual(stats["today_signed"], 1)
        self.assertEqual(stats["today_pending"], 0)
        self.assertEqual(stats["accounts_enabled"], 1)


class TestStreak(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        self.aid = int(self.acct["id"])

    def _mark(self, day: date):
        self.store.upsert_day(self.aid, day.isoformat(), signed=True)

    def test_consecutive_including_today(self):
        today = date(2026, 9, 15)
        for i in range(5):
            self._mark(today - timedelta(days=i))
        self.assertEqual(self.store.compute_streak(self.aid, today.isoformat()), 5)

    def test_today_missing_counts_yesterday_backwards(self):
        today = date(2026, 9, 15)
        for i in range(1, 4):
            self._mark(today - timedelta(days=i))
        self.assertEqual(self.store.compute_streak(self.aid, today.isoformat()), 3)

    def test_gap_breaks_streak(self):
        today = date(2026, 9, 15)
        self._mark(today)
        self._mark(today - timedelta(days=1))
        self._mark(today - timedelta(days=3))
        self.assertEqual(self.store.compute_streak(self.aid, today.isoformat()), 2)

    def test_no_records(self):
        self.assertEqual(self.store.compute_streak(self.aid, "2026-09-15"), 0)

    def test_retroactive_day_breaks_streak(self):
        """补签（is_retroactive=1）不计入连续签到，且会中断连续。"""
        today = date(2026, 9, 15)
        for i in range(3):  # 09-15 / 09-14 / 09-13 正常签到
            self._mark(today - timedelta(days=i))
        self.store.upsert_day(
            self.aid, (today - timedelta(days=3)).isoformat(), signed=True, is_retroactive=1
        )
        self._mark(today - timedelta(days=4))  # 09-11 正常签到
        self.assertEqual(self.store.compute_streak(self.aid, today.isoformat()), 3)

    def test_signed_dates_filter_for_retroactive(self):
        today = date(2026, 9, 15)
        self.store.upsert_day(self.aid, today.isoformat(), signed=True, is_retroactive=1)
        self.assertEqual(
            self.store.signed_dates(self.aid, today.isoformat(), today.isoformat()), set()
        )
        self.assertEqual(
            self.store.signed_dates(
                self.aid, today.isoformat(), today.isoformat(), exclude_retroactive=False
            ),
            {today.isoformat()},
        )


class TestAttempts(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.acct = self.store.create_account({"name": "a", "base_url": "https://x.net", "cookie": "a=1"})
        self.aid = int(self.acct["id"])

    def _attempt(self, status, check_date="2026-09-15", started="2026-09-15T03:00:00Z", **kw):
        data = {
            "account_id": self.aid,
            "account_name": "a",
            "check_date": check_date,
            "trigger": "auto",
            "status": status,
            "started_at": started,
            "finished_at": started,
        }
        data.update(kw)
        return self.store.add_attempt(data)

    def test_add_and_list(self):
        self._attempt("success", is_new=1, points=125)
        items, total = self.store.list_attempts(self.aid)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["points"], 125)
        self.assertEqual(items[0]["is_new"], 1)

    def test_count_by_status(self):
        self._attempt("failed")
        self._attempt("network_error")
        self._attempt("success", is_new=1)
        self.assertEqual(self.store.count_attempts(self.aid, "2026-09-15"), 3)
        self.assertEqual(
            self.store.count_attempts(self.aid, "2026-09-15", statuses=("failed", "network_error")), 2
        )

    def test_last_attempt(self):
        self._attempt("failed", started="2026-09-15T01:00:00Z")
        self._attempt("success", started="2026-09-15T02:00:00Z", is_new=1)
        last = self.store.last_attempt(self.aid, "2026-09-15")
        self.assertEqual(last["status"], "success")

    def test_pagination_and_filter(self):
        for i in range(25):
            self._attempt("success" if i % 2 else "failed",
                          started=f"2026-09-15T{i:02d}:00:00Z", is_new=1 if i % 2 else 0)
        items, total = self.store.list_attempts(self.aid, page=1, page_size=10)
        self.assertEqual(total, 25)
        self.assertEqual(len(items), 10)
        items2, _ = self.store.list_attempts(self.aid, page=3, page_size=10)
        self.assertEqual(len(items2), 5)
        only_failed, total_failed = self.store.list_attempts(self.aid, status="failed")
        self.assertEqual(total_failed, 13)

    def test_date_range_filter(self):
        self._attempt("success", started="2026-09-10T00:00:00Z")
        self._attempt("success", started="2026-09-20T00:00:00Z")
        _, total = self.store.list_attempts(self.aid, date_from="2026-09-15", date_to="2026-09-30")
        self.assertEqual(total, 1)

    def test_last_success_date(self):
        self._attempt("success", check_date="2026-09-14", is_new=1)
        self._attempt("success", check_date="2026-09-15", is_new=1)
        self.assertEqual(self.store.last_success_date(self.aid), "2026-09-15")

    def test_purge_raw(self):
        self._attempt("success", started="2020-01-01T00:00:00Z", raw_snippet="x" * 100)
        self._attempt("success", raw_snippet="y" * 100)
        self.store.purge_raw(keep_days=30)
        items, _ = self.store.list_attempts(self.aid)
        old = [i for i in items if i["started_at"].startswith("2020")][0]
        new = [i for i in items if not i["started_at"].startswith("2020")][0]
        self.assertIsNone(old["raw_snippet"])
        self.assertIsNotNone(new["raw_snippet"])


class TestKv(StoreTestCase):
    def test_set_get(self):
        self.store.kv_set("k", {"a": 1})
        self.assertEqual(self.store.kv_get("k"), {"a": 1})
        self.assertIsNone(self.store.kv_get("missing"))
        self.assertEqual(self.store.kv_get("missing", "d"), "d")


class TestEncryption(StoreTestCase):
    def test_roundtrip(self):
        for text in ("plain", "中文 cookie", "", "a" * 500):
            token = self.secrets.encrypt(text)
            self.assertEqual(self.secrets.decrypt(token), text)

    def test_wrong_key_fails_closed(self):
        token = self.secrets.encrypt("secret")
        other = SecretsBox(self.tmp / "other.key")
        self.assertEqual(other.decrypt(token), "")

    def test_plaintext_passthrough(self):
        self.assertEqual(self.secrets.decrypt("c_secure_uid=1"), "c_secure_uid=1")


if __name__ == "__main__":
    unittest.main()

"""签到主流程测试：使用假的 HTTP 客户端，完全离线。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import ptcheckin.checkin as checkin_mod
from ptcheckin.checkin import CheckinService
from ptcheckin.client import HttpResponse
from ptcheckin.notify import Notifier
from ptcheckin.secretsbox import SecretsBox
from ptcheckin.settings import Settings
from ptcheckin.store import Store
from tests.test_parser import ANON_PAGE, build_page

TZ = ZoneInfo("Asia/Shanghai")
# 全部为占位符，非真实凭据
COOKIE = "c_secure_uid=MTIzNDU%3D; c_secure_pass=0123456789abcdef0123456789abcdef; c_secure_login=bm9wZQ%3D%3D"


def ledger_with_today(created_at: str, date_str: str = "2026-09-15") -> str:
    return (
        '{"2026-09-14":{"id":1,"uid":2,"points":120,"date":"2026-09-14",'
        '"is_retroactive":0,"created_at":"2026-09-14 00:54:51","updated_at":"2026-09-14 00:54:51"},'
        f'"{date_str}":{{"id":2,"uid":2,"points":125,"date":"{date_str}",'
        f'"is_retroactive":0,"created_at":"{created_at}","updated_at":"{created_at}"}}}}'
    )


def ledger_without_today() -> str:
    return (
        '{"2026-09-13":{"id":1,"uid":2,"points":10,"date":"2026-09-13",'
        '"is_retroactive":0,"created_at":"2026-09-13 00:10:00","updated_at":"2026-09-13 00:10:00"},'
        '"2026-09-14":{"id":2,"uid":2,"points":120,"date":"2026-09-14",'
        '"is_retroactive":0,"created_at":"2026-09-14 00:54:51","updated_at":"2026-09-14 00:54:51"}}'
    )


class FakeClient:
    """可编程的假客户端。"""

    response: HttpResponse = HttpResponse(status=200, url="", body=b"")
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def attendance(self, timeout=None):
        FakeClient.calls.append("attendance")
        return FakeClient.response


def page_response(html: str, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, url="https://hhanclub.net/attendance.php",
                        body=html.encode("utf-8"), elapsed_ms=123)


class CheckinTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptcheckin-checkin-"))
        self.settings = Settings(data_dir=self.tmp)
        self.settings.update({"timezone": "Asia/Shanghai", "default_jitter_seconds": 0})
        self.secrets = SecretsBox(self.tmp / "secret.key")
        self.store = Store(self.tmp / "test.db", self.secrets)
        self.service = CheckinService(self.store, self.settings, Notifier(self.settings))
        self.account = self.store.create_account(
            {
                "name": "主号",
                "base_url": "https://hhanclub.net",
                "cookie": COOKIE,
                "timezone": "Asia/Shanghai",
            }
        )
        FakeClient.calls = []
        self._orig_client = checkin_mod.SiteClient
        checkin_mod.SiteClient = FakeClient  # type: ignore[misc]

    def tearDown(self):
        checkin_mod.SiteClient = self._orig_client  # type: ignore[misc]
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        acct = self.store.get_account(int(self.account["id"]), decrypt=True)
        return self.service.run(acct, trigger="manual", notify=False, **kw)

    # ------------------------------------------------------------- scenarios
    def test_fresh_signin_is_success(self):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"), ledger=ledger_with_today(created, today))
        )
        result = self._run()
        self.assertEqual(result.status, "success")
        self.assertTrue(result.is_new)
        self.assertEqual(result.points, 125)
        self.assertEqual(result.streak, 24)
        self.assertEqual(result.rank, 4371)
        self.assertEqual(result.check_date, today)

        day = self.store.get_day(int(self.account["id"]), today)
        self.assertEqual(day["signed"], 1)
        self.assertEqual(day["points"], 125)
        # 台账里的历史日期也被同步
        self.assertIsNotNone(self.store.get_day(int(self.account["id"]), "2026-09-14"))

    def test_already_signed_today(self):
        """站点记录时间明显早于本次请求 → 判定为"今日已签到"而非新签到。"""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"),
                       ledger=ledger_with_today("2020-01-01 08:00:00", today))
        )
        result = self._run()
        self.assertEqual(result.status, "already")
        self.assertFalse(result.is_new)
        self.assertTrue(result.ok)

    def test_second_run_same_day_is_already(self):
        """连续两次运行：第一次成功，第二次应为 already（不再重复计数）。"""
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"), ledger=ledger_with_today(created, today))
        )
        first = self._run()
        self.assertEqual(first.status, "success")
        second = self._run()
        self.assertEqual(second.status, "already")

    def test_missing_today_record_is_failure(self):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"), ledger=ledger_without_today(), message="")
        )
        result = self._run()
        self.assertEqual(result.status, "failed")
        self.assertIn("未返回今日签到记录", result.error)

    def test_expired_cookie_is_auth_failed(self):
        FakeClient.response = page_response(ANON_PAGE)
        result = self._run()
        self.assertEqual(result.status, "auth_failed")
        self.assertIn("登录状态失效", result.error)

    def test_network_error(self):
        FakeClient.response = HttpResponse(status=0, url="", error="网络错误: timed out")
        result = self._run()
        self.assertEqual(result.status, "network_error")
        self.assertIn("timed out", result.error)

    def test_invalid_cookie_skips_http(self):
        self.store.update_account(int(self.account["id"]), {"cookie": "not-a-cookie"})
        result = self._run()
        self.assertEqual(result.status, "auth_failed")
        self.assertEqual(FakeClient.calls, [], "Cookie 明显无效时不应发起请求")

    def test_attempt_is_recorded(self):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"), ledger=ledger_with_today(created, today))
        )
        result = self._run()
        items, total = self.store.list_attempts(int(self.account["id"]))
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["status"], "success")
        self.assertEqual(items[0]["http_status"], 200)
        self.assertEqual(items[0]["duration_ms"], 123)
        self.assertIsNotNone(items[0]["raw_snippet"])
        self.assertEqual(items[0]["account_id"], int(self.account["id"]))

    def test_recovered_flag(self):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        # 先失败一次
        FakeClient.response = page_response(ANON_PAGE)
        self._run()
        # 再成功
        created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        FakeClient.response = page_response(
            build_page(now_date=today.replace("-", "/"), ledger=ledger_with_today(created, today))
        )
        result = self._run()
        self.assertEqual(result.status, "success")
        self.assertTrue(result.recovered)

    def test_site_date_uses_page_value_not_local(self):
        """即使本地日期与站点日期不同，也以站点 nowDate 为准。"""
        FakeClient.response = page_response(
            build_page(now_date="2030/01/02", ledger=ledger_with_today("2030-01-02 08:00:00", "2030-01-02"))
        )
        result = self._run()
        self.assertEqual(result.site_date, "2030-01-02")
        self.assertEqual(result.check_date, "2030-01-02")
        self.assertIsNotNone(self.store.get_day(int(self.account["id"]), "2030-01-02"))

    def test_concurrent_run_is_rejected(self):
        import threading

        started = threading.Event()
        release = threading.Event()

        class SlowClient(FakeClient):
            def attendance(self, timeout=None):
                started.set()
                release.wait(5)
                return FakeClient.response

        checkin_mod.SiteClient = SlowClient  # type: ignore[misc]
        FakeClient.response = page_response(ANON_PAGE)
        acct = self.store.get_account(int(self.account["id"]), decrypt=True)
        thread = threading.Thread(target=lambda: self.service.run(acct, "auto", notify=False))
        thread.start()
        self.assertTrue(started.wait(3))
        second = self.service.run(acct, "auto", notify=False)
        self.assertIn("已有签到任务在执行中", second.error)
        release.set()
        thread.join(5)


if __name__ == "__main__":
    unittest.main()

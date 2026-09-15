"""调度逻辑测试：定时、随机延迟、重试与补签。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ptcheckin.checkin import CheckinService
from ptcheckin.notify import Notifier
from ptcheckin.scheduler import Scheduler, parse_hhmm, stable_jitter
from ptcheckin.secretsbox import SecretsBox
from ptcheckin.settings import Settings
from ptcheckin.store import Store

COOKIE = "c_secure_uid=U; c_secure_pass=P"


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptcheckin-sched-"))
        self.settings = Settings(data_dir=self.tmp)
        self.settings.update(
            {
                "timezone": "Asia/Shanghai",
                "default_schedule_time": "00:30",
                "default_jitter_seconds": 0,
                "max_retries_per_day": 2,
                "retry_interval_seconds": 900,
            }
        )
        self.secrets = SecretsBox(self.tmp / "secret.key")
        self.store = Store(self.tmp / "test.db", self.secrets)
        self.service = CheckinService(self.store, self.settings, Notifier(self.settings))
        self.scheduler = Scheduler(self.store, self.settings, self.service)
        self.account = self.store.create_account(
            {"name": "a", "base_url": "https://hhanclub.net", "cookie": COOKIE,
             "timezone": "Asia/Shanghai"}
        )
        self.aid = int(self.account["id"])

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, now):
        acct = self.store.get_account(self.aid, decrypt=True)
        return self.scheduler.plan(acct, now)

    # ---------------------------------------------------------------- basics
    def test_parse_hhmm(self):
        self.assertEqual((parse_hhmm("00:30").hour, parse_hhmm("00:30").minute), (0, 30))
        self.assertEqual((parse_hhmm("6:05").hour, parse_hhmm("6:05").minute), (6, 5))
        self.assertIsNone(parse_hhmm("25:00"))
        self.assertIsNone(parse_hhmm("abc"))
        self.assertIsNone(parse_hhmm(None))

    def test_stable_jitter_is_deterministic_and_in_range(self):
        a = stable_jitter(7, "2026-09-15", 600)
        b = stable_jitter(7, "2026-09-15", 600)
        self.assertEqual(a, b)
        self.assertTrue(0 <= a <= 600)
        self.assertNotEqual(stable_jitter(7, "2026-09-15", 600), stable_jitter(8, "2026-09-15", 600))
        self.assertNotEqual(stable_jitter(7, "2026-09-15", 600), stable_jitter(7, "2026-09-16", 600))
        self.assertEqual(stable_jitter(7, "2026-09-15", 0), 0)

    # ------------------------------------------------------------------ plan
    def test_pending_before_target(self):
        # 北京时间 2026-09-15 00:10（UTC 前一天 16:10），目标 00:30
        plan = self._plan(utc(2026, 9, 14, 16, 10))
        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["date"], "2026-09-15")
        self.assertEqual(plan["target_at"], "2026-09-14T16:30:00Z")

    def test_due_after_target(self):
        plan = self._plan(utc(2026, 9, 15, 1, 0))  # 北京 09:00
        self.assertEqual(plan["state"], "due")
        self.assertTrue(plan["notify"])

    def test_catch_up_after_long_downtime(self):
        """服务停机很久后启动，当天已过签到时间 → 立即补签。"""
        plan = self._plan(utc(2026, 9, 15, 12, 0))  # 北京 20:00
        self.assertEqual(plan["state"], "due")

    def test_done_after_signed(self):
        self.store.upsert_day(self.aid, "2026-09-15", signed=True, points=125)
        plan = self._plan(utc(2026, 9, 15, 1, 0))
        self.assertEqual(plan["state"], "done")
        self.assertTrue(plan["signed"])
        self.assertTrue(plan["next_run_at"].startswith("2026-09-15T16:30:00"))

    def test_waiting_retry_after_failure(self):
        self.store.add_attempt({
            "account_id": self.aid, "check_date": "2026-09-15", "trigger": "auto",
            "status": "failed", "started_at": "2026-09-15T01:00:00Z",
            "finished_at": "2026-09-15T01:00:01Z",
        })
        plan = self._plan(utc(2026, 9, 15, 1, 5))  # 距上次失败 5 分钟 < 15 分钟
        self.assertEqual(plan["state"], "waiting_retry")
        self.assertEqual(plan["next_run_at"], "2026-09-15T01:15:00Z")

        plan2 = self._plan(utc(2026, 9, 15, 1, 20))  # 超过间隔 → 可重试
        self.assertEqual(plan2["state"], "due")
        self.assertFalse(plan2["notify"], "重试时不应重复发送失败通知")

    def test_exhausted_after_max_retries(self):
        # max_retries_per_day=2 → 允许 1 次首发 + 2 次重试 = 3 次失败
        for i in range(3):
            self.store.add_attempt({
                "account_id": self.aid, "check_date": "2026-09-15", "trigger": "retry",
                "status": "failed", "started_at": f"2026-09-15T0{i}:00:00Z",
                "finished_at": f"2026-09-15T0{i}:00:01Z",
            })
        plan = self._plan(utc(2026, 9, 15, 5, 0))
        self.assertEqual(plan["state"], "exhausted")
        self.assertTrue(plan["next_run_at"].startswith("2026-09-15T16:30:00"))

    def test_last_allowed_attempt_notifies(self):
        for i in range(2):
            self.store.add_attempt({
                "account_id": self.aid, "check_date": "2026-09-15", "trigger": "retry",
                "status": "failed", "started_at": f"2026-09-15T0{i}:00:00Z",
                "finished_at": f"2026-09-15T0{i}:00:01Z",
            })
        plan = self._plan(utc(2026, 9, 15, 5, 0))
        self.assertEqual(plan["state"], "due")
        self.assertTrue(plan["notify"], "达到重试上限的最后一次应发送通知")

    def test_disabled_account(self):
        self.store.update_account(self.aid, {"enabled": False})
        plan = self._plan(utc(2026, 9, 15, 5, 0))
        self.assertEqual(plan["state"], "disabled")

    def test_invalid_schedule(self):
        self.store.update_account(self.aid, {"schedule_time": "99:99"})
        plan = self._plan(utc(2026, 9, 15, 5, 0))
        self.assertEqual(plan["state"], "invalid_schedule")
        self.assertIn("签到时间格式", plan["error"])

    def test_per_account_schedule_and_jitter_override(self):
        self.store.update_account(self.aid, {"schedule_time": "06:00", "jitter_seconds": 60})
        plan = self._plan(utc(2026, 9, 14, 21, 59))  # 北京 05:59
        self.assertEqual(plan["state"], "pending")
        self.assertEqual(plan["schedule_time"], "06:00")
        self.assertTrue(0 <= plan["jitter_seconds"] <= 60)
        # 目标时间 = 06:00 + jitter
        expected = f"2026-09-14T22:00:{plan['jitter_seconds']:02d}Z"
        self.assertEqual(plan["target_at"], expected)

    def test_yesterday_failure_does_not_block_today(self):
        self.store.add_attempt({
            "account_id": self.aid, "check_date": "2026-09-14", "trigger": "retry",
            "status": "failed", "started_at": "2026-09-14T05:00:00Z",
            "finished_at": "2026-09-14T05:00:01Z",
        })
        plan = self._plan(utc(2026, 9, 15, 5, 0))
        self.assertEqual(plan["state"], "due")
        self.assertEqual(plan["failures"], 0)

    # ------------------------------------------------------------------ tick
    def test_tick_submits_due_accounts(self):
        submitted = []

        def fake_submit(account, plan):
            submitted.append((account["name"], plan["state"]))

        self.scheduler._submit = fake_submit  # type: ignore[assignment]
        result = self.scheduler.tick(utc(2026, 9, 15, 5, 0))
        self.assertEqual(len(result), 1)
        self.assertEqual(submitted, [("a", "due")])

    def test_tick_skips_when_disabled(self):
        self.settings.update({"scheduler_enabled": False})
        calls = []
        self.scheduler._submit = lambda a, p: calls.append(a)  # type: ignore[assignment]
        self.assertEqual(self.scheduler.tick(utc(2026, 9, 15, 5, 0)), [])
        self.assertEqual(calls, [])

    def test_tick_skips_already_signed(self):
        self.store.upsert_day(self.aid, "2026-09-15", signed=True)
        calls = []
        self.scheduler._submit = lambda a, p: calls.append(a)  # type: ignore[assignment]
        self.assertEqual(self.scheduler.tick(utc(2026, 9, 15, 5, 0)), [])
        self.assertEqual(calls, [])

    def test_next_run_overall(self):
        nxt = self.scheduler.next_run_overall(utc(2026, 9, 14, 16, 10))
        self.assertEqual(nxt, "2026-09-14T16:30:00Z")

    def test_status_shape(self):
        status = self.scheduler.status()
        for key in ("running", "enabled", "tick_seconds", "next_run_at", "server_time_utc"):
            self.assertIn(key, status)


if __name__ == "__main__":
    unittest.main()

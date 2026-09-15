"""解析器测试：覆盖真实页面结构、字符串内花括号、未登录页等场景。"""

from __future__ import annotations

import unittest

from ptcheckin.parser import (
    extract_ledger,
    extract_message,
    extract_now_date,
    match_brace,
    normalize_date,
    parse_attendance,
)

# 与 HHClub 真实页面保持一致的结构（含 nowDate 与内嵌台账 JSON）
PAGE_TEMPLATE = """
<html><head><title>HHCLUB - Powered by NexusPHP</title></head>
<body>
<div class="register-rank"><p class="register-info">
        今日签到排名：4371 / 4381</p></div>
<script>
    const nowDate = new Date("{now_date}");

    function firstCreate() {{
        let data = {ledger};
    }}

    function retroactive(start) {{
        jQuery.post('ajax.php', {{params: {{timestamp: 1}}, action: 'attendanceRetroactive'}});
    }}
</script>
<p class="register-now-info register-info">{message}</p>
</body></html>
"""

DEFAULT_MESSAGE = (
    "这是您的第24次签到，已连续签到24天，本次签到获得125个憨豆。你目前拥有补签卡0张。"
)


def build_page(now_date="2026/09/15", ledger=None, message=DEFAULT_MESSAGE):
    if ledger is None:
        ledger = (
            '{"2026-08-23":{"id":5657353,"uid":12345,"points":10,"date":"2026-08-23",'
            '"is_retroactive":0,"created_at":"2026-08-23 13:40:32","updated_at":"2026-08-23 13:40:32"},'
            '"2026-09-14":{"id":5795223,"uid":12345,"points":120,"date":"2026-09-14",'
            '"is_retroactive":0,"created_at":"2026-09-14 00:54:51","updated_at":"2026-09-14 00:54:51"},'
            '"2026-09-15":{"id":5805306,"uid":12345,"points":125,"date":"2026-09-15",'
            '"is_retroactive":0,"created_at":"2026-09-15 11:07:51","updated_at":"2026-09-15 11:07:51"}}'
        )
    return PAGE_TEMPLATE.format(now_date=now_date, ledger=ledger, message=message)


# 站点未登录时返回的截断页面（HTTP 200 但没有任何签到数据）
ANON_PAGE = (
    "<html><head><title>HHCLUB - Powered by NexusPHP</title></head>"
    '<body ><!-- 容器 --><div id="Container"><!-- 顶部菜单 -->        '
)


class TestHelpers(unittest.TestCase):
    def test_match_brace_simple(self):
        text = 'xx {"a": {"b": 1}} yy'
        start = text.index("{")
        end = match_brace(text, start)
        self.assertEqual(text[start : end + 1], '{"a": {"b": 1}}')

    def test_match_brace_ignores_braces_in_strings(self):
        text = '{"a": "} not a brace {", "b": {"c": 2}}'
        end = match_brace(text, 0)
        self.assertEqual(text[: end + 1], text)

    def test_match_brace_handles_escaped_quote(self):
        text = r'{"a": "quote \" and } brace", "b": 1}'
        end = match_brace(text, 0)
        self.assertEqual(text[: end + 1], text)

    def test_match_brace_unbalanced(self):
        self.assertEqual(match_brace('{"a": 1', 0), -1)

    def test_normalize_date(self):
        self.assertEqual(normalize_date("2026/09/15"), "2026-09-15")
        self.assertEqual(normalize_date("2026-9-5"), "2026-09-05")
        self.assertIsNone(normalize_date(""))
        self.assertIsNone(normalize_date("garbage"))


class TestExtract(unittest.TestCase):
    def test_extract_now_date(self):
        self.assertEqual(extract_now_date(build_page()), "2026-09-15")
        self.assertIsNone(extract_now_date(ANON_PAGE))

    def test_extract_ledger(self):
        records = extract_ledger(build_page())
        self.assertEqual(len(records), 3)
        rec = records["2026-09-15"]
        self.assertEqual(rec.points, 125)
        self.assertEqual(rec.uid, 12345)
        self.assertEqual(rec.created_at, "2026-09-15 11:07:51")
        self.assertEqual(rec.date, "2026-09-15")

    def test_extract_ledger_empty_object(self):
        self.assertEqual(extract_ledger(build_page(ledger="{}")), {})

    def test_extract_ledger_array(self):
        self.assertEqual(extract_ledger(build_page(ledger="[]")), {})

    def test_extract_ledger_ignores_unrelated_json(self):
        page = build_page() + '<script>let other = {"foo": {"bar": 1}};</script>'
        self.assertEqual(len(extract_ledger(page)), 3)

    def test_extract_ledger_survives_braces_inside_values(self):
        ledger = (
            '{"2026-09-15":{"id":1,"uid":2,"points":5,"date":"2026-09-15",'
            '"is_retroactive":0,"created_at":"2026-09-15 00:01:02",'
            '"updated_at":"2026-09-15 00:01:02","note":"包含 } 与 { 的字符串"}}'
        )
        records = extract_ledger(build_page(ledger=ledger))
        self.assertEqual(records["2026-09-15"].points, 5)

    def test_extract_message(self):
        message, info = extract_message(build_page())
        self.assertIn("第24次签到", message)
        self.assertEqual(info["total_count"], 24)
        self.assertEqual(info["streak"], 24)
        self.assertEqual(info["points"], 125)
        self.assertEqual(info["retro_cards"], 0)
        self.assertEqual(info["rank"], 4371)
        self.assertEqual(info["rank_total"], 4381)


class TestParseAttendance(unittest.TestCase):
    def test_authenticated_and_signed_today(self):
        page = parse_attendance(build_page())
        self.assertTrue(page.authenticated)
        self.assertEqual(page.site_date, "2026-09-15")
        self.assertEqual(page.site_date_source, "page")
        self.assertTrue(page.signed_today)
        self.assertEqual(page.today_record.points, 125)
        self.assertEqual(page.streak, 24)
        self.assertIsNone(page.error)

    def test_not_signed_today(self):
        ledger = (
            '{"2026-09-13":{"id":1,"uid":2,"points":10,"date":"2026-09-13",'
            '"is_retroactive":0,"created_at":"2026-09-13 01:00:00","updated_at":"2026-09-13 01:00:00"},'
            '"2026-09-14":{"id":2,"uid":2,"points":15,"date":"2026-09-14",'
            '"is_retroactive":0,"created_at":"2026-09-14 01:00:00","updated_at":"2026-09-14 01:00:00"}}'
        )
        page = parse_attendance(build_page(ledger=ledger, message=""))
        self.assertTrue(page.authenticated)
        self.assertEqual(page.site_date, "2026-09-15")
        self.assertFalse(page.signed_today)
        self.assertIsNone(page.today_record)

    def test_retroactive_flag(self):
        ledger = (
            '{"2026-09-15":{"id":1,"uid":2,"points":10,"date":"2026-09-15",'
            '"is_retroactive":1,"created_at":"2026-09-15 10:00:00","updated_at":"2026-09-15 10:00:00"}}'
        )
        page = parse_attendance(build_page(ledger=ledger))
        self.assertEqual(page.today_record.is_retroactive, 1)

    def test_anon_page_is_not_authenticated(self):
        page = parse_attendance(ANON_PAGE)
        self.assertFalse(page.authenticated)
        self.assertIsNotNone(page.error)
        self.assertIn("登录状态失效", page.error)

    def test_login_form_detected(self):
        html = ANON_PAGE + '<form action="takelogin.php"><input name="username"/></form>'
        page = parse_attendance(html)
        self.assertFalse(page.authenticated)
        self.assertIn("Cookie", page.error)

    def test_banned_account_detected(self):
        html = ANON_PAGE + "<div>你的账号已被禁用</div>"
        page = parse_attendance(html)
        self.assertFalse(page.authenticated)
        self.assertIn("禁用", page.error)

    def test_message_fallback_when_ledger_missing(self):
        """台账解析失败但站点提示存在时，仍应判定"已签到"。"""
        page = parse_attendance(build_page(ledger="{}"), fallback_date="2026-09-15")
        self.assertTrue(page.authenticated)
        self.assertEqual(page.site_date, "2026-09-15")
        self.assertTrue(page.signed_today)

    def test_site_date_from_ledger_when_nowdate_missing(self):
        html = build_page().replace('const nowDate = new Date("2026/09/15");', "")
        page = parse_attendance(html)
        self.assertEqual(page.site_date, "2026-09-15")
        self.assertEqual(page.site_date_source, "ledger")

    def test_empty_body(self):
        page = parse_attendance("")
        self.assertFalse(page.authenticated)
        self.assertEqual(page.error, "响应为空")

    def test_to_dict(self):
        data = parse_attendance(build_page()).to_dict()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["record_count"], 3)
        self.assertTrue(data["signed_today"])


if __name__ == "__main__":
    unittest.main()

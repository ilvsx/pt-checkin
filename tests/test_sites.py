"""多站点兼容测试：HDFans（FullCalendar 台账）与站点档案。

页面结构取自真实抓包，但所有账号相关数值均已替换为占位符。
"""

from __future__ import annotations

import unittest

from ptcheckin.parser import (
    extract_calendar_events,
    extract_ledger,
    extract_message,
    extract_signin_bonus,
    parse_attendance,
)
from ptcheckin.sites import DEFAULT_SITE, get_profile, guess_site, list_profiles

TODAY = "2026-09-15"

# 2026-08-24 只有背景事件没有 title —— 实测存在这种情况，必须也算已签到
EVENTS_WITH_TODAY = (
    '[{"start":"2026-08-24","end":"2026-08-24","display":"background"},'
    '{"start":"2026-09-13","end":"2026-09-13","display":"background"},'
    '{"start":"2026-09-13","end":"2026-09-13","title":10},'
    '{"start":"2026-09-14","end":"2026-09-14","display":"background"},'
    '{"start":"2026-09-14","end":"2026-09-14","title":20},'
    '{"start":"2026-09-15","end":"2026-09-15","display":"background"},'
    '{"start":"2026-09-15","end":"2026-09-15","title":60}]'
)

EVENTS_WITHOUT_TODAY = (
    '[{"start":"2026-08-24","end":"2026-08-24","display":"background"},'
    '{"start":"2026-09-13","end":"2026-09-13","display":"background"},'
    '{"start":"2026-09-13","end":"2026-09-13","title":10},'
    '{"start":"2026-09-14","end":"2026-09-14","display":"background"},'
    '{"start":"2026-09-14","end":"2026-09-14","title":20}]'
)

MESSAGE_HTML = (
    "这是您的第 <b>192</b> 次签到，已连续签到 <b>6</b> 天，本次签到获得 <b>60</b> 个魔力值。"
    "点击白色背景的圆点进行补签。你目前拥有补签卡 <b>5</b> 张。"
    '<span style="float:right">今日签到排名：<b>7898</b> / <b>8732</b></span>'
)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body>
<div id="userbar">
  <font class='color_bonus'>魔力值 </font>[<a href="mybonus.php">使用</a>]: 1,441,463.0
  <a href="attendance.php" class="">[签到已得{bonus}, 补签卡: {cards}]</a>
  <a href="medal.php">[勋章]</a>
</div>
<table class="main"><tr><td class="embedded">
<h2 align="left">{heading}</h2>
<table width="100%" border="1" cellspacing="0" cellpadding="10"><tr><td class="text">
<p>{message}</p></td></tr></table>
<div id="calendar"></div>
<ul><li>首次签到获得 10 个魔力值。</li><li>每次连续签到可额外获得 10 个魔力值，直到 100 封顶。</li>
<li><ol><li>连续签到第 10 天，额外获得 10 魔力值。</li><li>连续签到第 20 天，额外获得 20 魔力值。</li></ol></li></ul>
</td></tr></table>
<script type="text/javascript" src="vendor/fullcalendar-5.10.2/main.min.js"></script>
<script type="text/javascript">
let events = JSON.parse('{events}');
jQuery(function () {{ $('#calendar').fullCalendar({{events: events}}); }});
function retroactive(dateStr) {{
    jQuery.post('ajax.php', {{params: {{date: dateStr}}, action: 'attendanceRetroactive'}}, function (response) {{}});
}}
</script>
</body></html>
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><title>HDFans :: 登录 - Powered by NexusPHP</title></head>
<body>
<form method="post" action="takelogin.php">
  <input type="text" name="username"/>
  <input type="password" name="password"/>
  <input type="submit" value="登录"/>
</form>
</body></html>
"""


def build_page(events: str = EVENTS_WITH_TODAY, message: str = MESSAGE_HTML,
               bonus: int = 60, cards: int = 5, heading: str = "签到成功",
               title: str = "HDFans :: 签到 - Powered by NexusPHP") -> str:
    return PAGE_TEMPLATE.format(events=events, message=message, bonus=bonus,
                                cards=cards, heading=heading, title=title)


class TestHdfansLedger(unittest.TestCase):
    def test_events_ledger_detected(self):
        records, fmt = extract_ledger(build_page())
        self.assertEqual(fmt, "events")
        self.assertEqual(set(records), {"2026-08-24", "2026-09-13", "2026-09-14", "2026-09-15"})

    def test_points_from_title(self):
        records = extract_calendar_events(build_page())
        self.assertEqual(records["2026-09-15"].points, 60)
        self.assertEqual(records["2026-09-13"].points, 10)

    def test_background_only_date_counts_as_signed(self):
        """只有背景事件、没有 title 的日期也必须算已签到。"""
        records = extract_calendar_events(build_page())
        self.assertIn("2026-08-24", records)
        self.assertIsNone(records["2026-08-24"].points)

    def test_background_only_date_is_marked_retroactive(self):
        """只有背景事件的日期是补签：不计积分，也不计入连续签到。

        依据：实测每个这类日期之后连续奖励都会重置
        （09-08 得 100 → 09-09 无积分 → 09-10 得 10）。
        """
        records = extract_calendar_events(build_page())
        self.assertEqual(records["2026-08-24"].is_retroactive, 1)

    def test_normal_date_is_not_retroactive(self):
        records = extract_calendar_events(build_page())
        self.assertEqual(records["2026-09-15"].is_retroactive, 0)
        self.assertEqual(records["2026-09-15"].points, 60)

    def test_zero_point_title_is_not_retroactive(self):
        """title 为 0 表示"有积分记录"，不应被当成补签。"""
        page = build_page(events='[{"start":"2026-09-15","display":"background"},'
                                 '{"start":"2026-09-15","title":0}]')
        self.assertEqual(extract_calendar_events(page)["2026-09-15"].is_retroactive, 0)

    def test_unescapes_js_single_quoted_string(self):
        page = build_page(events=EVENTS_WITH_TODAY.replace('"', '\\"'))
        self.assertEqual(len(extract_calendar_events(page)), 4)

    def test_inline_array_without_json_parse(self):
        page = '<script>var events = %s;</script>' % EVENTS_WITH_TODAY
        records = extract_calendar_events(page)
        self.assertEqual(len(records), 4)

    def test_empty_ledger(self):
        self.assertEqual(extract_calendar_events(build_page(events="[]")), {})
        self.assertEqual(extract_ledger(build_page(events="[]"))[1], "none")


class TestHdfansParse(unittest.TestCase):
    def test_signed_today(self):
        page = parse_attendance(build_page(), fallback_date=TODAY)
        self.assertTrue(page.authenticated)
        self.assertEqual(page.site_date, TODAY)
        self.assertEqual(page.site_date_source, "fallback")
        self.assertEqual(page.ledger_format, "events")
        self.assertTrue(page.signed_today)
        self.assertEqual(page.today_record.points, 60)
        self.assertIsNone(page.error)

    def test_not_signed_today_uses_fallback_not_ledger_max(self):
        """**关键回归**：今天未签到时，站点日期绝不能取台账里最大的日期。

        否则 site_date 会指向"上次签到的日期"，signed_today 恒为 True，
        调度器将永远不会发起签到。
        """
        page = parse_attendance(build_page(events=EVENTS_WITHOUT_TODAY), fallback_date=TODAY)
        self.assertEqual(page.site_date, TODAY, "站点日期必须是账号时区的今天")
        self.assertEqual(page.site_date_source, "fallback")
        self.assertFalse(page.signed_today, "今天不在台账里，必须判定为未签到")
        self.assertIsNone(page.today_record)
        self.assertEqual(len(page.records), 3, "历史台账仍应被解析出来")

    def test_ledger_max_used_only_without_fallback(self):
        page = parse_attendance(build_page(events=EVENTS_WITHOUT_TODAY))
        self.assertEqual(page.site_date, "2026-09-14")
        self.assertEqual(page.site_date_source, "ledger")

    def test_message_parsed(self):
        page = parse_attendance(build_page(), fallback_date=TODAY)
        self.assertEqual(page.total_count, 192)
        self.assertEqual(page.streak, 6)
        self.assertEqual(page.points, 60)
        self.assertEqual(page.points_unit, "魔力值")
        self.assertEqual(page.retro_cards, 5)
        self.assertIn("第 192 次签到", page.message)
        self.assertIn("补签卡 5 张", page.message)

    def test_rank_parsed_despite_bold_tags(self):
        page = parse_attendance(build_page(), fallback_date=TODAY)
        self.assertEqual(page.rank, 7898)
        self.assertEqual(page.rank_total, 8732)

    def test_signin_bonus_from_userbar(self):
        page = parse_attendance(build_page(), fallback_date=TODAY)
        self.assertEqual(page.signin_bonus, 60)

    def test_message_of_the_day_unit_variant(self):
        """站点文案单位变化时仍能解析（憨豆 / 魔力值 / 积分）。"""
        for unit in ("憨豆", "魔力值", "积分"):
            msg = MESSAGE_HTML.replace("魔力值。", f"{unit}。")
            _, info = extract_message(build_page(message=msg))
            self.assertEqual(info["points"], 60, unit)
            self.assertEqual(info["points_unit"], unit)
            self.assertEqual(info["retro_cards"], 5)

    def test_to_dict_exposes_ledger_format(self):
        data = parse_attendance(build_page(), fallback_date=TODAY).to_dict()
        self.assertEqual(data["ledger_format"], "events")
        self.assertEqual(data["points_unit"], "魔力值")


class TestHdfansAuth(unittest.TestCase):
    def test_login_page_is_auth_failed(self):
        page = parse_attendance(LOGIN_PAGE, fallback_date=TODAY)
        self.assertFalse(page.authenticated)
        self.assertIn("登录状态失效", page.error)

    def test_login_title_detected(self):
        html = '<html><head><title>HDFans :: 登录 - Powered by NexusPHP</title></head><body>x</body></html>'
        page = parse_attendance(html, fallback_date=TODAY)
        self.assertFalse(page.authenticated)
        self.assertIn("登录", page.error)

    def test_login_form_beats_stale_ledger(self):
        """同时出现登录表单和残留台账时，以登录表单为准。"""
        page = parse_attendance(LOGIN_PAGE + build_page(), fallback_date=TODAY)
        self.assertFalse(page.authenticated)


class TestSigninBonus(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(extract_signin_bonus(build_page()), (60, 5))

    def test_thousands_separator(self):
        html = '<a href="attendance.php">[签到已得1,234, 补签卡: 2]</a>'
        self.assertEqual(extract_signin_bonus(html), (1234, 2))

    def test_absent(self):
        self.assertEqual(extract_signin_bonus("<html><body>nothing</body></html>"), (None, None))


class TestSiteProfiles(unittest.TestCase):
    def test_known_sites(self):
        self.assertEqual(get_profile("hhanclub").name, "HHClub")
        self.assertEqual(get_profile("hdfans").name, "HDFans")
        self.assertEqual(get_profile("hdfans").points_unit, "魔力值")
        self.assertEqual(get_profile("hhanclub").points_unit, "憨豆")

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(get_profile("nope").key, DEFAULT_SITE)
        self.assertEqual(get_profile(None).key, DEFAULT_SITE)
        self.assertEqual(get_profile("").key, DEFAULT_SITE)

    def test_case_insensitive(self):
        self.assertEqual(get_profile("HDFans").key, "hdfans")

    def test_referer_differs_per_site(self):
        self.assertEqual(get_profile("hhanclub").referer, "mybonus.php")
        self.assertEqual(get_profile("hdfans").referer, "index.php")

    def test_guess_site_from_url(self):
        self.assertEqual(guess_site("https://hdfans.org"), "hdfans")
        self.assertEqual(guess_site("https://hhanclub.net/attendance.php"), "hhanclub")
        self.assertEqual(guess_site("https://www.hdfans.org/"), "hdfans")
        self.assertEqual(guess_site("https://hhanclub.net:443"), "hhanclub")

    def test_guess_site_unknown_returns_host(self):
        self.assertEqual(guess_site("https://example.com"), "example.com")

    def test_list_profiles(self):
        profiles = list_profiles()
        self.assertEqual({p["key"] for p in profiles}, {"hhanclub", "hdfans"})
        for p in profiles:
            self.assertTrue(p["default_base_url"].startswith("http"))


if __name__ == "__main__":
    unittest.main()

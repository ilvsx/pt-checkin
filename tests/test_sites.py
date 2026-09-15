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

# 日历里同时有 background 与 title 事件。实测结论：
#   只有带 title（积分）的日期才算已签到；
#   仅 background 的日期只是"在补签窗口内"，并不代表已签到。
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

# 今天只被标记为 background、还没签到（最危险的场景）
EVENTS_TODAY_BACKGROUND_ONLY = (
    '[{"start":"2026-09-13","end":"2026-09-13","display":"background"},'
    '{"start":"2026-09-13","end":"2026-09-13","title":10},'
    '{"start":"2026-09-15","end":"2026-09-15","display":"background"}]'
)

# 取自 QingWa 真实结构：日历标记 31 天，其中只有 1 天带积分，
# 而站点自报"已连续签到 1 天" —— 这是证明 background ≠ 已签到的关键证据。
QINGWA_EVENTS = (
    '[{"start":"2026-08-16","end":"2026-08-16","display":"background"},'
    '{"start":"2026-09-14","end":"2026-09-14","display":"background"},'
    '{"start":"2026-09-15","end":"2026-09-15","display":"background"},'
    '{"start":"2026-09-15","end":"2026-09-15","title":10}]'
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
        # 只有带 title 的三天算已签到；08-24 仅 background，不算
        self.assertEqual(set(records), {"2026-09-13", "2026-09-14", "2026-09-15"})

    def test_points_from_title(self):
        records = extract_calendar_events(build_page())
        self.assertEqual(records["2026-09-15"].points, 60)
        self.assertEqual(records["2026-09-13"].points, 10)

    def test_background_only_date_is_not_signed(self):
        """**关键**：只有 background 事件、没有积分的日期不算已签到。

        证据：QingWa 日历标记 31 天但只有 1 天带积分，站点自报"连续签到 1 天"。
        若把 background 当已签到，未签到的今天会被误判为已签到，
        调度器将永远不会签到。
        """
        records = extract_calendar_events(build_page())
        self.assertNotIn("2026-08-24", records)

    def test_today_background_only_means_not_signed(self):
        page = build_page(events=EVENTS_TODAY_BACKGROUND_ONLY, message="")
        parsed = parse_attendance(page, fallback_date=TODAY)
        self.assertIn("2026-09-13", parsed.records)
        self.assertFalse(parsed.signed_today, "今天只是被标记，尚未签到")

    def test_normal_date_is_not_retroactive(self):
        records = extract_calendar_events(build_page())
        self.assertEqual(records["2026-09-15"].is_retroactive, 0)
        self.assertEqual(records["2026-09-15"].points, 60)

    def test_zero_point_title_counts_as_signed(self):
        """title 为 0 是"有积分记录"，仍算已签到。"""
        page = build_page(events='[{"start":"2026-09-15","display":"background"},'
                                 '{"start":"2026-09-15","title":0}]')
        records = extract_calendar_events(page)
        self.assertIn("2026-09-15", records)
        self.assertEqual(records["2026-09-15"].points, 0)

    def test_unescapes_js_single_quoted_string(self):
        page = build_page(events=EVENTS_WITH_TODAY.replace('"', '\\"'))
        self.assertEqual(len(extract_calendar_events(page)), 3)

    def test_inline_array_without_json_parse(self):
        page = '<script>var events = %s;</script>' % EVENTS_WITH_TODAY
        records = extract_calendar_events(page)
        self.assertEqual(len(records), 3)

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
        self.assertEqual(len(page.records), 2, "历史台账仍应被解析出来")

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


class TestQingwaSite(unittest.TestCase):
    """QingWa：单会话 Cookie（qw_session）+ 日历事件台账 + 蝌蚪积分。"""

    MESSAGE = (
        "这是您的第 <b>142</b> 次签到，已连续签到 <b>1</b> 天，本次签到获得 <b>10</b> 个蝌蚪。"
        "点击白色背景的圆点进行补签。你目前拥有补签卡 <b>0</b> 张。"
        '<span style="float:right">今日签到排名：<b>3558</b> / <b>3558</b></span>'
    )

    def _page(self, events=QINGWA_EVENTS, message=None):
        return build_page(
            events=events,
            message=message if message is not None else self.MESSAGE,
            bonus=10, cards=0, heading="签到成功",
            title="青蛙 :: 签到 - Powered by NexusPHP",
        )

    def test_profile(self):
        profile = get_profile("qingwapt")
        self.assertEqual(profile.name, "QingWa")
        self.assertEqual(profile.points_unit, "蝌蚪")
        self.assertEqual(profile.default_base_url, "https://www.qingwapt.com")
        self.assertEqual(profile.referer, "index.php")

    def test_guess_site_from_url(self):
        self.assertEqual(guess_site("https://www.qingwapt.com"), "qingwapt")
        self.assertEqual(guess_site("https://www.qingwapt.com/attendance.php"), "qingwapt")

    def test_only_titled_dates_are_signed(self):
        """日历标记多个日期，但只有带积分的那天算已签到。

        这与站点自报"已连续签到 1 天"一致 —— 是推翻
        "background 也算已签到" 的关键证据。
        """
        page = parse_attendance(self._page(), fallback_date=TODAY)
        self.assertEqual(set(page.records), {"2026-09-15"})
        self.assertTrue(page.signed_today)
        self.assertEqual(page.today_record.points, 10)

    def test_message_and_unit(self):
        page = parse_attendance(self._page(), fallback_date=TODAY)
        self.assertEqual(page.total_count, 142)
        self.assertEqual(page.streak, 1)
        self.assertEqual(page.points, 10)
        self.assertEqual(page.points_unit, "蝌蚪")
        self.assertEqual(page.retro_cards, 0)
        self.assertEqual(page.rank, 3558)
        self.assertEqual(page.rank_total, 3558)
        self.assertIn("第 142 次签到", page.message)

    def test_not_signed_today_when_only_background(self):
        """今天只在补签窗口内、尚未签到 → 必须判为未签到。"""
        events = ('[{"start":"2026-09-13","display":"background"},'
                  '{"start":"2026-09-13","title":10},'
                  '{"start":"2026-09-15","display":"background"}]')
        page = parse_attendance(self._page(events=events, message=""), fallback_date=TODAY)
        self.assertFalse(page.signed_today)
        self.assertEqual(page.site_date, TODAY)

    def test_login_page_detected(self):
        html = ('<html><head><title>登录</title></head><body>'
                '<form action="takelogin.php"><input name="username"/></form></body></html>')
        page = parse_attendance(html, fallback_date=TODAY)
        self.assertFalse(page.authenticated)
        self.assertIn("登录状态失效", page.error)


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
        self.assertEqual({p["key"] for p in profiles}, {"hhanclub", "hdfans", "qingwapt"})
        for p in profiles:
            self.assertTrue(p["default_base_url"].startswith("http"))


if __name__ == "__main__":
    unittest.main()

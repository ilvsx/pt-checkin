"""curl 解析测试：使用与真实抓包结构完全一致的样例（Cookie 已脱敏）。"""

from __future__ import annotations

import unittest

from ptcheckin.client import validate_cookie
from ptcheckin.curlparse import parse_curl, shell_split

SAMPLE_CURL = r"""curl 'https://hhanclub.net/attendance.php' \
  -H 'accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7' \
  -H 'accept-language: zh-CN,zh;q=0.9' \
  -b 'c_secure_uid=MTIzNDU%3D; c_secure_pass=0123456789abcdef0123456789abcdef; c_secure_ssl=eWVhaA%3D%3D; c_secure_tracker_ssl=eWVhaA%3D%3D; c_secure_login=bm9wZQ%3D%3D' \
  -H 'priority: u=0, i' \
  -H 'referer: https://hhanclub.net/mybonus.php' \
  -H 'sec-ch-ua: "Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Windows"' \
  -H 'sec-fetch-dest: document' \
  -H 'sec-fetch-mode: navigate' \
  -H 'sec-fetch-site: same-origin' \
  -H 'sec-fetch-user: ?1' \
  -H 'upgrade-insecure-requests: 1' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36' """


class TestShellSplit(unittest.TestCase):
    def test_single_quotes(self):
        self.assertEqual(shell_split("curl 'a b' c"), ["curl", "a b", "c"])

    def test_double_quotes_with_escapes(self):
        self.assertEqual(shell_split(r'curl -H "sec-ch-ua: \"Not\"A\""'), ["curl", "-H", 'sec-ch-ua: "Not"A"'])

    def test_line_continuation(self):
        self.assertEqual(shell_split("curl a \\\n  b"), ["curl", "a", "b"])

    def test_empty_quotes(self):
        self.assertEqual(shell_split("curl '' x"), ["curl", "", "x"])


class TestParseCurl(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_curl(SAMPLE_CURL)

    def test_url_and_base(self):
        self.assertEqual(self.parsed["url"], "https://hhanclub.net/attendance.php")
        self.assertEqual(self.parsed["base_url"], "https://hhanclub.net")

    def test_site_guess(self):
        self.assertEqual(self.parsed["site"], "hhanclub")

    def test_cookie(self):
        cookie = self.parsed["cookie"]
        self.assertIn("c_secure_uid=MTIzNDU%3D", cookie)
        self.assertIn("c_secure_pass=0123456789abcdef0123456789abcdef", cookie)
        self.assertIn("c_secure_login=bm9wZQ%3D%3D", cookie)
        ok, reason = validate_cookie(cookie)
        self.assertTrue(ok, reason)

    def test_user_agent(self):
        self.assertIn("Chrome/150", self.parsed["user_agent"])

    def test_referer(self):
        self.assertEqual(self.parsed["referer"], "https://hhanclub.net/mybonus.php")

    def test_extra_headers_captured(self):
        self.assertIn("sec-ch-ua-platform", self.parsed["headers"])

    def test_no_warnings(self):
        self.assertEqual(self.parsed["warnings"], [])

    def test_cookie_header_form(self):
        text = "curl 'https://x.net/a' -H 'Cookie: a=1; b=2'"
        parsed = parse_curl(text)
        self.assertEqual(parsed["cookie"], "a=1; b=2")

    def test_cookie_dedup_last_wins(self):
        text = "curl 'https://x.net/a' -b 'a=1; b=2' -b 'a=9'"
        parsed = parse_curl(text)
        self.assertIn("a=9", parsed["cookie"])
        self.assertEqual(parsed["cookie"].count("a="), 1)

    def test_missing_url_warns(self):
        parsed = parse_curl("curl -b 'a=1'")
        self.assertTrue(any("URL" in w for w in parsed["warnings"]))

    def test_missing_cookie_warns(self):
        parsed = parse_curl("curl 'https://x.net/a'")
        self.assertTrue(any("Cookie" in w for w in parsed["warnings"]))

    def test_non_curl_input(self):
        parsed = parse_curl("https://hhanclub.net/attendance.php -b 'c_secure_uid=1; c_secure_pass=2'")
        self.assertEqual(parsed["base_url"], "https://hhanclub.net")
        self.assertTrue(any("curl" in w for w in parsed["warnings"]))

    def test_empty_input(self):
        parsed = parse_curl("")
        self.assertEqual(parsed["warnings"], ["输入为空"])


class TestValidateCookie(unittest.TestCase):
    def test_valid(self):
        ok, _ = validate_cookie("c_secure_uid=abc; c_secure_pass=def; c_secure_login=nope")
        self.assertTrue(ok)

    def test_missing_pass(self):
        ok, reason = validate_cookie("c_secure_uid=abc")
        self.assertFalse(ok)
        self.assertIn("c_secure_pass", reason)

    def test_empty(self):
        ok, _ = validate_cookie("")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

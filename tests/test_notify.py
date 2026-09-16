"""通知渠道测试，重点覆盖飞书（加签、请求体形态、业务错误码）。"""

from __future__ import annotations

import json
import unittest
from typing import Any

import ptcheckin.notify as notify_mod
from ptcheckin.checkin import CheckResult
from ptcheckin.notify import (
    Notifier,
    build_feishu_app_payload,
    build_feishu_webhook_payload,
    feishu_card,
    feishu_sign,
)

# 由飞书文档算法独立复算得到的固定向量，用于防止实现漂移
VECTOR_TS = 1700000000
VECTOR_SECRET = "test-secret"
VECTOR_SIGN = "mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84="


class FakeSettings:
    def __init__(self, notify: dict[str, Any]):
        self._notify = notify

    def get(self, key: str, default: Any = None) -> Any:
        return self._notify if key == "notify" else default


class CapturingPost:
    """替换 notify._post_json，记录调用并按预设返回。"""

    def __init__(self, responses: list[str] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses or ['{"code":0,"msg":"success"}'])

    def __call__(self, url, payload, timeout=15, headers=None):
        self.calls.append({"url": url, "payload": payload, "headers": headers or {}})
        return self.responses.pop(0) if self.responses else '{"code":0,"msg":"success"}'


class FeishuSignTest(unittest.TestCase):
    def test_known_vector(self):
        ts, sign = feishu_sign(VECTOR_SECRET, timestamp=VECTOR_TS)
        self.assertEqual(ts, str(VECTOR_TS))
        self.assertEqual(sign, VECTOR_SIGN)

    def test_key_and_message_positions(self):
        """HMAC 的 key 是 "timestamp\\nsecret"，消息体为空 —— 写反会得到不同结果。"""
        import base64
        import hashlib
        import hmac

        _, sign = feishu_sign(VECTOR_SECRET, timestamp=VECTOR_TS)
        correct = base64.b64encode(
            hmac.new(f"{VECTOR_TS}\n{VECTOR_SECRET}".encode(), b"", hashlib.sha256).digest()
        ).decode()
        swapped = base64.b64encode(
            hmac.new(b"", f"{VECTOR_TS}\n{VECTOR_SECRET}".encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(sign, correct)
        self.assertNotEqual(sign, swapped)

    def test_different_timestamp_changes_signature(self):
        _, a = feishu_sign(VECTOR_SECRET, timestamp=1700000000)
        _, b = feishu_sign(VECTOR_SECRET, timestamp=1700000001)
        self.assertNotEqual(a, b)

    def test_default_timestamp_is_now(self):
        ts, _ = feishu_sign("s")
        self.assertTrue(ts.isdigit())
        self.assertAlmostEqual(int(ts), __import__("time").time(), delta=5)


class FeishuPayloadTest(unittest.TestCase):
    def test_webhook_text_shape(self):
        payload = build_feishu_webhook_payload("标题", "**账号**：主号")
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"], {"text": "标题\n账号：主号"})
        self.assertNotIn("card", payload)

    def test_webhook_card_shape(self):
        payload = build_feishu_webhook_payload("签到成功", "**账号**：主号", use_card=True)
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertIn("card", payload)
        self.assertEqual(payload["card"]["header"]["title"]["content"], "签到成功")
        # 卡片是 lark_md，保留 Markdown
        self.assertIn("**账号**", payload["card"]["elements"][0]["text"]["content"])

    def test_app_payload_content_is_json_string(self):
        payload = build_feishu_app_payload("oc_xxx", "标题", "**账号**：主号")
        self.assertEqual(payload["receive_id"], "oc_xxx")
        self.assertIsInstance(payload["content"], str, "im/v1 的 content 必须是 JSON 字符串")
        self.assertEqual(json.loads(payload["content"]), {"text": "标题\n账号：主号"})

    def test_app_payload_card(self):
        payload = build_feishu_app_payload("oc_x", "签到成功", "正文", use_card=True)
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(json.loads(payload["content"])["header"]["title"]["content"], "签到成功")

    def test_card_colors(self):
        self.assertEqual(feishu_card("签到成功", "")["header"]["template"], "green")
        self.assertEqual(feishu_card("签到失败", "")["header"]["template"], "red")
        self.assertEqual(feishu_card("Cookie 失效", "")["header"]["template"], "red")
        self.assertEqual(feishu_card("今日已签到", "")["header"]["template"], "grey")
        self.assertEqual(feishu_card("测试通知", "")["header"]["template"], "blue")


class FeishuErrorCodeTest(unittest.TestCase):
    """飞书业务失败也返回 HTTP 200，必须靠响应体里的 code 判断。"""

    def test_code_zero_is_ok(self):
        notify_mod._raise_on_feishu_error('{"code":0,"msg":"success"}')

    def test_nonzero_code_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            notify_mod._raise_on_feishu_error(
                '{"code":19021,"msg":"sign match fail or timestamp is not within one hour"}'
            )
        self.assertIn("19021", str(ctx.exception))
        self.assertIn("sign match fail", str(ctx.exception))

    def test_empty_and_non_json_are_ignored(self):
        notify_mod._raise_on_feishu_error("")
        notify_mod._raise_on_feishu_error("<html>ok</html>")

    def test_non_dict_ignored(self):
        notify_mod._raise_on_feishu_error("[1,2,3]")


class FeishuDispatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = notify_mod._post_json

    def tearDown(self):
        notify_mod._post_json = self._orig

    def _notifier(self, channels):
        return Notifier(FakeSettings({"enabled": True, "channels": channels}))

    def test_webhook_without_secret(self):
        post = CapturingPost()
        notify_mod._post_json = post
        notifier = self._notifier([{"type": "feishu", "url": "https://open.feishu.cn/x", "enabled": True}])
        results = notifier.send("[PT签到] 测试", "正文", event="test")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, results[0].detail)
        payload = post.calls[0]["payload"]
        self.assertEqual(payload["msg_type"], "text")
        self.assertNotIn("sign", payload)

    def test_webhook_with_secret_adds_signature(self):
        post = CapturingPost()
        notify_mod._post_json = post
        notifier = self._notifier(
            [{"type": "feishu", "url": "https://open.feishu.cn/x", "secret": VECTOR_SECRET, "enabled": True}]
        )
        notifier.send("[PT签到] 测试", "正文", event="test")
        payload = post.calls[0]["payload"]
        self.assertIn("timestamp", payload)
        self.assertIn("sign", payload)
        # 用相同 timestamp 复算，签名应一致
        expected = feishu_sign(VECTOR_SECRET, timestamp=payload["timestamp"])[1]
        self.assertEqual(payload["sign"], expected)

    def test_business_error_is_reported_as_failure(self):
        post = CapturingPost(['{"code":19021,"msg":"sign match fail"}'])
        notify_mod._post_json = post
        notifier = self._notifier([{"type": "feishu", "url": "https://x", "enabled": True}])
        results = notifier.send("[PT签到] 测试", "正文", event="test")
        self.assertFalse(results[0].ok, "HTTP 200 但 code!=0 必须判为失败")
        self.assertIn("19021", results[0].detail)

    def test_card_mode_used_when_enabled(self):
        post = CapturingPost()
        notify_mod._post_json = post
        notifier = self._notifier(
            [{"type": "feishu", "url": "https://x", "card": True, "enabled": True}]
        )
        notifier.send("[PT签到] 测试", "**正文**", event="test")
        self.assertEqual(post.calls[0]["payload"]["msg_type"], "interactive")

    def test_missing_url_reports_failure(self):
        notify_mod._post_json = CapturingPost()
        notifier = self._notifier([{"type": "feishu", "enabled": True}])
        results = notifier.send("[PT签到] 测试", "正文", event="test")
        self.assertFalse(results[0].ok)
        self.assertIn("url", results[0].detail)

    def test_app_bot_flow(self):
        post = CapturingPost(
            [
                '{"code":0,"msg":"ok","tenant_access_token":"t-abc","expire":7200}',
                '{"code":0,"msg":"success"}',
            ]
        )
        notify_mod._post_json = post
        notifier = self._notifier(
            [
                {
                    "type": "feishu_app",
                    "app_id": "cli_x",
                    "app_secret": "sec",
                    "receive_id": "oc_group",
                    "receive_id_type": "chat_id",
                    "enabled": True,
                }
            ]
        )
        results = notifier.send("[PT签到] 测试", "正文", event="test")
        self.assertTrue(results[0].ok, results[0].detail)
        self.assertEqual(len(post.calls), 2)

        token_call, send_call = post.calls
        self.assertIn("tenant_access_token", token_call["url"])
        self.assertEqual(token_call["payload"], {"app_id": "cli_x", "app_secret": "sec"})

        self.assertIn("im/v1/messages", send_call["url"])
        self.assertIn("receive_id_type=chat_id", send_call["url"])
        self.assertEqual(send_call["headers"]["Authorization"], "Bearer t-abc")
        self.assertEqual(send_call["payload"]["receive_id"], "oc_group")
        self.assertIsInstance(send_call["payload"]["content"], str)

    def test_app_bot_token_error(self):
        post = CapturingPost(['{"code":10003,"msg":"invalid app_secret"}'])
        notify_mod._post_json = post
        notifier = self._notifier(
            [{"type": "feishu_app", "app_id": "a", "app_secret": "b", "receive_id": "c", "enabled": True}]
        )
        results = notifier.send("[PT签到] 测试", "正文", event="test")
        self.assertFalse(results[0].ok)
        self.assertIn("10003", results[0].detail)

    def test_lark_alias(self):
        post = CapturingPost()
        notify_mod._post_json = post
        notifier = self._notifier([{"type": "lark", "url": "https://x", "enabled": True}])
        self.assertTrue(notifier.send("t", "b", event="test")[0].ok)


class NotifyGatingTest(unittest.TestCase):
    def test_disabled_channel_skipped(self):
        notify_mod._post_json = CapturingPost()
        notifier = Notifier(
            FakeSettings({"enabled": True, "channels": [{"type": "feishu", "url": "https://x", "enabled": False}]})
        )
        self.assertEqual(notifier.send("t", "b", event="test"), [])

    def test_disabled_globally_skips_real_events_but_not_test(self):
        notifier = Notifier(
            FakeSettings({"enabled": False, "channels": [{"type": "feishu", "url": "https://x"}]})
        )
        self.assertFalse(notifier.should_send("success"))
        self.assertTrue(notifier.should_send("test"))

    def test_on_already_defaults_off(self):
        notifier = Notifier(FakeSettings({"enabled": True, "channels": []}))
        self.assertFalse(notifier.should_send("already"))
        self.assertTrue(notifier.should_send("success"))
        self.assertTrue(notifier.should_send("failure"))


class CheckinMessageTest(unittest.TestCase):
    def _result(self, **kw):
        result = CheckResult(status="success", trigger="auto", points=60, streak=6,
                             total_count=192, rank=3558, rank_total=3558)
        for key, value in kw.items():
            setattr(result, key, value)
        return result

    def test_format_contains_key_fields(self):
        title, body = Notifier.format_checkin(
            self._result(), {"id": 1, "name": "QingWa 主号", "base_url": "https://www.qingwapt.com"}
        )
        self.assertIn("QingWa 主号", title)
        self.assertIn("签到成功", title)
        self.assertIn("QingWa 主号", body)
        self.assertIn("60", body)
        self.assertIn("6 天", body)

    def test_failure_title_is_red_card(self):
        result = self._result(status="auth_failed", error="登录状态失效")
        title, _ = Notifier.format_checkin(result, {"name": "x", "base_url": ""})
        self.assertEqual(feishu_card(title, "")["header"]["template"], "red")


if __name__ == "__main__":
    unittest.main()

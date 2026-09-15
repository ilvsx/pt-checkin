"""命令行集成测试：覆盖站点自动识别与参数顺序等易错点。"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ptcheckin.cli import main
from ptcheckin.secretsbox import SecretsBox
from ptcheckin.settings import Settings
from ptcheckin.store import Store

COOKIE = "c_secure_uid=MTIzNDU%3D; c_secure_pass=0123456789abcdef0123456789abcdef"


def run_cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:  # argparse 出错时
            code = int(exc.code or 0)
    return code, out.getvalue() + err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ptcheckin-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self) -> Store:
        settings = Settings(data_dir=self.tmp)
        return Store(settings.db_path, SecretsBox(settings.key_path))


class TestAdd(CliTestCase):
    def test_infers_hdfans_from_url(self):
        code, out = run_cli("add", "--data", str(self.tmp), "--name", "hdfans-test",
                            "--url", "https://hdfans.org", "--cookie", COOKIE, "--no-verify")
        self.assertEqual(code, 0, out)
        store = self._store()
        acct = store.get_account_by_name("hdfans-test")
        self.assertEqual(acct["site"], "hdfans", "站点必须按地址识别，而不是写入 URL")
        store.close()

    def test_infers_hhanclub_from_url(self):
        code, out = run_cli("add", "--data", str(self.tmp), "--name", "hh-test",
                            "--url", "https://hhanclub.net", "--cookie", COOKIE, "--no-verify")
        self.assertEqual(code, 0, out)
        store = self._store()
        self.assertEqual(store.get_account_by_name("hh-test")["site"], "hhanclub")
        store.close()

    def test_unknown_host_keeps_host_as_site(self):
        code, out = run_cli("add", "--data", str(self.tmp), "--name", "x",
                            "--url", "https://example.com", "--cookie", COOKIE, "--no-verify")
        self.assertEqual(code, 0, out)
        store = self._store()
        self.assertEqual(store.get_account_by_name("x")["site"], "example.com")
        store.close()

    def test_rejects_cookie_without_pass(self):
        code, out = run_cli("add", "--data", str(self.tmp), "--name", "bad",
                            "--url", "https://hdfans.org", "--cookie", "c_secure_uid=1", "--no-verify")
        self.assertNotEqual(code, 0)
        self.assertIn("c_secure_pass", out)

    def test_rejects_duplicate_name(self):
        args = ("add", "--data", str(self.tmp), "--name", "dup",
                "--url", "https://hdfans.org", "--cookie", COOKIE, "--no-verify")
        self.assertEqual(run_cli(*args)[0], 0)
        code, out = run_cli(*args)
        self.assertNotEqual(code, 0)
        self.assertIn("已存在", out)

    def test_rejects_bad_schedule(self):
        code, out = run_cli("add", "--data", str(self.tmp), "--name", "s",
                            "--url", "https://hdfans.org", "--cookie", COOKIE,
                            "--schedule", "99:99", "--no-verify")
        self.assertNotEqual(code, 0)
        self.assertIn("HH:MM", out)

    def test_add_from_curl_stdin(self):
        curl = f"curl 'https://hdfans.org/attendance.php' -b '{COOKIE}'"
        import sys

        old = sys.stdin
        sys.stdin = io.StringIO(curl)
        try:
            code, out = run_cli("add", "--data", str(self.tmp), "--curl", "-", "--no-verify")
        finally:
            sys.stdin = old
        self.assertEqual(code, 0, out)
        store = self._store()
        accts = store.list_accounts()
        self.assertEqual(len(accts), 1)
        self.assertEqual(accts[0]["site"], "hdfans")
        store.close()


class TestGlobalFlags(CliTestCase):
    """--data 既能放在子命令前，也能放在子命令后。"""

    def test_flag_after_subcommand(self):
        code, out = run_cli("doctor", "--data", str(self.tmp))
        self.assertEqual(code, 0, out)
        self.assertIn(str(self.tmp), out)

    def test_flag_before_subcommand(self):
        code, out = run_cli("--data", str(self.tmp), "doctor")
        self.assertEqual(code, 0, out)
        self.assertIn(str(self.tmp), out)

    def test_debug_flag_both_positions(self):
        self.assertEqual(run_cli("--debug", "--data", str(self.tmp), "doctor")[0], 0)
        self.assertEqual(run_cli("--data", str(self.tmp), "doctor", "--debug")[0], 0)


class TestOtherCommands(CliTestCase):
    def test_curl_subcommand_parses(self):
        code, out = run_cli("curl", f"curl 'https://hdfans.org/attendance.php' -b '{COOKIE}'")
        self.assertEqual(code, 0)
        data = json.loads(out[out.index("{") :])
        self.assertEqual(data["site"], "hdfans")
        self.assertTrue(data["cookie_valid"])

    def test_status_on_empty_dir(self):
        code, out = run_cli("status", "--data", str(self.tmp))
        self.assertEqual(code, 1, "没有账号时 status 应返回非 0")
        self.assertIn("尚未添加", out)

    def test_accounts_lists_added(self):
        run_cli("add", "--data", str(self.tmp), "--name", "listed",
                "--url", "https://hdfans.org", "--cookie", COOKIE, "--no-verify")
        code, out = run_cli("accounts", "--data", str(self.tmp))
        self.assertEqual(code, 0)
        self.assertIn("listed", out)

    def test_remove(self):
        run_cli("add", "--data", str(self.tmp), "--name", "todelete",
                "--url", "https://hdfans.org", "--cookie", COOKIE, "--no-verify")
        code, out = run_cli("remove", "--data", str(self.tmp), "--account", "todelete")
        self.assertEqual(code, 0, out)
        store = self._store()
        self.assertEqual(store.list_accounts(), [])
        store.close()


if __name__ == "__main__":
    unittest.main()

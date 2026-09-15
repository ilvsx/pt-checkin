"""命令行入口：serve / once / status / accounts / curl / notify-test / doctor。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .checkin import CheckinService
from .client import validate_cookie
from .curlparse import parse_curl
from .logutil import get_logger, setup_logging
from .notify import Notifier
from .scheduler import Scheduler, parse_hhmm
from .secretsbox import SecretsBox
from .settings import Settings
from .sites import guess_site
from .store import Store, utcnow


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = Path(args.data).expanduser().resolve() if getattr(args, "data", None) else None
    settings = Settings(data_dir=data_dir)
    settings.ensure_dirs()
    setup_logging(settings.log_dir, debug=getattr(args, "debug", False))
    secrets = SecretsBox(settings.key_path)
    store = Store(settings.db_path, secrets)
    service = CheckinService(store, settings, Notifier(settings))
    scheduler = Scheduler(store, settings, service)
    return {"settings": settings, "store": store, "service": service, "scheduler": scheduler}


def _fmt_status(label: str) -> str:
    return {
        "success": "✅ 签到成功",
        "already": "☑️  今日已签到",
        "pending": "🕒 待签到",
        "retrying": "🔁 等待重试",
        "exhausted": "⛔ 重试已用尽",
        "failed": "❌ 签到失败",
        "auth_failed": "🔑 Cookie 失效",
        "network_error": "🌐 网络错误",
        "disabled": "⏸  已停用",
        "invalid_schedule": "⚠️  时间配置错误",
    }.get(label, label)


# ----------------------------------------------------------------- commands
def cmd_serve(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    settings, store, service, scheduler = ctx["settings"], ctx["store"], ctx["service"], ctx["scheduler"]
    log = get_logger("cli")

    if not args.no_scheduler and settings.get("scheduler_enabled", True):
        scheduler.start()

    from .web.server import create_server, serve_forever

    host = args.host or (settings.get("web") or {}).get("host")
    port = args.port or (settings.get("web") or {}).get("port")
    httpd = create_server(settings, store, service, scheduler, host=host, port=port)
    token = (settings.get("web") or {}).get("auth_token")
    url = f"http://{host}:{port}/"
    if token:
        url += f"?token={token}"
    log.info("=" * 62)
    log.info("  PT 自动签到已就绪  →  %s", url)
    log.info("  数据目录: %s", settings.data_dir)
    log.info("  账号数量: %d", len(store.list_accounts()))
    log.info("  调度器: %s", "运行中" if scheduler.running else "未启动")
    log.info("=" * 62)
    try:
        serve_forever(httpd)
    finally:
        scheduler.stop()
        store.close()
    return 0


def _select_accounts(store: Store, selector: str | None, only_enabled: bool = True) -> list[dict[str, Any]]:
    accounts = store.list_accounts(decrypt=True, only_enabled=only_enabled)
    if not selector:
        return accounts
    selected = []
    for acct in accounts:
        if selector.isdigit() and int(acct["id"]) == int(selector):
            selected.append(acct)
        elif acct["name"] == selector:
            selected.append(acct)
    if not selected:
        raise SystemExit(f"未找到账号: {selector}")
    return selected


def cmd_once(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    store, service = ctx["store"], ctx["service"]
    accounts = _select_accounts(store, args.account, only_enabled=not args.all)
    if not accounts:
        print("没有可执行的账号")
        return 1
    exit_code = 0
    for acct in accounts:
        result = service.run(acct, trigger="cli", notify=not args.no_notify)
        flag = "OK " if result.ok else "FAIL"
        print(f"[{flag}] {result.account_name}: {_fmt_status(result.status)}"
              f"{' | 获得 ' + str(result.points) if result.points is not None else ''}"
              f"{' | 连续 ' + str(result.streak) + ' 天' if result.streak else ''}"
              f"{' | ' + result.error if result.error else ''}")
        if args.verbose:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        if not result.ok:
            exit_code = 2
    store.close()
    return exit_code


def cmd_status(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    store, service, scheduler = ctx["store"], ctx["service"], ctx["scheduler"]
    accounts = store.list_accounts(decrypt=True)
    if not accounts:
        print("尚未添加任何账号。使用 `run.py add --curl '...'` 添加。")
        return 1

    now = utcnow()
    print(f"{'ID':<4} {'账号':<18} {'今日':<14} {'获得':<6} {'连续':<5} {'下次运行':<20} 备注")
    print("-" * 96)
    for acct in accounts:
        plan = scheduler.plan(acct, now)
        today = plan["date"]
        day = store.get_day(int(acct["id"]), today) or {}
        if not acct["enabled"]:
            label = "disabled"
        elif day.get("signed"):
            label = "already"
        elif plan.get("state") == "exhausted":
            label = "exhausted"
        elif plan.get("failures"):
            label = "retrying"
        else:
            label = "pending"
        nxt = plan.get("next_run_at") or ""
        # 连续天数优先用站点自报值，缺失时按本地台账推算
        streak = day.get("streak")
        if streak is None:
            streak = store.compute_streak(int(acct["id"]), today)
        print(f"{acct['id']:<4} {acct['name'][:17]:<18} {_fmt_status(label):<14} "
              f"{str(day.get('points') or '-'):<6} {str(streak) + '天':<5} "
              f"{nxt[:19]:<20} {acct.get('note') or ''}")
    store.close()
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    store = ctx["store"]
    accounts = store.list_accounts()
    if not accounts:
        print("尚未添加任何账号。")
        return 0
    for acct in accounts:
        flag = "启用" if acct["enabled"] else "停用"
        print(f"#{acct['id']} {acct['name']} [{flag}] {acct['base_url']} "
              f"签到时间={acct.get('schedule_time') or '默认'} "
              f"下次={acct.get('timezone') or '默认时区'}")
    store.close()
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    store, service = ctx["store"], ctx["service"]

    base_url = args.url or ""
    cookie = args.cookie or ""
    user_agent = args.user_agent or ""
    name = args.name or ""
    if args.curl:
        parsed = parse_curl(args.curl if args.curl != "-" else sys.stdin.read())
        base_url = base_url or parsed.get("base_url") or ""
        cookie = cookie or parsed.get("cookie") or ""
        user_agent = user_agent or parsed.get("user_agent") or ""
        name = name or (parsed.get("site") or "").upper()

    if not base_url or not cookie:
        print("错误：需要 --url 与 --cookie，或使用 --curl 提供完整 curl 文本", file=sys.stderr)
        return 1
    ok, reason = validate_cookie(cookie)
    if not ok:
        print(f"错误：Cookie 校验失败 - {reason}", file=sys.stderr)
        return 1
    if args.schedule and parse_hhmm(args.schedule) is None:
        print("错误：--schedule 格式应为 HH:MM", file=sys.stderr)
        return 1
    if not name:
        from .client import parse_cookie_string

        name = f"{base_url.split('//')[-1].split('/')[0].upper()}-{parse_cookie_string(cookie).get('c_secure_uid', '')[:8]}"

    if store.get_account_by_name(name):
        print(f"错误：账号名称已存在 {name}", file=sys.stderr)
        return 1

    account = store.create_account({
        "name": name,
        "site": guess_site(base_url),
        "base_url": base_url,
        "cookie": cookie,
        "user_agent": user_agent,
        "schedule_time": args.schedule,
        "jitter_seconds": args.jitter,
        "timezone": args.timezone,
        "note": args.note,
        "enabled": not args.disabled,
    })
    print(f"已添加账号 #{account['id']} {account['name']}")
    if not args.no_verify:
        print("正在校验 Cookie 并同步站点日历…")
        result = service.run(store.get_account(int(account["id"]), decrypt=True) or account,
                             trigger="cli", notify=False)
        print(f"校验结果：{_fmt_status(result.status)}"
              f"{' | ' + result.error if result.error else ''}"
              f"{' | 同步 ' + str(result.records_synced) + ' 天记录' if result.records_synced else ''}")
        if not result.ok:
            return 2
    store.close()
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    store = ctx["store"]
    accounts = _select_accounts(store, args.account, only_enabled=False)
    for acct in accounts:
        store.delete_account(int(acct["id"]))
        print(f"已删除 #{acct['id']} {acct['name']}")
    store.close()
    return 0


def cmd_curl(args: argparse.Namespace) -> int:
    text = args.curl if args.curl != "-" else sys.stdin.read()
    parsed = parse_curl(text)
    cookie = parsed.pop("cookie", "")
    parsed["cookie_preview"] = (cookie[:40] + "…") if cookie else ""
    parsed["cookie_valid"], parsed["cookie_check"] = validate_cookie(cookie)
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    notifier = Notifier(ctx["settings"])
    results = notifier.send("[PT签到] 测试通知", "命令行测试通知，收到即表示渠道可用。", event="test")
    if not results:
        print("未配置任何通知渠道（或均被禁用）。")
        return 1
    for item in results:
        print(f"{'✅' if item.ok else '❌'} {item.name}: {item.detail}")
    return 0 if all(r.ok for r in results) else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    settings, store = ctx["settings"], ctx["store"]
    print(f"pt-checkin {__version__}")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"数据目录    : {settings.data_dir}")
    print(f"数据库      : {settings.db_path}")
    print(f"加密后端    : {store.secrets.backend}")
    print(f"时区        : {settings.get('timezone')}")
    print(f"调度器开关  : {settings.get('scheduler_enabled')}")
    print(f"默认签到时间: {settings.get('default_schedule_time')} (随机延迟 {settings.get('default_jitter_seconds')}s)")
    print(f"账号数量    : {len(store.list_accounts())}")
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
        print("时区数据    : OK")
    except Exception as exc:  # noqa: BLE001
        print(f"时区数据    : 失败 - {exc}")
    store.close()
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pt-checkin",
        description="PT 站点（HHClub）每日自动签到系统：定时签到、记录查询、精确判定当日状态。",
    )
    parser.add_argument("--version", action="version", version=f"pt-checkin {__version__}")
    parser.add_argument("--data", help="数据目录（默认 ./data，可用 PTCHECKIN_DATA 覆盖）")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    # 让 --data / --debug 既能写在子命令前，也能写在子命令后。
    # default=SUPPRESS 保证子解析器不会用默认值覆盖主解析器已解析出的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    p = sub.add_parser("serve", parents=[common], help="启动 Web 界面与定时调度（默认命令）")
    p.add_argument("--host", help="监听地址")
    p.add_argument("--port", type=int, help="监听端口")
    p.add_argument("--no-scheduler", action="store_true", help="只启动 Web，不启用定时调度")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("once", parents=[common], help="立即执行一次签到")
    p.add_argument("--account", help="账号名称或 ID，缺省表示全部")
    p.add_argument("--all", action="store_true", help="包含已停用的账号")
    p.add_argument("--no-notify", action="store_true", help="不发送通知")
    p.add_argument("--verbose", action="store_true", help="输出完整结果 JSON")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("status", parents=[common], help="查看各账号今日签到状态与下次运行时间")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("accounts", parents=[common], help="列出所有账号")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("add", parents=[common], help="添加账号（可用 --curl 直接粘贴浏览器复制内容）")
    p.add_argument("--name", help="账号名称")
    p.add_argument("--url", help="站点地址，例如 https://hhanclub.net")
    p.add_argument("--cookie", help="Cookie 字符串")
    p.add_argument("--curl", help="curl 命令文本，或 - 从 stdin 读取")
    p.add_argument("--schedule", help="每日签到时间 HH:MM")
    p.add_argument("--jitter", type=int, help="随机延迟上限（秒）")
    p.add_argument("--timezone", help="账号时区")
    p.add_argument("--note", help="备注")
    p.add_argument("--user-agent", dest="user_agent", help="自定义 User-Agent")
    p.add_argument("--disabled", action="store_true", help="添加后不启用自动签到")
    p.add_argument("--no-verify", action="store_true", help="添加后不立即校验")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", parents=[common], help="删除账号")
    p.add_argument("--account", required=True, help="账号名称或 ID")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("curl", parents=[common], help="解析 curl 文本并打印识别结果")
    p.add_argument("curl", nargs="?", default="-", help="curl 文本，或 - 从 stdin 读取")
    p.set_defaults(func=cmd_curl)

    p = sub.add_parser("notify-test", parents=[common], help="发送一条测试通知")
    p.set_defaults(func=cmd_notify_test)

    p = sub.add_parser("doctor", parents=[common], help="环境自检")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 默认子命令为 serve
        args = parser.parse_args(["serve", *argv])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        get_logger("cli").exception("执行失败: %s", exc)
        return 1

"""配置管理：数据目录、默认配置、读写与环境变量覆盖。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .logutil import get_logger

log = get_logger("settings")

# 项目根目录（pt-checkin/）
ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SCHEDULE_TIME = "00:30"

DEFAULTS: dict[str, Any] = {
    # 全局默认时区：用于判断"今天"以及计算签到时间点
    "timezone": "Asia/Shanghai",
    # 默认签到时间（HH:MM，站点所在时区）
    "default_schedule_time": DEFAULT_SCHEDULE_TIME,
    # 默认随机延迟上限（秒）。同一账号同一天的延迟是稳定的，重启不会改变。
    "default_jitter_seconds": 600,
    # 当日失败后的最大重试次数（不含首次）
    "max_retries_per_day": 5,
    # 两次尝试之间的最小间隔（秒）
    "retry_interval_seconds": 900,
    # 单次 HTTP 请求超时（秒）
    "request_timeout": 30,
    # 是否校验 TLS 证书（自建反代证书异常时可关闭）
    "verify_ssl": True,
    # 代理，例如 http://127.0.0.1:7890 ；留空表示直连
    "proxy": "",
    # 调度器
    "scheduler_enabled": True,
    "scheduler_tick_seconds": 20,
    # 站点日历返回的历史记录保留策略（原始响应片段保留天数）
    "keep_raw_days": 30,
    # Web 服务
    "web": {
        "host": "127.0.0.1",
        "port": 8787,
        # 非空时所有 API 与页面都要求携带该令牌（?token= 或 X-Auth-Token 头）
        "auth_token": "",
    },
    # 通知
    "notify": {
        "enabled": False,
        "on_success": True,
        "on_failure": True,
        "on_already": False,
        "channels": [],
    },
}


def resolve_data_dir() -> Path:
    """数据目录：环境变量 PTCHECKIN_DATA 优先，否则 <项目>/data。"""
    env = os.environ.get("PTCHECKIN_DATA", "").strip()
    return Path(env).expanduser().resolve() if env else (ROOT_DIR / "data")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    """线程安全的配置对象，落盘为 ``data/config.json``。"""

    def __init__(self, data_dir: Path | None = None, config_path: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else resolve_data_dir()
        self.config_path = Path(config_path) if config_path else self.data_dir / "config.json"
        self.db_path = self.data_dir / "ptcheckin.db"
        self.key_path = self.data_dir / "secret.key"
        self.log_dir = self.data_dir / "logs"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> dict[str, Any]:
        with self._lock:
            raw: dict[str, Any] = {}
            if self.config_path.exists():
                try:
                    raw = json.loads(self.config_path.read_text(encoding="utf-8")) or {}
                except (OSError, json.JSONDecodeError) as exc:
                    log.error("配置文件解析失败，使用默认值: %s", exc)
                    raw = {}
            self._data = _deep_merge(DEFAULTS, raw)
            self._apply_env()
            return self._data

    def _apply_env(self) -> None:
        web = self._data.setdefault("web", {})
        if os.environ.get("PTCHECKIN_HOST"):
            web["host"] = os.environ["PTCHECKIN_HOST"]
        if os.environ.get("PTCHECKIN_PORT"):
            try:
                web["port"] = int(os.environ["PTCHECKIN_PORT"])
            except ValueError:
                log.warning("PTCHECKIN_PORT 不是合法端口: %r", os.environ["PTCHECKIN_PORT"])
        if os.environ.get("PTCHECKIN_TOKEN") is not None:
            web["auth_token"] = os.environ["PTCHECKIN_TOKEN"]
        if os.environ.get("PTCHECKIN_PROXY") is not None:
            self._data["proxy"] = os.environ["PTCHECKIN_PROXY"]
        if os.environ.get("PTCHECKIN_TZ"):
            self._data["timezone"] = os.environ["PTCHECKIN_TZ"]

    def save(self) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self.config_path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # -------------------------------------------------------------- access
    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """按顶层键合并更新（web/notify 为嵌套字典时做深合并）。"""
        with self._lock:
            self._data = _deep_merge(self._data, patch or {})
            self._apply_env()
            return copy.deepcopy(self._data)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # 数据目录包含 Cookie 密文与密钥，收紧权限
        for path in (self.data_dir, self.log_dir):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

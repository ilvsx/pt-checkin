"""Cookie 加密存储。

优先使用 ``cryptography`` 的 Fernet（AES-128-CBC + HMAC）做真正的加密；
当该库不可用时退化为"仅混淆"方案，并显式标记，避免给人虚假的安全感。

密钥保存在数据目录的 ``secret.key``（权限 0600）。请务必备份该文件，
丢失后已保存的 Cookie 将无法解密。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from .logutil import get_logger

log = get_logger("secrets")

try:  # pragma: no cover - 取决于运行环境
    from cryptography.fernet import Fernet, InvalidToken

    _HAVE_FERNET = True
except Exception:  # noqa: BLE001 - 任何导入错误都退化
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]
    _HAVE_FERNET = False

_OBFUSCATED_PREFIX = "obf:"
_FERNET_PREFIX = "enc:"


class SecretsBox:
    """负责 Cookie 的加解密与密钥管理。"""

    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        self._fernet = None
        self._raw_key: bytes | None = None
        self._load_or_create_key()

    # ------------------------------------------------------------------ key
    @property
    def backend(self) -> str:
        return "fernet" if _HAVE_FERNET else "obfuscated"

    def _load_or_create_key(self) -> None:
        key = b""
        if self.key_path.exists():
            try:
                key = self.key_path.read_bytes().strip()
            except OSError as exc:
                log.error("读取密钥失败 %s: %s", self.key_path, exc)
        if not key:
            key = base64.urlsafe_b64encode(secrets.token_bytes(32))
            try:
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self.key_path.write_bytes(key)
                os.chmod(self.key_path, 0o600)
                log.info("已生成新的加密密钥: %s", self.key_path)
            except OSError as exc:
                log.error("写入密钥失败 %s: %s", self.key_path, exc)

        self._raw_key = hashlib.sha256(key).digest()
        if _HAVE_FERNET:
            try:
                self._fernet = Fernet(key)
            except Exception:  # 密钥格式不对时重新派生一个合法 key
                self._fernet = Fernet(base64.urlsafe_b64encode(self._raw_key))

    # --------------------------------------------------------------- crypto
    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            return ""
        data = plaintext.encode("utf-8")
        if self._fernet is not None:
            return _FERNET_PREFIX + self._fernet.encrypt(data).decode("ascii")
        return _OBFUSCATED_PREFIX + self._xor_obfuscate(data)

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        if token.startswith(_FERNET_PREFIX):
            body = token[len(_FERNET_PREFIX) :]
            if self._fernet is not None:
                try:
                    return self._fernet.decrypt(body.encode("ascii")).decode("utf-8")
                except InvalidToken:
                    log.error("Cookie 解密失败：密钥不匹配或数据损坏")
                    return ""
                except Exception as exc:  # noqa: BLE001
                    log.error("Cookie 解密异常: %s", exc)
                    return ""
            log.error("数据由 Fernet 加密，但当前环境缺少 cryptography 库")
            return ""
        if token.startswith(_OBFUSCATED_PREFIX):
            try:
                return self._deobfuscate(token[len(_OBFUSCATED_PREFIX) :])
            except Exception as exc:  # noqa: BLE001
                log.error("Cookie 反混淆失败: %s", exc)
                return ""
        # 兼容早期/手工写入的明文
        return token

    # ------------------------------------------------- xor fallback helpers
    def _keystream(self, length: int) -> bytes:
        assert self._raw_key is not None
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(self._raw_key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    def _xor_obfuscate(self, data: bytes) -> str:
        stream = self._keystream(len(data))
        body = bytes(a ^ b for a, b in zip(data, stream))
        mac = hmac.new(self._raw_key or b"", body, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(mac + body).decode("ascii")

    def _deobfuscate(self, token: str) -> str:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
        mac, body = blob[:16], blob[16:]
        expect = hmac.new(self._raw_key or b"", body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expect):
            raise ValueError("完整性校验失败")
        stream = self._keystream(len(body))
        return bytes(a ^ b for a, b in zip(body, stream)).decode("utf-8")

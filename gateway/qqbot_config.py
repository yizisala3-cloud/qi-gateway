"""QQ is opt-in; validating disabled configuration has no storage/network effects."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _flag(env, name, default=False):
    value = str(env.get(name, str(default))).strip().lower()
    if value not in {"1", "0", "true", "false"}:
        raise ValueError(f"{name} 必须是 true/false")
    return value in {"1", "true"}


def _number(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 超出允许范围")
    return value


@dataclass(frozen=True)
class QQConfig:
    enabled: bool = False
    token: str = ""
    account_id: str = ""
    owner_id: str = ""
    assistant_id: str = ""
    store_path: str = "data/qqbot.sqlite3"
    sticker_manifest: str = ""
    system_prompt_file: str = ""
    debounce_seconds: int = 3
    max_batch_wait: int = 10
    action_timeout: int = 15
    heartbeat_timeout: int = 90
    history_limit: int = 30
    export_timeout: int = 15
    proactive_enabled: bool = False
    timezone: str = "Asia/Shanghai"
    active_start: str = "09:00"
    active_end: str = "22:00"
    check_seconds: int = 300
    quiet_seconds: int = 1800
    cooldown_seconds: int = 3600
    daily_limit: int = 3
    proactive_ttl: int = 1800

    @property
    def conversation_id(self):
        return f"qq:{self.account_id}:private:{self.owner_id}"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        if not _flag(env, "QQBOT_ENABLED"):
            return cls()
        token = env.get("QQBOT_TOKEN", "").strip()
        if not re.fullmatch(r"[\x21-\x7e]{24,}",token):
            raise ValueError("QQBOT_TOKEN 至少24字符且不能有空白")
        account, owner = (env.get(k, "").strip() for k in ("QQBOT_ACCOUNT_ID", "QQBOT_OWNER_ID"))
        if not all(re.fullmatch(r"[1-9][0-9]{4,19}", value) for value in (account, owner)) or account == owner:
            raise ValueError("QQBOT_ACCOUNT_ID / QQBOT_OWNER_ID 必须是不同的有效QQ号")
        assistant = env.get("QQBOT_ASSISTANT_ID", "").strip()
        if not assistant or len(assistant) > 200:
            raise ValueError("QQBOT_ASSISTANT_ID 必须明确绑定现有角色")
        configured = env.get("MEMORY_ASSISTANT_ID", "").strip()
        if configured and configured != assistant:
            raise ValueError("QQBOT_ASSISTANT_ID 必须与 MEMORY_ASSISTANT_ID 一致")
        proactive = _flag(env, "QQBOT_PROACTIVE_ENABLED")
        tz = (env.get("QQBOT_TIMEZONE") or "Asia/Shanghai").strip()
        start, end = ((env.get(k) or d).strip() for k, d in (("QQBOT_ACTIVE_START", "09:00"), ("QQBOT_ACTIVE_END", "22:00")))
        try:
            ZoneInfo(tz)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("QQBOT_TIMEZONE 必须是有效IANA时区") from exc
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v) for v in (start, end)):
            raise ValueError("QQBOT_ACTIVE_START / END 必须是HH:mm")
        if proactive and any(not env.get(k, "").strip() for k in ("QQBOT_TIMEZONE", "QQBOT_ACTIVE_START", "QQBOT_ACTIVE_END")):
            raise ValueError("启用主动消息前须明确配置时区和发送时段")
        store = env.get("QQBOT_STORE_PATH", "data/qqbot.sqlite3").strip()
        if not store or store == ":memory:":
            raise ValueError("QQBOT_STORE_PATH 必须是持久化文件")
        prompt_file = env.get("QQBOT_SYSTEM_PROMPT_FILE", "").strip()
        if prompt_file and not Path(prompt_file).is_file():
            raise ValueError("QQBOT_SYSTEM_PROMPT_FILE 不存在")
        return cls(enabled=True, token=token, account_id=account, owner_id=owner, assistant_id=assistant,
                   store_path=store, sticker_manifest=env.get("QQBOT_STICKER_MANIFEST", "").strip(),
                   system_prompt_file=prompt_file, proactive_enabled=proactive, timezone=tz,
                   active_start=start, active_end=end,
                   debounce_seconds=_number(env,"QQBOT_DEBOUNCE_SECONDS",3,0,15),
                   max_batch_wait=_number(env,"QQBOT_MAX_BATCH_WAIT",10,1,30),
                   action_timeout=_number(env,"QQBOT_ACTION_TIMEOUT",15,1,60),
                   heartbeat_timeout=_number(env,"QQBOT_HEARTBEAT_TIMEOUT",90,10,600),
                   history_limit=_number(env,"QQBOT_HISTORY_LIMIT",30,2,100),
                   export_timeout=_number(env,'QQBOT_EXPORT_TIMEOUT',15,1,120),
                   check_seconds=_number(env,"QQBOT_CHECK_SECONDS",300,60,86400),
                   quiet_seconds=_number(env,"QQBOT_QUIET_SECONDS",1800,1,86400),
                   cooldown_seconds=_number(env,"QQBOT_COOLDOWN_SECONDS",3600,60,604800),
                   daily_limit=_number(env,"QQBOT_DAILY_LIMIT",3,1,100),
                   proactive_ttl=_number(env,"QQBOT_PROACTIVE_TTL",1800,60,7200))

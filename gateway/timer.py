"""标签解析与定时器管理。

从模型回复中解析 <<delay:N>>、<<schedule:HH:MM:summary>>、<<busy:N>> 标签，
注册对应的定时任务。
"""
import logging
import re
import time
from datetime import datetime, timezone, timedelta

from . import db

log = logging.getLogger("gateway.timer")

# ── 正则 ──────────────────────────────────────────
RE_DELAY = re.compile(r'<{1,2}delay:(\d+)>{1,2}[ \t]*\n?')
RE_SCHEDULE = re.compile(r'<{1,2}schedule:(\d{1,2}:\d{2}):(.+?)>{1,2}[ \t]*\n?')
RE_BUSY = re.compile(r'<{1,2}busy:(\d+)>{1,2}[ \t]*\n?')

# ── 配额 ──────────────────────────────────────────
MAX_DELAY_MINUTES = 360
MAX_BUSY_MINUTES = 480
MIN_BUSY_MINUTES = 30
MAX_SCHEDULES_PER_DAY = 5


def parse_and_strip_tags(text: str) -> tuple[str, list[dict]]:
    """解析并剥离回复末尾的标签。

    返回 (clean_text, tags)
    tags: [{"type": "delay", "minutes": 30}, {"type": "schedule", "time": "22:00", "summary": "..."}, ...]
    """
    tags = []

    # 扫描 busy
    for m in RE_BUSY.finditer(text):
        minutes = int(m.group(1))
        if MIN_BUSY_MINUTES <= minutes <= MAX_BUSY_MINUTES:
            tags.append({"type": "busy", "minutes": minutes})

    # 扫描 delay
    for m in RE_DELAY.finditer(text):
        minutes = int(m.group(1))
        if 1 <= minutes <= MAX_DELAY_MINUTES:
            tags.append({"type": "delay", "minutes": minutes})

    # 扫描 schedule
    for m in RE_SCHEDULE.finditer(text):
        time_str = m.group(1)
        summary = m.group(2).strip()
        # 校验时间格式
        try:
            parts = time_str.split(":")
            h, mi = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= mi <= 59:
                tags.append({"type": "schedule", "time": time_str, "summary": summary})
        except (ValueError, IndexError):
            pass

    # 互斥裁决：delay 优先于 busy
    has_delay = any(t["type"] == "delay" for t in tags)
    has_busy = any(t["type"] == "busy" for t in tags)
    if has_delay and has_busy:
        tags = [t for t in tags if t["type"] != "busy"]

    # 剥离标签文本
    clean = RE_DELAY.sub('', text)
    clean = RE_SCHEDULE.sub('', clean)
    clean = RE_BUSY.sub('', clean)
    clean = clean.rstrip()

    return clean, tags


def register_tags(tags: list[dict]):
    """将解析出的标签注册到数据库。"""
    if not tags:
        return

    now = datetime.now(timezone.utc)
    client = db.get_client()
    if not client:
        return

    for tag in tags:
        try:
            if tag["type"] == "delay":
                expire = now + timedelta(minutes=tag["minutes"])
                # 取消之前未执行的 delay（新 delay 覆盖旧的）
                client.table("timers").update({
                    "cancelled": True
                }).eq("type", "delay").eq("executed", False).eq("cancelled", False).execute()

                client.table("timers").insert({
                    "type": "delay",
                    "minutes": tag["minutes"],
                    "expire_at": expire.isoformat(),
                    "trigger_context": f"你 {tag['minutes']} 分钟前说过一会儿来找叶子。现在时间到了。",
                }).execute()
                log.info(f"注册 delay: {tag['minutes']}min → {expire.isoformat()}")

            elif tag["type"] == "schedule":
                time_str = tag["time"]
                h, m = map(int, time_str.split(":"))
                # 东八区
                cst = timezone(timedelta(hours=8))
                now_cst = datetime.now(cst)
                target = now_cst.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now_cst:
                    target += timedelta(days=1)  # 时间已过，顺延到明天
                target_date = target.date().isoformat()
                expire_utc = target.astimezone(timezone.utc)

                # 检查每日配额
                today_resp = client.table("timers").select("id").eq(
                    "type", "schedule"
                ).eq("target_date", target_date).eq("executed", False).eq("cancelled", False).execute()

                if today_resp.data and len(today_resp.data) >= MAX_SCHEDULES_PER_DAY:
                    # 淘汰最早的一条
                    oldest_id = today_resp.data[0]["id"]
                    client.table("timers").update({"cancelled": True}).eq("id", oldest_id).execute()

                client.table("timers").insert({
                    "type": "schedule",
                    "target_time": time_str,
                    "summary": tag["summary"],
                    "target_date": target_date,
                    "expire_at": expire_utc.isoformat(),
                    "trigger_context": f"定时提醒到了：{tag['summary']}（你之前设定的 {time_str} 提醒）",
                }).execute()
                log.info(f"注册 schedule: {time_str} - {tag['summary']}")

            elif tag["type"] == "busy":
                expire = now + timedelta(minutes=tag["minutes"])
                # 取消之前的 busy
                client.table("timers").update({
                    "cancelled": True
                }).eq("type", "busy").eq("executed", False).eq("cancelled", False).execute()

                client.table("timers").insert({
                    "type": "busy",
                    "minutes": tag["minutes"],
                    "expire_at": expire.isoformat(),
                    "trigger_context": f"忙碌时间结束了（设定了 {tag['minutes']} 分钟）。",
                }).execute()
                log.info(f"注册 busy: {tag['minutes']}min → {expire.isoformat()}")

        except Exception as e:
            log.error(f"注册标签失败 {tag}: {e}")


def cancel_delay_on_user_message():
    """用户发消息时取消未执行的 delay（对方主动来了，不需要再找她）。"""
    client = db.get_client()
    if not client:
        return
    try:
        client.table("timers").update({
            "cancelled": True
        }).eq("type", "delay").eq("executed", False).eq("cancelled", False).execute()
    except Exception as e:
        log.error(f"取消 delay 失败: {e}")


def get_active_busy() -> dict | None:
    """检查当前是否在 busy 状态。"""
    client = db.get_client()
    if not client:
        return None
    try:
        resp = client.table("timers").select("*").eq(
            "type", "busy"
        ).eq("executed", False).eq("cancelled", False).order("created_at", desc=True).limit(1).execute()
        if resp.data:
            expire_str = resp.data[0].get("expire_at", "")
            if expire_str:
                expire = datetime.fromisoformat(expire_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < expire:
                    return resp.data[0]
        return None
    except Exception as e:
        log.error(f"查询 busy 状态失败: {e}")
        return None


def save_to_busy_inbox(content: str):
    """busy 期间的消息存入 inbox。"""
    client = db.get_client()
    if not client:
        return
    try:
        client.table("busy_inbox").insert({"content": content}).execute()
    except Exception as e:
        log.error(f"写入 busy_inbox 失败: {e}")


def get_pending_timers() -> list[dict]:
    """获取到期但未执行的 timer。"""
    client = db.get_client()
    if not client:
        return []
    try:
        now = datetime.now(timezone.utc).isoformat()
        resp = client.table("timers").select("*").eq(
            "executed", False
        ).eq("cancelled", False).lte("expire_at", now).execute()
        return resp.data or []
    except Exception as e:
        log.error(f"查询到期 timer 失败: {e}")
        return []


def mark_executed(timer_id: int):
    """标记 timer 为已执行。"""
    client = db.get_client()
    if not client:
        return
    try:
        client.table("timers").update({
            "executed": True,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", timer_id).execute()
    except Exception as e:
        log.error(f"标记 timer 已执行失败: {e}")


def get_timer_status_for_context() -> str:
    """生成当前计时器状态摘要，注入到模型上下文。"""
    client = db.get_client()
    if not client:
        return ""
    try:
        resp = client.table("timers").select("*").eq(
            "executed", False
        ).eq("cancelled", False).order("expire_at").limit(10).execute()
        if not resp.data:
            return ""

        cst = timezone(timedelta(hours=8))
        lines = ["【当前计时器状态】"]
        for t in resp.data:
            expire_str = t.get("expire_at", "")
            if expire_str:
                expire = datetime.fromisoformat(expire_str.replace("Z", "+00:00")).astimezone(cst)
                expire_fmt = expire.strftime("%H:%M")
            else:
                expire_fmt = "未知"

            if t["type"] == "delay":
                lines.append(f"- 延时提醒：{t.get('minutes', '?')}分钟后（{expire_fmt}）主动找叶子")
            elif t["type"] == "schedule":
                lines.append(f"- 定时提醒：{t.get('target_time', '?')} - {t.get('summary', '')}")
            elif t["type"] == "busy":
                lines.append(f"- 忙碌模式：直到 {expire_fmt}")

        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        log.error(f"获取 timer 状态失败: {e}")
        return ""


def flush_busy_inbox() -> list[str]:
    """清空并返回 busy 期间积攒的消息。"""
    client = db.get_client()
    if not client:
        return []
    try:
        resp = client.table("busy_inbox").select("*").order("received_at").execute()
        if not resp.data:
            return []
        messages = [m["content"] for m in resp.data]
        # 清空
        for m in resp.data:
            client.table("busy_inbox").delete().eq("id", m["id"]).execute()
        return messages
    except Exception as e:
        log.error(f"清空 busy_inbox 失败: {e}")
        return []


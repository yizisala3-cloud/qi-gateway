"""记忆热度管理模块。

- 每日衰减：heat 随时间自然降低
- 升温：被召回时由 memory_search 调 RPC 完成
- 归档：heat 过低且长期未召回 → 标记 is_active=false
"""
import logging
from datetime import datetime, timezone, timedelta

from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_heat")

# 衰减参数
DECAY_BASE = 0.95       # 每天保留 95% 热度
ARCHIVE_THRESHOLD = 5.0  # 低于此热度考虑归档
ARCHIVE_DAYS = 90        # 超过此天数未召回才归档


def run_heat_decay():
    """对所有活跃记忆执行一次热度衰减。

    公式：heat_new = heat × (DECAY_BASE ^ days_since_recall) × emotion_factor
    emotion_factor = 1.0 + emotion_weight × 0.5（情绪越强衰减越慢）

    importance=10 的记忆永不衰减。
    """
    client = get_client()
    if not client:
        return

    try:
        # 拉所有活跃记忆
        resp = (
            client.table("memories")
            .select("id, heat, importance, emotion_weight, last_recalled_at, created_at")
            .eq("is_active", True)
            .neq("importance", 10)  # importance=10 永不衰减
            .execute()
        )
        if not resp.data:
            return

        now = datetime.now(timezone.utc)
        updated = 0
        archived = 0

        for mem in resp.data:
            heat = mem.get("heat", 50.0)
            emotion_weight = mem.get("emotion_weight", 0.5)

            # 计算距上次召回的天数
            last_recalled = mem.get("last_recalled_at")
            if last_recalled:
                try:
                    if isinstance(last_recalled, str):
                        last_dt = datetime.fromisoformat(last_recalled.replace("Z", "+00:00"))
                    else:
                        last_dt = last_recalled
                    days = (now - last_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    days = 1.0
            else:
                # 从未被召回，用创建时间
                created = mem.get("created_at", "")
                try:
                    if isinstance(created, str):
                        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    else:
                        created_dt = created
                    days = (now - created_dt).total_seconds() / 86400.0
                except (ValueError, TypeError):
                    days = 1.0

            # 限制最大衰减天数（防止极端值）
            days = min(days, 365.0)
            days = max(days, 0.0)

            # 情绪因子：emotion_weight 高的衰减慢
            emotion_factor = 1.0 + emotion_weight * 0.5

            # 计算新热度（每天衰减一点）
            # 只算今天这一轮的衰减（假设每天跑一次）
            decay_factor = DECAY_BASE ** (1.0 / emotion_factor)
            new_heat = heat * decay_factor

            # 归档判定
            if new_heat < ARCHIVE_THRESHOLD and days > ARCHIVE_DAYS:
                client.table("memories").update({
                    "is_active": False,
                    "heat": round(new_heat, 2),
                }).eq("id", mem["id"]).execute()
                archived += 1
            elif abs(new_heat - heat) > 0.01:
                client.table("memories").update({
                    "heat": round(new_heat, 2),
                }).eq("id", mem["id"]).execute()
                updated += 1

        if updated or archived:
            log.info(f"热度衰减完成: {updated} 条更新, {archived} 条归档")

    except Exception as e:
        log.error(f"热度衰减失败: {e}")

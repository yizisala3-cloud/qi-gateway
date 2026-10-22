"""Idempotent bridge into the existing immutable chat_messages/digest input."""
from datetime import datetime, timezone

from . import db


def export_message(config, key, row):
    client = db.get_client()
    if client is None or not db.get_client_status().get("elevated_active"):
        raise RuntimeError("qq_memory_server_key_unavailable")
    result = client.rpc("qqbot_publish_chat_message", {
        "p_message_key":key,"p_assistant_id":config.assistant_id,
        "p_conversation_id":config.conversation_id,"p_role":row["role"],
        "p_content":row["content"],
        "p_created_at":datetime.fromtimestamp(row["created"],timezone.utc).isoformat(),
    }).execute()
    if not isinstance(result.data,int) or isinstance(result.data,bool):
        raise RuntimeError("qq_memory_invalid_receipt")

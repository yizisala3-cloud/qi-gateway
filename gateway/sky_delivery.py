"""Persist only Sky replies whose local game receipt has been confirmed."""
from datetime import datetime
from uuid import UUID

from . import db
from .memory_extract import resolve_assistant_id


class DeliveryError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def validate_delivery(body):
    if not isinstance(body, dict) or body.get("confirmed") is not True:
        raise DeliveryError("game_receipt_required")
    try:
        delivery_id = str(UUID(body["delivery_id"]))
        stamp = datetime.fromisoformat(body["confirmed_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DeliveryError("invalid_delivery_identity") from None
    if stamp.tzinfo is None or stamp.year < 1970:
        raise DeliveryError("invalid_delivery_timestamp")
    vm = body.get("vm_index")
    query, text = body.get("query"), body.get("text")
    if type(vm) is not int or not 0 <= vm <= 9999:
        raise DeliveryError("invalid_delivery_device")
    if not isinstance(query, str) or not query.strip() or len(query) > 20000 or "\x00" in query:
        raise DeliveryError("invalid_delivery_query")
    if (not isinstance(text, str) or not text.strip() or len(text) > 100
            or text.strip().casefold() == "pass" or any(c in text for c in "\r\n\x00")):
        raise DeliveryError("invalid_delivered_text")
    return {"p_delivery_id": delivery_id, "p_vm_index": vm,
            "p_user_text": query, "p_assistant_text": text,
            "p_confirmed_at": stamp.isoformat()}


def publish_delivery(payload):
    # This is the server-owned, validated envelope, never the raw HTTP body.
    # Reusing an envelope retains its first resolved author; fresh requests are
    # also protected across process restarts by the RPC's original-row check.
    if "p_assistant_id" not in payload:
        payload["p_assistant_id"] = resolve_assistant_id()
    client = db.get_client()
    if client is None or not db.get_client_status().get("elevated_active"):
        raise DeliveryError("delivery_storage_unavailable", 503)
    try:
        result = client.rpc("sky_publish_chat_delivery", payload).execute()
    except Exception as exc:
        if getattr(exc, "code", "") == "22023":
            raise DeliveryError("delivery_payload_conflict", 409) from exc
        raise
    receipt = result.data
    if (not isinstance(receipt, dict) or receipt.get("delivery_id") != payload["p_delivery_id"]
            or any(type(receipt.get(key)) is not int or receipt[key] <= 0
                   for key in ("user_message_id", "assistant_message_id"))):
        raise DeliveryError("invalid_storage_receipt", 503)
    return receipt

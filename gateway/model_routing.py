"""Helpers for selecting the upstream chat model."""

DEFAULT_UPSTREAM_MODEL = "deepseek-v4-pro"


def select_upstream_model(configured_model: str, requested_model: str) -> str:
    """Prefer the deployment setting, otherwise preserve the client model."""
    return (configured_model or "").strip() or (requested_model or "").strip()

"""Helpers for selecting the upstream chat model."""

DEFAULT_UPSTREAM_MODEL = "deepseek-v4-pro"


def normalize_upstream_model(model: str) -> str:
    """Route legacy Claude-family client names to the current chat model."""
    if "claude" in model.casefold():
        return DEFAULT_UPSTREAM_MODEL
    return model

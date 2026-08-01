"""Helpers for selecting the upstream chat model."""

CLAUDE_UPSTREAM_MODEL = "[特特价次kiro]claude-opus-4-6"


def normalize_upstream_model(model: str) -> str:
    """Route every Claude-family model name to the configured replacement."""
    if "claude" in model.casefold():
        return CLAUDE_UPSTREAM_MODEL
    return model

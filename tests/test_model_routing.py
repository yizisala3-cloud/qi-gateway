import unittest

from gateway.config import DEFAULT_UPSTREAM_BASE_URL
from gateway.model_routing import DEFAULT_UPSTREAM_MODEL, normalize_upstream_model


class NormalizeUpstreamModelTests(unittest.TestCase):
    def test_replaces_claude_model_names(self):
        for model in (
            "claude-sonnet-4-20250514",
            "Claude-Opus-4",
            "provider/CLAUDE-haiku",
        ):
            with self.subTest(model=model):
                self.assertEqual(normalize_upstream_model(model), DEFAULT_UPSTREAM_MODEL)

    def test_keeps_non_claude_model_names(self):
        self.assertEqual(normalize_upstream_model("Qwen/Qwen2.5-7B-Instruct"), "Qwen/Qwen2.5-7B-Instruct")

    def test_replacement_is_idempotent(self):
        self.assertEqual(normalize_upstream_model(DEFAULT_UPSTREAM_MODEL), DEFAULT_UPSTREAM_MODEL)

    def test_deepseek_defaults_match_the_chat_provider(self):
        self.assertEqual(DEFAULT_UPSTREAM_BASE_URL, "https://api.deepseek.com/v1")
        self.assertEqual(DEFAULT_UPSTREAM_MODEL, "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()

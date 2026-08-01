import unittest

from gateway.model_routing import CLAUDE_UPSTREAM_MODEL, normalize_upstream_model


class NormalizeUpstreamModelTests(unittest.TestCase):
    def test_replaces_claude_model_names(self):
        for model in (
            "claude-sonnet-4-20250514",
            "Claude-Opus-4",
            "provider/CLAUDE-haiku",
        ):
            with self.subTest(model=model):
                self.assertEqual(normalize_upstream_model(model), CLAUDE_UPSTREAM_MODEL)

    def test_keeps_non_claude_model_names(self):
        self.assertEqual(normalize_upstream_model("Qwen/Qwen2.5-7B-Instruct"), "Qwen/Qwen2.5-7B-Instruct")

    def test_replacement_is_idempotent(self):
        self.assertEqual(normalize_upstream_model(CLAUDE_UPSTREAM_MODEL), CLAUDE_UPSTREAM_MODEL)


if __name__ == "__main__":
    unittest.main()

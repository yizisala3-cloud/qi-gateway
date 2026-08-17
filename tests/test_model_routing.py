import unittest

from gateway.config import DEFAULT_UPSTREAM_BASE_URL
from gateway.model_routing import DEFAULT_UPSTREAM_MODEL, select_upstream_model


class SelectUpstreamModelTests(unittest.TestCase):
    def test_configured_model_overrides_any_client_model(self):
        self.assertEqual(
            select_upstream_model("my-next-model", "claude-sonnet-4"),
            "my-next-model",
        )

    def test_client_model_is_preserved_without_a_configured_model(self):
        self.assertEqual(
            select_upstream_model("", "claude-sonnet-4"),
            "claude-sonnet-4",
        )

    def test_empty_models_remain_empty(self):
        self.assertEqual(select_upstream_model("", ""), "")

    def test_deepseek_defaults_match_the_chat_provider(self):
        self.assertEqual(DEFAULT_UPSTREAM_BASE_URL, "https://api.deepseek.com/v1")
        self.assertEqual(DEFAULT_UPSTREAM_MODEL, "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()

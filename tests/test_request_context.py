import unittest

from gateway.request_context import (
    GATEWAY_CONTEXT_HEADING,
    append_gateway_context,
    extract_last_user_text,
    is_orangechat_proactive_request,
)


class ProactiveDetectionTests(unittest.TestCase):
    def test_detects_timer_proactive_request(self):
        messages = [
            {
                "role": "system",
                "content": (
                    "原始人设\n\n## 主动消息触发（定时触发）\n"
                    "绝对不要复述上一轮的对话内容。"
                ),
            },
            {"role": "user", "content": "昨天的最后一条真人消息"},
            {"role": "assistant", "content": "昨天已经回复过的内容"},
            {
                "role": "user",
                "content": "请根据以上上下文决定是否发消息。没什么好说的就回复 [PASS] 即可，不要强行找话题。",
            },
        ]

        self.assertTrue(is_orangechat_proactive_request(messages))

    def test_detects_device_event_proactive_request_with_text_parts(self):
        messages = [
            {
                "role": "system",
                "content": "## ⚠️ 当前触发原因：用户手机动向（设备事件触发）",
            },
            {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "请根据以上用户动向决定是否发消息。没什么好说的就回复 [PASS]。",
                }],
            },
        ]

        self.assertTrue(is_orangechat_proactive_request(messages))

    def test_requires_both_system_and_synthetic_user_markers(self):
        only_system = [
            {"role": "system", "content": "## 主动消息触发（定时触发）"},
            {"role": "user", "content": "我们聊聊主动消息吧"},
        ]
        only_user = [
            {"role": "system", "content": "普通人设"},
            {"role": "user", "content": "请根据以上上下文决定是否发消息。"},
        ]

        self.assertFalse(is_orangechat_proactive_request(only_system))
        self.assertFalse(is_orangechat_proactive_request(only_user))

    def test_last_user_text_is_the_synthetic_trigger_not_history(self):
        messages = [
            {"role": "user", "content": "真人消息"},
            {"role": "assistant", "content": "已经回复"},
            {"role": "user", "content": "主动触发指令"},
        ]
        self.assertEqual(extract_last_user_text(messages), "主动触发指令")


class ContextInjectionTests(unittest.TestCase):
    def test_adds_separate_system_message_without_mutating_original_prompt(self):
        original_system = {"role": "system", "content": "原始 system prompt"}
        history = {"role": "user", "content": "你好"}
        messages = [original_system, history]

        result = append_gateway_context(messages, "补充记忆")

        self.assertEqual(messages, [original_system, history])
        self.assertEqual(original_system["content"], "原始 system prompt")
        self.assertIsNot(result, messages)
        self.assertIs(result[0], original_system)
        self.assertEqual(result[1]["role"], "system")
        self.assertIn(GATEWAY_CONTEXT_HEADING, result[1]["content"])
        self.assertIn("补充记忆", result[1]["content"])
        self.assertIs(result[2], history)

    def test_context_follows_all_leading_system_messages(self):
        messages = [
            {"role": "system", "content": "system 1"},
            {"role": "system", "content": "system 2"},
            {"role": "user", "content": "hello"},
        ]

        result = append_gateway_context(messages, "context")

        self.assertEqual([message["role"] for message in result], [
            "system", "system", "system", "user",
        ])
        self.assertIn(GATEWAY_CONTEXT_HEADING, result[2]["content"])

    def test_empty_context_still_returns_a_new_unmodified_list(self):
        messages = [{"role": "system", "content": "original"}]
        result = append_gateway_context(messages, "")
        self.assertEqual(result, messages)
        self.assertIsNot(result, messages)


if __name__ == "__main__":
    unittest.main()


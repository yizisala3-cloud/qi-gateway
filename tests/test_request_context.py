import unittest

from gateway.request_context import (
    GATEWAY_CONTEXT_HEADING,
    PROACTIVE_REPLY_HEADING,
    TODO_FEEDBACK_HEADING,
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    is_orangechat_proactive_request,
    require_proactive_reply,
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

    def test_proactive_requirement_explains_synthetic_trigger_without_editing_original_system(self):
        original_system = {
            "role": "system",
            "content": "原始人设和主动消息规则：没什么好说的就回复 [PASS]。",
        }
        history = [
            {"role": "user", "content": "最后一条真实消息"},
            {"role": "assistant", "content": "已经回复过"},
            {"role": "user", "content": "请根据以上上下文决定是否发消息。"},
        ]
        messages = [original_system, *history]

        result = require_proactive_reply(messages)

        self.assertEqual(messages, [original_system, *history])
        self.assertEqual(
            original_system["content"],
            "原始人设和主动消息规则：没什么好说的就回复 [PASS]。",
        )
        self.assertIs(result[0], original_system)
        self.assertEqual(result[1]["role"], "system")
        self.assertIn(PROACTIVE_REPLY_HEADING, result[1]["content"])
        self.assertIn("不是用户本人发言", result[1]["content"])
        self.assertIn("不要重复回答", result[1]["content"])
        self.assertIn("只读查询工具", result[1]["content"])
        self.assertIn("不要猜测或编造用户的信息", result[1]["content"])
        self.assertEqual(result[2:], history)


class TodoFeedbackGuidanceTests(unittest.TestCase):
    def test_detects_completion_postponement_and_cancellation_feedback(self):
        examples = (
            "报告已经交了",
            "这个待办晚点再做",
            "三个小时后再提醒我",
            "把它推迟到明天下午",
            "刚才那个提醒取消",
            "不用提醒我了",
        )
        for text in examples:
            with self.subTest(text=text):
                guidance = build_todo_feedback_guidance(text)
                self.assertIn(TODO_FEEDBACK_HEADING, guidance)
                self.assertIn("list_today_todos", guidance)
                self.assertIn("不得调用 create_todo", guidance)

    def test_does_not_treat_new_plans_or_progress_numbers_as_todo_feedback(self):
        examples = (
            "明天有个新任务",
            "代码完成度现在是 60%",
            "我们聊聊待办功能",
            "取消按钮怎么设计",
            "今天心情不错",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(build_todo_feedback_guidance(text), "")

    def test_ambiguous_feedback_requires_confirmation(self):
        guidance = build_todo_feedback_guidance("晚点再做")
        self.assertIn("匹配到多条", guidance)
        self.assertIn("先向用户确认", guidance)



if __name__ == "__main__":
    unittest.main()


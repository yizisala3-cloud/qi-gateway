import unittest

from gateway.request_context import (
    GATEWAY_CONTEXT_HEADING,
    PROACTIVE_CONTROL_HEADING,
    TODO_FEEDBACK_HEADING,
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    extract_recent_turns,
    is_orangechat_proactive_request,
    annotate_proactive_control_signal,
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

    def test_proactive_annotation_explains_control_signal_without_overriding_client_policy(self):
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

        result = annotate_proactive_control_signal(messages)

        self.assertEqual(messages, [original_system, *history])
        self.assertEqual(
            original_system["content"],
            "原始人设和主动消息规则：没什么好说的就回复 [PASS]。",
        )
        self.assertIs(result[0], original_system)
        self.assertEqual(result[1]["role"], "system")
        self.assertIn(PROACTIVE_CONTROL_HEADING, result[1]["content"])
        self.assertIn("不是用户本人发言", result[1]["content"])
        self.assertIn("不要重复回答", result[1]["content"])
        self.assertIn("遵循客户端原始 system prompt", result[1]["content"])
        self.assertNotIn("请直接输出", result[1]["content"])
        self.assertNotIn("必须回复", result[1]["content"])
        self.assertNotIn("NO_REPLY", result[1]["content"])
        self.assertNotIn("SKIP", result[1]["content"])
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



class RecentTurnsTests(unittest.TestCase):
    def test_normal_three_turns_are_paired_chronologically(self):
        messages = [
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "第一种方案讲什么？"},
            {"role": "assistant", "content": "第一种方案是异步写入。"},
            {"role": "user", "content": "第二种方案呢？"},
            {"role": "assistant", "content": "第二种方案是批量导入。"},
            {"role": "user", "content": "继续之前那个话题"},
            {"role": "assistant", "content": "好的，继续。"},
            {"role": "user", "content": "那它以后怎么办"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(
            turns,
            [
                ("第一种方案讲什么？", "第一种方案是异步写入。"),
                ("第二种方案呢？", "第二种方案是批量导入。"),
                ("继续之前那个话题", "好的，继续。"),
            ],
        )

    def test_more_than_three_turns_keeps_only_the_latest_three(self):
        messages = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "u4"},
            {"role": "assistant", "content": "a4"},
            {"role": "user", "content": "当前消息"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(
            turns,
            [("u2", "a2"), ("u3", "a3"), ("u4", "a4")],
        )

    def test_consecutive_assistants_keep_the_latest_one_per_turn(self):
        messages = [
            {"role": "user", "content": "第一种方案是什么"},
            {"role": "assistant", "content": "先说结论。"},
            {"role": "assistant", "content": "第一种方案是异步写入。"},
            {"role": "user", "content": "当前消息"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(turns, [("第一种方案是什么", "第一种方案是异步写入。")])

    def test_consecutive_users_open_separate_turns_without_overwriting(self):
        messages = [
            {"role": "user", "content": "第一种方案呢"},
            {"role": "user", "content": "第二种方案呢"},
            {"role": "assistant", "content": "分别是异步写入和批量导入。"},
            {"role": "user", "content": "当前消息"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(
            turns,
            [("第一种方案呢", None), ("第二种方案呢", "分别是异步写入和批量导入。")],
        )

    def test_leading_orphan_assistant_is_extra_context_not_a_turn(self):
        messages = [
            {"role": "assistant", "content": "开场白"},
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答"},
            {"role": "user", "content": "当前消息"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(turns, [(None, "开场白"), ("第一个问题", "第一个回答")])

    def test_orphan_assistant_is_dropped_when_turns_are_truncated(self):
        messages = [
            {"role": "assistant", "content": "开场白"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
            {"role": "user", "content": "当前消息"},
        ]

        turns = extract_recent_turns(messages)

        self.assertEqual(turns, [("u1", "a1"), ("u2", "a2"), ("u3", "a3")])

    def test_incomplete_history_keeps_actual_messages_without_fabricating(self):
        messages = [
            {"role": "user", "content": "没有回复的问题"},
            {"role": "user", "content": "当前消息"},
        ]

        self.assertEqual(extract_recent_turns(messages), [("没有回复的问题", None)])

    def test_system_tool_and_empty_messages_are_excluded(self):
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "tool", "content": "工具返回"},
            {"role": "assistant", "content": [{"type": "text", "text": " "}]},
            {"role": "user", "content": "按我们刚才确定的来"},
        ]

        self.assertEqual(extract_recent_turns(messages), [])

    def test_multimodal_content_only_contributes_text_parts(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                ],
            },
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "你刚才提到的那个问题"},
        ]

        self.assertEqual(
            extract_recent_turns(messages),
            [("看看这张图", "好的")],
        )

    def test_custom_turn_limit_is_respected(self):
        messages = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "当前消息"},
        ]

        self.assertEqual(extract_recent_turns(messages, max_turns=1), [("u2", "a2")])

    def test_non_list_input_returns_empty(self):
        self.assertEqual(extract_recent_turns(None), [])
        self.assertEqual(extract_recent_turns("text"), [])



if __name__ == "__main__":
    unittest.main()


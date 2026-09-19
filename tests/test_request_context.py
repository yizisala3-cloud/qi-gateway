import unittest

from gateway.request_context import (
    GATEWAY_CONTEXT_HEADING,
    TODO_FEEDBACK_HEADING,
    append_gateway_context,
    build_todo_feedback_guidance,
    extract_last_user_text,
    extract_recent_turns,
)


class LastUserTextTests(unittest.TestCase):
    def test_last_user_text_returns_the_latest_user_message(self):
        messages = [
            {"role": "user", "content": "真人消息"},
            {"role": "assistant", "content": "已经回复"},
            {"role": "user", "content": "最新的用户消息"},
        ]
        self.assertEqual(extract_last_user_text(messages), "最新的用户消息")

    def test_last_user_text_supports_multimodal_text_parts(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                ],
            },
        ]
        self.assertEqual(extract_last_user_text(messages), "看看这张图")

    def test_last_user_text_without_user_returns_empty(self):
        self.assertEqual(extract_last_user_text([{"role": "assistant", "content": "hi"}]), "")
        self.assertEqual(extract_last_user_text(None), "")


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


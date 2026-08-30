"""Remote MCP surface for continuity memory tools.

The official MCP SDK owns protocol negotiation, JSON-RPC framing and
Streamable HTTP behavior. This module only supplies authentication and the
seven typed memory tools. Each write tool exposes its own flat fields; the
server assembles continuity_data and always fixes continuity_type, source and
assistant_id, so the client can never guess the generic payload shape or
override the review policy.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .config import cfg
from .memory_requests import MemoryRequestError, create_memory_request
from .memory_review import list_reviewable_memory_requests, review_ai_memory_request


memory_mcp = MCPServer(
    name="qi-gateway-memory",
    title="Qi Gateway Memory",
    description="Write and review six-class continuity memories.",
    version="1.1.0",
)


def _assistant_id() -> str:
    assistant_id = cfg.MEMORY_ASSISTANT_ID.strip()
    if not assistant_id:
        raise ToolError("memory assistant is not configured")
    return assistant_id


def _tool_error(exc: MemoryRequestError) -> ToolError:
    return ToolError(f"{exc.code}: {exc}")


_COMMON_RULES = (
    "【何时调用】仅在出现值得跨窗口保留、以后可能影响理解、延续话题或互动方式的内容时调用。"
    "以下情况不要调用：普通寒暄；临时措辞；单纯测试 MCP 是否连接；已经写入的重复内容；"
    "模型自己的推测；为了展示工具可用而制造虚假记忆；仅因为用户提到某件事一次就推断为稳定 profile；"
    "普通待办事项不能为了方便全部写成 thread。"
    "每一项实际记忆通常只调用一个最匹配的类型工具一次，不要为同一事实依次尝试多个类型工具。\n"
    "【content】5-600 字符；必须脱离当前窗口仍能独立理解；"
    "避免“刚才”“这个”“上面说的”等失去上下文后无法理解的指代；"
    "不把结构化字段机械重复成正文；不编造用户没有表达的信息。\n"
    "【reason】说明为什么该信息以后值得召回，不要只写“用户要求保存”。\n"
    "【recall_scene】以后触发召回的自然语言场景：什么情况下应该想起这条记忆；"
    "它是检索用的场景描述，不是记忆正文，不要复制正文；有可靠依据时填写，没有就省略，不要编造。\n"
    "【recall_tags】自由填写的召回场景标签数组，帮助以后按场景归类检索；"
    "不限制数量和内容，但必须来自对话中的真实依据，没有可靠依据时省略。\n"
    "【通用元数据】importance 为 1-10 整数，默认 5；continuity_value 未提供时沿用 importance；"
    "subject 合法值为 yezi/qi/shared/project/other；"
    "source_type 合法值为 natural_chat/persona_prompt/code/document/quote/roleplay/tool_result/system_meta/unknown；"
    "participants 只允许 yezi/qi/other；"
    "update_mode=append 表示新增独立记忆（memory_key 必须为空），"
    "update_mode=replace 表示替换同一项可变事实、同一条持续线索或同一条规则的旧版本（memory_key 必填，"
    "同一对象后续更新必须复用相同 memory_key，不得用 replace 合并普通相似事件）；"
    "title、tags、时间类可选字段没有证据时不要编造。"
)

_REVIEW_RULES = (
    "列出或处理仅允许 AI 审核的 pending 记忆申请。"
    "AI 只能审核 moment、thread、inside_joke 三类；"
    "episode、profile、interaction_rule 不能审核，永远留给叶子在后台人工处理，即使认为内容正确也不能尝试绕过。\n"
    "建议流程：先 action=list 查看待审核项，从返回结果中选择明确可判断的 request_id，"
    "再执行 approve、reject、merge、duplicate 或 conflict；没有待审核项目时停止，不要循环 list；"
    "remember_* 工具直接写入的内容不会进入 pending；不得猜测 request_id 或 related_memory_id。\n"
    "【list】不需要 request_id 和编辑字段；返回最多 50 条当前 assistant 可审核的 pending，"
    "不会返回 episode、profile、interaction_rule。\n"
    "【approve】request_id 必填；content、title、tags、importance 可省略，省略时沿用申请内容；"
    "content 为 5-600 字符；title 最长 100；tags 最多 5 项、每项最长 24；importance 为 1-10；"
    "update_mode 可选 append（不得带 memory_key）或 replace（必须带 memory_key）；"
    "review_note 可选，最长 500。\n"
    "【reject】request_id 必填；只允许附带可选 review_note，"
    "不得附带 content、title、tags、importance、update_mode、memory_key、related_memory_id。\n"
    "【merge】request_id 和 related_memory_id 必填；"
    "related_memory_id 必须是一条 active 且 verified 的正式 memory ID（不是申请 ID）；"
    "content 应是整理后的最终合并内容；title、tags、importance 可选；"
    "不允许 memory_key 或 update_mode；内容完全相同时应使用 duplicate。\n"
    "【duplicate】request_id 和 related_memory_id 必填；可选 review_note；不得附带记忆编辑字段。\n"
    "【conflict】request_id 和 related_memory_id 必填；可选 review_note；不得附带记忆编辑字段；"
    "表示交给叶子后续判断，不代表自动覆盖正式记忆。\n"
    "找不到明确目标时不要执行 merge、duplicate 或 conflict。"
    "本工具不会写 memory_relations，也不能处理已审核过、类型不允许或不属于当前 assistant 的申请。"
)

_CONTENT = Annotated[str, Field(
    description="记忆正文，5-600 字符，必须脱离当前窗口仍能独立理解",
    min_length=5, max_length=600,
)]
_REASON = Annotated[str, Field(
    description="为什么这条信息以后值得召回",
    min_length=3, max_length=500,
)]
_TITLE = Annotated[str | None, Field(description="可选标题，最长 100 字符；没有证据时省略", max_length=100)]
_TAGS = Annotated[list[str] | None, Field(description="可选标签，最多 5 个；没有证据时省略", max_length=5)]
_RECALL_SCENE = Annotated[str | None, Field(
    description="可选召回场景：以后什么情况下应该想起这条记忆的自然语言描述；"
                "是检索场景，不是记忆正文；没有可靠依据时省略，不要编造",
)]
_RECALL_TAGS = Annotated[list[str] | None, Field(
    description="可选召回场景标签数组，自由填写，不限制数量和内容；"
                "必须来自对话真实依据，没有可靠依据时省略",
)]
_IMPORTANCE = Annotated[int, Field(description="重要性 1-10 整数，默认 5", ge=1, le=10)]
_CONTINUITY_VALUE = Annotated[int | None, Field(
    description="连续感价值 1-10 整数；未提供时沿用 importance", ge=1, le=10,
)]
_SUBJECT = Literal["yezi", "qi", "shared", "project", "other"]
_SOURCE_TYPE = Literal[
    "natural_chat", "persona_prompt", "code", "document",
    "quote", "roleplay", "tool_result", "system_meta", "unknown",
]
_RETENTION_CLASS = Literal["normal", "core"]
_PARTICIPANTS = Annotated[list[Literal["yezi", "qi", "other"]] | None, Field(
    description="参与者列表，只能包含 yezi/qi/other；默认 [yezi, qi]", max_length=3,
)]
_UPDATE_MODE = Literal["append", "replace"]
_MEMORY_KEY = Annotated[str | None, Field(
    description="稳定主题键（^[a-z0-9][a-z0-9._:/-]{2,119}$）；append 时必须为空，"
                "replace 时必填，同一对象后续更新必须复用相同键",
    pattern=r"^[a-z0-9][a-z0-9._:/-]{2,119}$",
)]
_CONVERSATION_ID = Annotated[str | None, Field(description="可选会话 ID", max_length=100)]
_SOURCE_MESSAGE_ID = Annotated[int | None, Field(description="可选来源消息 ID", ge=1)]
_OPT_TIME = Annotated[str | None, Field(
    description="ISO 日期或日期时间，最长 40 字符；没有证据时省略", max_length=40,
)]
_OPT_TEXT = Annotated[str | None, Field(description="可选文本，最长 600 字符；没有证据时省略", max_length=600)]
_OPT_STR_LIST = Annotated[list[str] | None, Field(
    description="字符串数组，最多 8 项，每项 1-120 字符；没有证据时省略", max_length=8,
)]


async def _submit_typed_memory(
    *,
    continuity_type: str,
    content: str,
    reason: str,
    continuity_data: dict[str, Any],
    thread_state: str | None,
    title: str | None,
    tags: list[str] | None,
    recall_scene: str | None,
    recall_tags: list[str] | None,
    importance: int,
    continuity_value: int | None,
    subject: str,
    source_type: str,
    retention_class: str,
    participants: list[str] | None,
    update_mode: str,
    memory_key: str | None,
    conversation_id: str | None,
    source_message_id: int | None,
) -> dict[str, Any]:
    """Assemble the generic payload and call the shared request service.

    Field validation stays in gateway/memory_continuity_schema.py, the memory
    request service and the database validator/RPC; this helper only fixes the
    type, assembles arguments and injects the server-controlled identity.
    """
    payload = {
        "content": content,
        "reason": reason,
        "continuity_type": continuity_type,
        "continuity_data": {key: value for key, value in continuity_data.items() if value is not None},
        "thread_state": thread_state,
        "title": title,
        "tags": tags or [],
        "recall_scene": recall_scene,
        "recall_tags": recall_tags or [],
        "importance": importance,
        "continuity_value": importance if continuity_value is None else continuity_value,
        "subject": subject,
        "source_type": source_type,
        "retention_class": retention_class,
        "participants": participants or ["yezi", "qi"],
        "update_mode": update_mode,
        "memory_key": memory_key,
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
    }
    try:
        return await asyncio.to_thread(
            create_memory_request,
            payload,
            "",
            source="mcp_memory",
            assistant_id=_assistant_id(),
        )
    except MemoryRequestError as exc:
        raise _tool_error(exc) from exc


@memory_mcp.tool(
    name="remember_moment",
    description=(
        "记录一个具体、值得以后想起的片段或瞬间，例如某次确认、共同反应、"
        "具有情绪或叙事意义的小事件。校验成功后直接写入正式记忆，不进入叶子审核。"
        "不适合完整长经历（用 propose_episode）、稳定资料（用 propose_profile）"
        "或长期互动规则（用 propose_interaction_rule）；普通连接测试不应写入。\n" + _COMMON_RULES
    ),
)
async def remember_moment(
    content: _CONTENT,
    reason: _REASON,
    scene: Annotated[str, Field(description="场景：事情发生的环境或背景", min_length=1, max_length=600)],
    event: Annotated[str, Field(description="事件：具体发生了什么", min_length=1, max_length=600)],
    moment_state: Annotated[Literal["standalone", "linked", "absorbed"], Field(
        description="片段状态：standalone 独立片段 / linked 关联其他记忆 / absorbed 已被更大记忆吸收",
    )],
    response: Annotated[str | None, Field(description="可选：当时的回应或互动", max_length=600)] = None,
    outcome: Annotated[str | None, Field(description="可选：结果或后续", max_length=600)] = None,
    salience_reason: Annotated[str | None, Field(description="可选：为什么这个瞬间值得记住", max_length=600)] = None,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    update_mode: _UPDATE_MODE = "append",
    memory_key: _MEMORY_KEY = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="moment",
        content=content,
        reason=reason,
        thread_state=None,
        continuity_data={
            "scene": scene,
            "event": event,
            "moment_state": moment_state,
            "response": response,
            "outcome": outcome,
            "salience_reason": salience_reason,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode=update_mode,
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="remember_thread",
    description=(
        "记录一个尚未完成、以后需要继续跟进或确认结局的线索。校验成功后直接写入正式记忆。"
        "thread 不是普通待办：只有具有跨窗口连续意义的开放问题、项目进度、关系线索或等待后续结果的事情"
        "才使用；更新同一条 thread 时使用 replace 并复用相同 memory_key。\n"
        "【thread_state】open/paused 不允许 closure_summary、closure_reason、closed_at；"
        "resolved/dissolved/abandoned 必须同时填写这三个闭合字段；"
        "unknown 表示当前无法判断状态，不要编造关闭字段。"
        "opened_at、closed_at 使用 ISO 日期或日期时间；"
        "closure_criteria、abstract_retrieval_hints、concrete_retrieval_hints 为字符串数组，最多 8 项。\n"
        + _COMMON_RULES
    ),
)
async def remember_thread(
    content: _CONTENT,
    reason: _REASON,
    thread_state: Annotated[Literal["open", "paused", "resolved", "dissolved", "abandoned", "unknown"], Field(
        description="线索状态：open 进行中 / paused 暂停 / resolved 已解决 / dissolved 已消解 / "
                    "abandoned 已放弃 / unknown 无法判断",
    )],
    open_question: Annotated[str, Field(description="开放问题：这条线索要回答或解决什么", min_length=1, max_length=600)],
    current_state: Annotated[str, Field(description="当前进展或最新状态", min_length=1, max_length=600)],
    next_expected: Annotated[str | None, Field(description="可选：下一步预期会发生什么", max_length=600)] = None,
    closure_criteria: Annotated[list[str] | None, Field(
        description="可选：关闭标准，字符串数组，最多 8 项，每项 1-120 字符", max_length=8,
    )] = None,
    closure_summary: Annotated[str | None, Field(
        description="闭合总结；仅 resolved/dissolved/abandoned 必填", max_length=600,
    )] = None,
    closure_reason: Annotated[str | None, Field(
        description="闭合原因；仅 resolved/dissolved/abandoned 必填", max_length=600,
    )] = None,
    opened_at: _OPT_TIME = None,
    closed_at: _OPT_TIME = None,
    abstract_retrieval_hints: _OPT_STR_LIST = None,
    concrete_retrieval_hints: _OPT_STR_LIST = None,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    update_mode: _UPDATE_MODE = "append",
    memory_key: _MEMORY_KEY = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="thread",
        content=content,
        reason=reason,
        thread_state=thread_state,
        continuity_data={
            "open_question": open_question,
            "current_state": current_state,
            "next_expected": next_expected,
            "closure_criteria": closure_criteria,
            "closure_summary": closure_summary,
            "closure_reason": closure_reason,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "abstract_retrieval_hints": abstract_retrieval_hints,
            "concrete_retrieval_hints": concrete_retrieval_hints,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode=update_mode,
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="remember_inside_joke",
    description=(
        "记录双方共享、以后再次出现时能够触发共同含义或特定回应方式的内部梗。"
        "校验成功后直接写入正式记忆。一次性的普通玩笑不一定值得保存；"
        "必须能够说明来源、触发方式和共享含义。"
        "trigger_phrases 为非空字符串数组，最多 8 项，每项 1-120 字符；"
        "usage_context、avoid_context 最多 8 项；reinforcement_count 为非负整数；"
        "时间使用 ISO 日期或日期时间。\n" + _COMMON_RULES
    ),
)
async def remember_inside_joke(
    content: _CONTENT,
    reason: _REASON,
    origin: Annotated[str, Field(description="梗的来源：如何产生", min_length=1, max_length=600)],
    trigger_phrases: Annotated[list[str], Field(
        description="触发短语，非空字符串数组，最多 8 项，每项 1-120 字符",
        min_length=1, max_length=8,
    )],
    shared_meaning: Annotated[str, Field(description="共享含义：这个梗对双方意味着什么", min_length=1, max_length=600)],
    usage_context: _OPT_STR_LIST = None,
    avoid_context: _OPT_STR_LIST = None,
    response_style: Annotated[str | None, Field(description="可选：出现该梗时的典型回应方式", max_length=600)] = None,
    first_seen_at: _OPT_TIME = None,
    last_reinforced_at: _OPT_TIME = None,
    reinforcement_count: Annotated[int, Field(description="加强次数，非负整数，默认 0", ge=0)] = 0,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    update_mode: _UPDATE_MODE = "append",
    memory_key: _MEMORY_KEY = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="inside_joke",
        content=content,
        reason=reason,
        thread_state=None,
        continuity_data={
            "origin": origin,
            "trigger_phrases": trigger_phrases,
            "shared_meaning": shared_meaning,
            "usage_context": usage_context,
            "avoid_context": avoid_context,
            "response_style": response_style,
            "first_seen_at": first_seen_at,
            "last_reinforced_at": last_reinforced_at,
            "reinforcement_count": reinforcement_count,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode=update_mode,
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="propose_episode",
    description=(
        "将一段有开始、发展和结果的完整共同经历整理成记忆申请。"
        "只创建 pending，必须等待叶子审核，不会立即进入正式记忆。"
        "一个短暂片段应使用 remember_moment，而不是 episode；"
        "尚未结束的事情通常应使用 remember_thread。\n"
        + _COMMON_RULES
    ),
)
async def propose_episode(
    content: _CONTENT,
    reason: _REASON,
    beginning: Annotated[str, Field(description="经历的开端", min_length=1, max_length=600)],
    development: Annotated[str, Field(description="经历的发展过程", min_length=1, max_length=600)],
    outcome: Annotated[str, Field(description="经历的结果", min_length=1, max_length=600)],
    closure_quality: Annotated[Literal["complete", "partial", "uncertain"], Field(
        description="完结程度：complete 完整 / partial 部分 / uncertain 不确定",
    )],
    turning_point: Annotated[str | None, Field(description="可选：转折点", max_length=600)] = None,
    aftereffect: Annotated[str | None, Field(description="可选：后续影响", max_length=600)] = None,
    episode_start_time: _OPT_TIME = None,
    episode_end_time: _OPT_TIME = None,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    update_mode: _UPDATE_MODE = "append",
    memory_key: _MEMORY_KEY = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="episode",
        content=content,
        reason=reason,
        thread_state=None,
        continuity_data={
            "beginning": beginning,
            "development": development,
            "turning_point": turning_point,
            "outcome": outcome,
            "aftereffect": aftereffect,
            "episode_start_time": episode_start_time,
            "episode_end_time": episode_end_time,
            "closure_quality": closure_quality,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode=update_mode,
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="propose_profile",
    description=(
        "提交叶子的稳定资料、明确偏好、长期事实或具有范围限制的资料申请。"
        "只创建 pending，必须等待叶子审核，不会立即写入正式记忆。"
        "不得根据一次性情绪或模型猜测创建 profile；"
        "必须有明确自述、明确偏好、重复观察或已审核总结作为依据。"
        "exceptions 为字符串数组，最多 8 项。\n" + _COMMON_RULES
    ),
)
async def propose_profile(
    content: _CONTENT,
    reason: _REASON,
    facet: Annotated[str, Field(description="资料侧面：这条 profile 描述哪一方面", min_length=1, max_length=600)],
    statement: Annotated[str, Field(description="资料陈述：具体内容", min_length=1, max_length=600)],
    scope: Annotated[str, Field(description="适用范围：在什么情境下成立", min_length=1, max_length=600)],
    stability: Annotated[Literal["stable", "contextual", "provisional"], Field(
        description="稳定性：stable 稳定 / contextual 依赖情境 / provisional 暂定",
    )],
    basis: Annotated[
        Literal["explicit_self_report", "explicit_preference", "repeated_observation", "reviewed_summary"],
        Field(description="依据：明确自述 / 明确偏好 / 重复观察 / 已审核总结"),
    ],
    effective_from: _OPT_TIME = None,
    effective_until: _OPT_TIME = None,
    exceptions: _OPT_STR_LIST = None,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    update_mode: _UPDATE_MODE = "append",
    memory_key: _MEMORY_KEY = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="profile",
        content=content,
        reason=reason,
        thread_state=None,
        continuity_data={
            "facet": facet,
            "statement": statement,
            "scope": scope,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "stability": stability,
            "exceptions": exceptions,
            "basis": basis,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode=update_mode,
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="propose_interaction_rule",
    description=(
        "提交叶子明确提出的长期互动规则，例如在特定情况下应该怎么回应、禁止怎么回应。"
        "只创建 pending，必须等待叶子审核。"
        "模型不能根据叶子的语气、习惯或一次反应自行推断规则，必须源自叶子的明确指令；"
        "explicit_instruction 必须是叶子的明确原话或不改变含义的直接转述，不得填模型推测。"
        "第一次使用新的 memory_key 合法（服务端会创建 continuity_id），"
        "后续更新同一规则必须复用完全相同的 memory_key；本工具固定使用替换更新（replace）。"
        "priority 为 1-10 整数；rule_state 为 active/revoked/superseded；"
        "forbidden_behavior、exceptions 为字符串数组，最多 8 项。\n" + _COMMON_RULES
    ),
)
async def propose_interaction_rule(
    content: _CONTENT,
    reason: _REASON,
    memory_key: Annotated[str, Field(
        description="规则的稳定主题键（^[a-z0-9][a-z0-9._:/-]{2,119}$）；"
                    "同一规则后续更新必须复用完全相同的键",
        pattern=r"^[a-z0-9][a-z0-9._:/-]{2,119}$",
    )],
    trigger: Annotated[str, Field(description="触发条件：什么情况下适用该规则", min_length=1, max_length=600)],
    expected_behavior: Annotated[str, Field(description="期望行为：应该怎么回应", min_length=1, max_length=600)],
    scope: Annotated[str, Field(description="适用范围", min_length=1, max_length=600)],
    priority: Annotated[int, Field(description="优先级 1-10 整数", ge=1, le=10)],
    rule_state: Annotated[Literal["active", "revoked", "superseded"], Field(
        description="规则状态：active 生效 / revoked 撤销 / superseded 已被替代",
    )],
    explicit_instruction: Annotated[str, Field(
        description="叶子的明确原话或不改变含义的直接转述；不得填模型推测",
        min_length=1, max_length=600,
    )],
    forbidden_behavior: _OPT_STR_LIST = None,
    effective_from: _OPT_TIME = None,
    effective_until: _OPT_TIME = None,
    exceptions: _OPT_STR_LIST = None,
    title: _TITLE = None,
    tags: _TAGS = None,
    recall_scene: _RECALL_SCENE = None,
    recall_tags: _RECALL_TAGS = None,
    importance: _IMPORTANCE = 5,
    continuity_value: _CONTINUITY_VALUE = None,
    subject: _SUBJECT = "shared",
    source_type: _SOURCE_TYPE = "natural_chat",
    retention_class: _RETENTION_CLASS = "normal",
    participants: _PARTICIPANTS = None,
    conversation_id: _CONVERSATION_ID = None,
    source_message_id: _SOURCE_MESSAGE_ID = None,
) -> dict[str, Any]:
    return await _submit_typed_memory(
        continuity_type="interaction_rule",
        content=content,
        reason=reason,
        thread_state=None,
        continuity_data={
            "trigger": trigger,
            "expected_behavior": expected_behavior,
            "forbidden_behavior": forbidden_behavior,
            "scope": scope,
            "priority": priority,
            "rule_state": rule_state,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "exceptions": exceptions,
            "explicit_instruction": explicit_instruction,
        },
        title=title,
        tags=tags,
        recall_scene=recall_scene,
        recall_tags=recall_tags,
        importance=importance,
        continuity_value=continuity_value,
        subject=subject,
        source_type=source_type,
        retention_class=retention_class,
        participants=participants,
        update_mode="replace",
        memory_key=memory_key,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )


@memory_mcp.tool(
    name="review_memory_requests",
    description=_REVIEW_RULES,
)
async def review_memory_requests(
    action: Annotated[Literal["list", "approve", "reject", "merge", "duplicate", "conflict"], Field(
        description="list 列出待审核 / approve 通过 / reject 拒绝 / merge 合并 / duplicate 标记重复 / conflict 标记冲突",
    )],
    request_id: Annotated[int | None, Field(description="目标申请 ID；list 不需要，其他 action 必填", ge=1)] = None,
    content: Annotated[str | None, Field(
        description="approve/merge 时的编辑后内容，5-600 字符；省略时沿用申请内容",
        min_length=5, max_length=600,
    )] = None,
    title: Annotated[str | None, Field(description="approve/merge 时的标题，最长 100", max_length=100)] = None,
    tags: Annotated[list[str] | None, Field(description="approve/merge 时的标签，最多 5 项，每项最长 24", max_length=5)] = None,
    importance: Annotated[int | None, Field(description="approve/merge 时的重要性 1-10", ge=1, le=10)] = None,
    review_note: Annotated[str | None, Field(description="可选审核备注，最长 500", max_length=500)] = None,
    update_mode: Annotated[Literal["append", "replace"] | None, Field(
        description="approve 可选：append 新增（不得带 memory_key）/ replace 替换旧版本（必须带 memory_key）",
    )] = None,
    memory_key: Annotated[str | None, Field(
        description="approve 且 update_mode=replace 时必填的稳定主题键",
        pattern=r"^[a-z0-9][a-z0-9._:/-]{2,119}$",
    )] = None,
    related_memory_id: Annotated[int | None, Field(
        description="merge/duplicate/conflict 必填：目标正式 memory ID（active 且 verified），不是申请 ID", ge=1,
    )] = None,
) -> dict[str, Any]:
    assistant_id = _assistant_id()
    try:
        if action == "list":
            return {
                "requests": await asyncio.to_thread(
                    list_reviewable_memory_requests, assistant_id, 50
                )
            }
        if request_id is None:
            raise MemoryRequestError("invalid_review", "request_id is required")
        payload = {
            key: value
            for key, value in {
                "action": action,
                "content": content,
                "title": title,
                "tags": tags,
                "importance": importance,
                "review_note": review_note,
                "update_mode": update_mode,
                "memory_key": memory_key,
                "related_memory_id": related_memory_id,
            }.items()
            if value is not None
        }
        return await asyncio.to_thread(
            review_ai_memory_request, assistant_id, request_id, payload
        )
    except MemoryRequestError as exc:
        raise _tool_error(exc) from exc


class MCPBearerAuth:
    """Minimal ASGI guard that never reads or logs the MCP request body."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        configured = cfg.MCP_MEMORY_TOKEN.strip()
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        supplied = auth.removeprefix("Bearer ").strip()
        if not configured:
            await self._reject(send, 503, "mcp_not_configured")
            return
        if not supplied or not hmac.compare_digest(supplied, configured):
            await self._reject(send, 401, "unauthorized")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, status: int, code: str) -> None:
        body = json.dumps({"error": code}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


memory_mcp_http_app = MCPBearerAuth(
    memory_mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=65_536,
        # This is a public, bearer-protected gateway mounted behind the same
        # reverse proxy as /v1. Pinning the SDK to localhost Host values would
        # reject the user's real gateway domain.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
)

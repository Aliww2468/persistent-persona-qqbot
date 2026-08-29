"""事件主观评价器：AI理解语境，程序验证证据并计算影响。

默认运行在影子模式：只记录候选事件和评分，不改变雪的回复、正式记忆、
情绪或心理图式。未来可以复用 ``promote_candidate_to_memory`` 接口显式晋升。
"""

from __future__ import annotations

import asyncio
import json
from difflib import SequenceMatcher
from typing import Any

from nonebot import get_driver
from nonebot.log import logger

from core.decay import clamp
from core.llm import complete_json
from core.memory import (
    get_active_schemas,
    get_appraisal_checkpoint,
    get_effective_emotional_state,
    get_recent_messages_with_ids,
    recent_candidate_events,
    save_appraisal_run,
    save_event_candidate,
    set_appraisal_checkpoint,
)
from core.persona import get_relationship, load_persona

config = get_driver().config
APPRAISAL_EVERY = int(getattr(config, "appraisal_every", 6))
APPRAISAL_WINDOW = int(getattr(config, "appraisal_window", 14))
APPRAISAL_MAX_CANDIDATES = int(getattr(config, "appraisal_max_candidates", 3))
SHADOW_MODE = bool(getattr(config, "appraisal_shadow_mode", True))

EVENT_TYPES = {
    "user_fact",
    "shared_event",
    "promise",
    "relationship_event",
    "character_event",
    "temporary_context",
}

_user_locks: dict[str, asyncio.Lock] = {}


def _growth_policy() -> dict[str, Any]:
    return load_persona().get("growth_policy", {})


def calculate_candidate(
    raw: dict[str, Any],
    *,
    user_id: str,
    valid_message_ids: set[int],
    schemas: list[dict],
    existing_events: list[str] | None = None,
) -> dict[str, Any]:
    """把模型提议转换为受程序约束、可审计的候选事件。"""
    policy = _growth_policy()
    schema_map = {item["id"]: item for item in schemas}
    event_type = str(raw.get("event_type", "shared_event"))
    if event_type not in EVENT_TYPES:
        event_type = "shared_event"

    observed_event = str(raw.get("observed_event", "")).strip()[:500]
    subjective_meaning = str(raw.get("subjective_meaning", "")).strip()[:500]
    evidence_ids = _valid_int_list(raw.get("evidence_message_ids", []))
    evidence_valid = bool(evidence_ids) and set(evidence_ids).issubset(valid_message_ids)

    scores = raw.get("scores", {}) if isinstance(raw.get("scores"), dict) else {}
    confidence = clamp(scores.get("confidence", raw.get("confidence", 0.0)))
    emotional_intensity = clamp(scores.get("emotional_intensity", 0.0))
    relationship_weight = clamp(scores.get("relationship_weight", 0.0))
    vulnerability = clamp(scores.get("vulnerability", 0.0))
    social_cost = clamp(scores.get("social_cost", 0.0))
    identity_relevance = clamp(scores.get("identity_relevance", 0.0))
    expectation_violation = clamp(scores.get("expectation_violation", 0.0))

    activated_schemas = []
    raw_activations = []
    for activation in raw.get("activated_schemas", []):
        if not isinstance(activation, dict):
            continue
        schema_id = str(activation.get("schema_id", ""))
        if schema_id not in schema_map:
            continue
        value = clamp(activation.get("activation", 0.0))
        schema = schema_map[schema_id]
        raw_activations.append(value)
        activated_schemas.append(
            {
                "schema_id": schema_id,
                "activation": round(value, 4),
                "direction": "reinforce"
                if activation.get("direction") == "reinforce"
                else "contradict",
                "reason": str(activation.get("reason", ""))[:240],
            }
        )
    # AI判断的是“此刻被击中多强”；schema.sensitivity 用于提供上下文，
    # 不能再次把已经识别出的高激活压低，否则临界的单次转折会被漏掉。
    schema_activation = max(raw_activations, default=0.0)
    breakthrough = schema_activation * expectation_violation * relationship_weight
    importance = (
        emotional_intensity * 0.25
        + breakthrough * 0.30
        + vulnerability * 0.15
        + social_cost * 0.15
        + identity_relevance * 0.15
    )
    importance = clamp(importance)

    turning_point = (
        schema_activation
        >= float(policy.get("turning_point_schema_activation", 0.80))
        and expectation_violation
        >= float(policy.get("turning_point_expectation_violation", 0.75))
        and emotional_intensity
        >= float(policy.get("turning_point_emotional_intensity", 0.75))
    )

    status = "shadow" if SHADOW_MODE else "candidate"
    rejection_reason = ""
    if not observed_event:
        status, rejection_reason = "rejected", "缺少客观事件"
    elif not evidence_valid:
        status, rejection_reason = "rejected", "证据消息不存在或超出本次上下文"
    elif confidence < 0.45:
        status, rejection_reason = "rejected", "模型置信度过低"
    elif _is_duplicate(observed_event, existing_events or []):
        status, rejection_reason = "duplicate", "与近期候选事件高度重复"
    elif importance < float(policy.get("candidate_threshold", 0.48)) and not turning_point:
        status, rejection_reason = "below_threshold", "未达到候选事件阈值"

    return {
        "user_id": str(user_id),
        "event_type": event_type,
        "observed_event": observed_event,
        "subjective_meaning": subjective_meaning,
        "evidence_message_ids": evidence_ids,
        "activated_schemas": activated_schemas,
        "emotions": _normalize_emotions(raw.get("emotions", [])),
        "proposed_belief_changes": _normalize_belief_changes(
            raw.get("proposed_belief_changes", []), schema_map
        ),
        "confidence": round(confidence, 4),
        "emotional_intensity": round(emotional_intensity, 4),
        "relationship_weight": round(relationship_weight, 4),
        "vulnerability": round(vulnerability, 4),
        "social_cost": round(social_cost, 4),
        "identity_relevance": round(identity_relevance, 4),
        "expectation_violation": round(expectation_violation, 4),
        "schema_activation": round(schema_activation, 4),
        "breakthrough_score": round(breakthrough, 4),
        "importance_score": round(importance, 4),
        "is_turning_point": turning_point,
        "status": status,
        "rejection_reason": rejection_reason,
        "expires_at": raw.get("expires_at"),
    }


async def analyze_messages(
    user_id: str,
    messages: list[dict],
    *,
    schemas: list[dict] | None = None,
    emotional_state: dict | None = None,
    relationship: dict | None = None,
) -> dict[str, Any]:
    """调用幕后模型理解事件；不写数据库，便于独立测试。"""
    schemas = schemas if schemas is not None else get_active_schemas()
    emotional_state = emotional_state if emotional_state is not None else get_effective_emotional_state()
    relationship = relationship if relationship is not None else get_relationship(user_id)
    transcript = "\n".join(
        f"[消息{item['id']}] {'用户' if item['role'] == 'user' else '雪'}：{item['content'][:500]}"
        for item in messages
    )
    schema_context = [
        {
            "id": item["id"],
            "belief": item["belief"],
            "strength": item.get("effective_strength", item["strength"]),
            "sensitivity": item["sensitivity"],
        }
        for item in schemas
    ]
    prompt = f"""你负责判断最近对话中是否发生了对雪具有主观意义的事件。

重要原则：
- 事件重要性不取决于句子长短，而取决于它是否击中旧伤、需要、关系和原有预期。
- 一句简单的话在特殊背景下也可能是心理转折。
- 评分必须基于“雪原本预期什么”与“现在实际发生什么”的差距，而不是句子长度或辞藻。
- 如果雪正处于被否定、孤立或高压力状态，一个重要的人明确承担立场、坚定维护她，通常具有高图式激活、高预期违背和高情绪强度。
- social_cost 衡量对方公开表态、承担冲突或关系风险的程度，不是消息字数。
- 只能引用下面真实存在的消息编号，不得补写没有发生的行为或过去。
- 区分客观事件与雪的主观理解；主观理解不能冒充事实。
- 普通寒暄、礼貌回复和无后续意义的闲聊通常不应成为候选事件。
- 最多返回 {APPRAISAL_MAX_CANDIDATES} 个候选；没有就返回空数组。

雪当前的心理图式：
{json.dumps(schema_context, ensure_ascii=False)}

雪当前的心理状态：
{json.dumps(emotional_state, ensure_ascii=False)}

雪与用户的当前关系：
{json.dumps(relationship, ensure_ascii=False)}

最近对话：
{transcript}

严格返回JSON，不要Markdown：
{{
  "candidates": [
    {{
      "event_type": "user_fact|shared_event|promise|relationship_event|character_event|temporary_context",
      "observed_event": "只写实际发生的事",
      "subjective_meaning": "雪当时可能怎样理解",
      "evidence_message_ids": [1, 2],
      "activated_schemas": [
        {{"schema_id": "已有图式ID", "activation": 0.0, "direction": "reinforce|contradict", "reason": "原因"}}
      ],
      "emotions": [{{"name": "情绪", "intensity": 0.0}}],
      "proposed_belief_changes": [
        {{"schema_id": "已有图式ID", "delta": 0.0, "reason": "只是一项建议，不会直接生效"}}
      ],
      "scores": {{
        "confidence": 0.0,
        "emotional_intensity": 0.0,
        "relationship_weight": 0.0,
        "vulnerability": 0.0,
        "social_cost": 0.0,
        "identity_relevance": 0.0,
        "expectation_violation": 0.0
      }},
      "expires_at": null
    }}
  ]
}}"""
    result = await complete_json(
        prompt,
        system="你是严谨的角色心理事件观察器，只返回有消息证据的结构化判断。",
        max_tokens=1800,
        background=True,
        timeout=60,
    )
    # 某些模型会用 [] 直接表达“没有候选事件”，这与
    # {"candidates": []} 语义相同，不应当记录为调用失败。
    if isinstance(result, list):
        result = {"candidates": result}
    if not isinstance(result, dict):
        raise ValueError("事件评价模型没有返回JSON对象或候选数组")
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("事件评价结果的 candidates 不是数组")
    result["candidates"] = candidates[:APPRAISAL_MAX_CANDIDATES]
    return result


async def maybe_appraise_conversation(user_id: str, *, force: bool = False) -> list[int]:
    """按检查点增量评价；返回本次写入的候选事件ID。"""
    lock = _user_locks.setdefault(str(user_id), asyncio.Lock())
    if lock.locked():
        return []
    async with lock:
        messages = get_recent_messages_with_ids(user_id, APPRAISAL_WINDOW)
        if not messages:
            return []
        checkpoint = get_appraisal_checkpoint(user_id)
        unprocessed = [item for item in messages if item["id"] > checkpoint]
        if not force and len(unprocessed) < max(2, APPRAISAL_EVERY):
            return []

        schemas = get_active_schemas()
        valid_ids = {int(item["id"]) for item in messages}
        existing_events = recent_candidate_events(user_id)
        try:
            result = await analyze_messages(
                user_id,
                messages,
                schemas=schemas,
                emotional_state=get_effective_emotional_state(),
                relationship=get_relationship(user_id),
            )
            candidate_ids = []
            for raw in result.get("candidates", []):
                if not isinstance(raw, dict):
                    continue
                candidate = calculate_candidate(
                    raw,
                    user_id=user_id,
                    valid_message_ids=valid_ids,
                    schemas=schemas,
                    existing_events=existing_events,
                )
                candidate["evidence_snapshot"] = [
                    {
                        "id": int(item["id"]),
                        "role": item["role"],
                        "content": item["content"][:500],
                    }
                    for item in messages
                    if int(item["id"]) in candidate["evidence_message_ids"]
                ]
                candidate_ids.append(save_event_candidate(candidate))
                existing_events.append(candidate["observed_event"])

            save_appraisal_run(user_id, messages, result, len(candidate_ids))
            set_appraisal_checkpoint(user_id, int(messages[-1]["id"]))
            logger.info(
                f"影子评价完成：用户 {user_id}，候选 {len(candidate_ids)} 条，"
                f"不会影响雪当前回复"
            )
            return candidate_ids
        except Exception as exc:
            save_appraisal_run(user_id, messages, {}, 0, str(exc))
            logger.error(f"影子事件评价失败: {exc}")
            return []


def _valid_int_list(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result[:12]


def _normalize_emotions(values: Any) -> list[dict]:
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        name = str(value.get("name", "")).strip()[:40]
        if name:
            result.append({"name": name, "intensity": round(clamp(value.get("intensity", 0)), 4)})
    return result[:6]


def _normalize_belief_changes(values: Any, schema_map: dict[str, dict]) -> list[dict]:
    result = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        schema_id = str(value.get("schema_id", ""))
        if schema_id not in schema_map:
            continue
        proposed = max(-0.25, min(0.25, float(value.get("delta", 0.0))))
        result.append(
            {
                "schema_id": schema_id,
                "delta": round(proposed, 4),
                "reason": str(value.get("reason", ""))[:240],
            }
        )
    return result[:6]


def _is_duplicate(event: str, existing_events: list[str]) -> bool:
    normalized = "".join(event.lower().split())
    if not normalized:
        return False
    for existing in existing_events:
        other = "".join(str(existing).lower().split())
        if other and SequenceMatcher(None, normalized, other).ratio() >= 0.88:
            return True
    return False

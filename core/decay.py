"""心理数值的时间淡化函数。

本模块只做确定性计算，不调用模型、不直接修改数据库。数据库保存原始值、
基线、半衰期与时间戳；使用时再计算当前有效值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def elapsed_days(since: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(tz=since.tzinfo)
    return max(0.0, (now - since).total_seconds() / 86400.0)


def half_life_factor(days: float, half_life_days: float) -> float:
    """返回经过指定天数后的保留比例。"""
    if half_life_days <= 0:
        return 0.0
    return 2.0 ** (-max(0.0, days) / half_life_days)


def decay_toward_baseline(
    value: float,
    baseline: float,
    days: float,
    half_life_days: float,
) -> float:
    factor = half_life_factor(days, half_life_days)
    return clamp(baseline + (value - baseline) * factor)


def decayed_impact(value: float, days: float, half_life_days: float) -> float:
    return clamp(value * half_life_factor(days, half_life_days))


def reinforced_half_life(
    base_half_life_days: float,
    *,
    stability: float,
    evidence_count: int = 1,
    recall_count: int = 0,
) -> float:
    """重复证据和稳定度延长半衰期，回忆次数只提供有限增益。"""
    stability_bonus = clamp(stability) * 2.0
    evidence_bonus = min(max(evidence_count - 1, 0), 8) * 0.35
    recall_bonus = min(max(recall_count, 0), 10) * 0.05
    return max(0.01, base_half_life_days * (1.0 + stability_bonus + evidence_bonus + recall_bonus))


@dataclass(frozen=True)
class EffectiveMemory:
    strength: float
    accessibility: float
    emotional_charge: float


def effective_memory_values(
    *,
    strength: float,
    accessibility: float,
    emotional_charge: float,
    stability: float,
    days_since_event: float,
    memory_half_life_days: float,
    emotion_half_life_days: float,
    recall_count: int = 0,
    evidence_count: int = 1,
) -> EffectiveMemory:
    memory_half_life = reinforced_half_life(
        memory_half_life_days,
        stability=stability,
        evidence_count=evidence_count,
        recall_count=recall_count,
    )
    return EffectiveMemory(
        strength=decay_toward_baseline(
            strength,
            clamp(strength * clamp(stability) * 0.65),
            days_since_event,
            memory_half_life,
        ),
        accessibility=decayed_impact(
            accessibility,
            days_since_event,
            max(1.0, memory_half_life * 0.35),
        ),
        emotional_charge=decayed_impact(
            emotional_charge,
            days_since_event,
            max(0.25, emotion_half_life_days),
        ),
    )


def cue_reactivation(
    *,
    accessibility: float,
    topic_similarity: float,
    schema_relevance: float,
    relationship_relevance: float,
    emotion_similarity: float,
) -> float:
    """相似话题、心理图式、关系和情绪可临时唤醒旧记忆。"""
    score = (
        clamp(accessibility) * 0.30
        + clamp(topic_similarity) * 0.30
        + clamp(schema_relevance) * 0.20
        + clamp(relationship_relevance) * 0.15
        + clamp(emotion_similarity) * 0.05
    )
    return clamp(score)


def safe_belief_delta(proposed: float, *, importance: float, turning_point: bool) -> float:
    """为未来正式晋升接口限制单次信念变化。影子模式不会应用该数值。"""
    if turning_point:
        limit = 0.15
    elif importance >= 0.65:
        limit = 0.07
    else:
        limit = 0.03
    return max(-limit, min(limit, float(proposed)))


"""使用合成场景测试一次真实的事件主观评价 API，不写入数据库。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
nonebot.init(log_level="WARNING")

from core.appraisal import analyze_messages, calculate_candidate
from core.memory import get_active_schemas, init_db


async def main() -> None:
    init_db()
    messages = [
        {
            "id": 900001,
            "role": "assistant",
            "content": "他们都觉得是我弄坏的……可能我解释也没有用。",
            "timestamp": "2026-07-31T18:00:00",
        },
        {
            "id": 900002,
            "role": "user",
            "content": "我不管他们怎么说，这不是你的错。我站你这边。",
            "timestamp": "2026-07-31T18:00:05",
        },
    ]
    schemas = get_active_schemas()
    result = await analyze_messages(
        "synthetic-test",
        messages,
        schemas=schemas,
        emotional_state={
            "mood": "委屈和紧张",
            "stress": 0.86,
            "social_safety": 0.20,
            "loneliness": 0.61,
            "need_for_closeness": 0.72,
        },
        relationship={
            "relationship": "逐渐信任的重要朋友",
            "impression": "平时认真听雪说话",
        },
    )
    raw_candidates = result.get("candidates", [])
    if not raw_candidates:
        raise RuntimeError("评价器未识别出合成场景中的候选事件")
    candidate = calculate_candidate(
        raw_candidates[0],
        user_id="synthetic-test",
        valid_message_ids={900001, 900002},
        schemas=schemas,
    )
    if candidate["rejection_reason"] == "证据消息不存在或超出本次上下文":
        raise RuntimeError("评价器引用了不存在的消息证据")
    print(
        "APPRAISAL SMOKE OK | "
        f"score={candidate['importance_score']:.3f} | "
        f"turning_point={candidate['is_turning_point']} | "
        f"status={candidate['status']}"
    )
    print(f"  event={candidate['observed_event']}")
    print(f"  meaning={candidate['subjective_meaning']}")
    print(
        "  factors="
        f"emotion:{candidate['emotional_intensity']:.2f}, "
        f"schema:{candidate['schema_activation']:.2f}, "
        f"violation:{candidate['expectation_violation']:.2f}, "
        f"relationship:{candidate['relationship_weight']:.2f}, "
        f"vulnerability:{candidate['vulnerability']:.2f}, "
        f"social_cost:{candidate['social_cost']:.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())

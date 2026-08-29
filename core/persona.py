"""角色设定、关系档案与提示词组装。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot import get_driver
from nonebot.log import logger

from core.llm import complete_json
from core.world import build_world_prompt

ROOT_DIR = Path(__file__).resolve().parent.parent
PERSONA_FILE = ROOT_DIR / "config" / "persona.json"
RELATIONSHIPS_FILE = ROOT_DIR / "data" / "relationships.json"

config = get_driver().config
UPDATE_EVERY = int(getattr(config, "character_update_every", 10))
MEMORY_LIMIT = int(getattr(config, "persona_memory_limit", 4))
_file_lock = threading.RLock()
_relationship_update_lock = asyncio.Lock()


def load_persona() -> dict[str, Any]:
    try:
        return json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"读取角色设定失败: {exc}")
        return {}


def _load_relationship_data() -> dict[str, Any]:
    try:
        data = json.loads(RELATIONSHIPS_FILE.read_text(encoding="utf-8"))
        data.setdefault("relationships", {})
        return data
    except FileNotFoundError:
        return {"version": 2, "relationships": {}}
    except Exception as exc:
        logger.error(f"读取关系档案失败: {exc}")
        return {"version": 2, "relationships": {}}


def _save_relationship_data(data: dict[str, Any]) -> None:
    RELATIONSHIPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    with _file_lock:
        fd, temp_name = tempfile.mkstemp(
            dir=RELATIONSHIPS_FILE.parent,
            prefix="relationships-",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(serialized)
            os.replace(temp_name, RELATIONSHIPS_FILE)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def get_relationship(user_id: str) -> dict[str, Any]:
    data = _load_relationship_data()
    return data["relationships"].get(
        str(user_id),
        {
            "nickname": "",
            "relationship": "陌生人",
            "impression": "",
            "known_facts": [],
            "special_memories": [],
            "last_updated": "",
        },
    )


def _query_terms(query: str) -> set[str]:
    cleaned = re.sub(r"\s+", "", query.lower())
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", cleaned))
    for term in list(terms):
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            terms.update(term[index : index + 2] for index in range(len(term) - 1))
    return terms


def select_canon_memories(persona: dict[str, Any], query: str, limit: int = MEMORY_LIMIT) -> list[dict]:
    memories = persona.get("canon_memories", [])
    if not memories:
        return []
    query_terms = _query_terms(query)
    ranked: list[tuple[int, int, dict]] = []
    for index, memory in enumerate(memories):
        searchable = " ".join(
            [
                str(memory.get("event", "")),
                str(memory.get("impact", "")),
                " ".join(memory.get("tags", [])),
            ]
        ).lower()
        score = sum(3 if term in memory.get("tags", []) else 1 for term in query_terms if term in searchable)
        ranked.append((score, -index, memory))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    relevant = [item[2] for item in ranked if item[0] > 0]
    return relevant[:limit]


def build_persona_prompt(user_id: str = "", query: str = "") -> str:
    persona = load_persona()
    if not persona:
        return ""

    identity = persona.get("identity", {})
    core = persona.get("core_personality", {})
    speech = persona.get("speech_style", {})
    boundaries = persona.get("boundaries", [])
    memories = select_canon_memories(persona, query)

    world_prompt = build_world_prompt()
    lines = [
        world_prompt,
        "【角色核心设定（事实层，不得被临时对话改写）】",
        f"你叫{identity.get('name', '雪')}，{identity.get('species', '')}，实际年龄{identity.get('actual_age', 16)}岁，{identity.get('legal_status', '')}。",
        f"外貌概括：{persona.get('appearance_summary', '')}",
        f"人生背景：{persona.get('background', '')}",
        f"稳定性格：{persona.get('personality', '')}",
        f"核心信念：{'；'.join(core.get('beliefs', []))}",
        f"内在需要：{'；'.join(core.get('needs', []))}",
        f"害怕的事：{'；'.join(core.get('fears', []))}",
        f"性格矛盾：{'；'.join(core.get('contradictions', []))}",
        f"情绪与应对：{'；'.join(core.get('coping_patterns', []))}",
        f"说话方式：{speech.get('description', '')}",
        f"表达约束：{'；'.join(speech.get('rules', []))}",
        f"喜好：{'、'.join(persona.get('likes', []))}",
        f"反感：{'、'.join(persona.get('dislikes', []))}",
        f"习惯：{'；'.join(persona.get('habits', []))}",
        f"边界：{'；'.join(boundaries)}",
    ]

    if memories:
        lines.append("【与当前话题最相关的既有经历】")
        for memory in memories:
            impact = f"；留下的影响：{memory['impact']}" if memory.get("impact") else ""
            lines.append(f"- {memory.get('event', '')}{impact}")

    if user_id:
        relationship = get_relationship(user_id)
        lines.append("【与正在聊天的这个人的关系】")
        if relationship.get("nickname"):
            lines.append(f"你叫对方：{relationship['nickname']}")
        lines.append(f"关系：{relationship.get('relationship', '陌生人')}")
        if relationship.get("impression"):
            lines.append(f"目前印象：{relationship['impression']}")
        if relationship.get("known_facts"):
            lines.append(f"确认过的近期事实：{'；'.join(relationship['known_facts'][-8:])}")
        if relationship.get("special_memories"):
            lines.append(f"较重要的共同经历：{'；'.join(relationship['special_memories'][-6:])}")

    lines.extend(
        [
            "【一致性规则】",
            "只把上面的核心设定与明确记录当成事实；没有记录的过去不要编造成确定事实。",
            "新经历可以让表达和关系缓慢变化，但不能突然反转核心性格。",
            "像真实的人一样允许犹豫、误解、情绪变化和不知道，不要为了讨好而永远顺从。",
        ]
    )
    return "\n".join(line for line in lines if line and not line.endswith("："))


async def maybe_update_relationship(user_id: str, message_count: int) -> None:
    if UPDATE_EVERY <= 0 or message_count == 0 or message_count % UPDATE_EVERY != 0:
        return

    from core.memory import get_recent_messages

    recent = get_recent_messages(user_id, limit=UPDATE_EVERY)
    if not recent:
        return
    dialog = "\n".join(
        f"{'用户' if item['role'] == 'user' else '雪'}：{item['content'][:160]}"
        for item in recent
    )
    current = get_relationship(user_id)
    prompt = f"""根据最近对话更新雪对这个人的关系档案。

最近对话：
{dialog}

当前档案：
{json.dumps(current, ensure_ascii=False)}

规则：
- 只记录用户明确说过或双方确实发生的事，不把猜测写成事实。
- 不要删除原有正确事实；不要换一种说法重复同一件事。
- impression 和 relationship 只能渐进变化。
- 没有值得长期记录的新信息时 has_update=false。

严格返回 JSON：
{{"has_update": false, "nickname": "", "relationship": "", "impression": "", "new_facts": [], "new_memories": []}}"""

    try:
        result = await complete_json(prompt, max_tokens=400, background=True, timeout=35)
        if not isinstance(result, dict) or not result.get("has_update"):
            return
        async with _relationship_update_lock:
            data = _load_relationship_data()
            existing = data["relationships"].get(str(user_id), current)
            facts = _ordered_unique(existing.get("known_facts", []) + result.get("new_facts", []))[-20:]
            memories = _ordered_unique(
                existing.get("special_memories", []) + result.get("new_memories", [])
            )[-10:]
            data["relationships"][str(user_id)] = {
                "nickname": result.get("nickname") or existing.get("nickname", ""),
                "relationship": result.get("relationship") or existing.get("relationship", "陌生人"),
                "impression": result.get("impression") or existing.get("impression", ""),
                "known_facts": facts,
                "special_memories": memories,
                "last_updated": datetime.now().isoformat(),
            }
            _save_relationship_data(data)
        logger.info(f"用户 {user_id} 的关系档案已更新")
    except Exception as exc:
        logger.error(f"更新用户关系档案失败: {exc}")


def _ordered_unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result

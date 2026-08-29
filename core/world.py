"""读取并压缩注入世界观。完整世界规则只保存在 config/world.json。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nonebot.log import logger

ROOT_DIR = Path(__file__).resolve().parent.parent
WORLD_FILE = ROOT_DIR / "config" / "world.json"


def load_world() -> dict[str, Any]:
    try:
        return json.loads(WORLD_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"读取世界观失败: {exc}")
        return {}


def build_world_prompt() -> str:
    """只注入会影响当前回答的硬规则，详细资料按需由未来检索器读取。"""
    world = load_world()
    if not world:
        return ""
    traits = world.get("animal_trait_people", {})
    law = world.get("law_and_adulthood", {})
    education = world.get("education", {})
    employment = world.get("employment_and_economy", {})
    geography = world.get("geography", {})
    current = world.get("xue_current_life", {})
    family = world.get("family_context", {})
    hometown = geography.get("hometown", {})
    city = geography.get("current_city", {})
    return "\n".join(
        [
            "【世界硬规则】",
            world.get("premise", ""),
            f"兽征者：{traits.get('origin', '')}",
            f"法律身份：{traits.get('legal_status', {}).get('citizenship', '')}。",
            f"社会现实：{traits.get('social_attitudes', {}).get('mainstream', '')}；"
            f"{traits.get('social_attitudes', {}).get('negative', '')}。",
            f"成年规则：{law.get('legal_adulthood_age', 16)}岁成年，可以独立居住、租房和正常工作。",
            f"雪的学业：{education.get('xue_status', '')}",
            f"雪的工作：{employment.get('xue_current_status', '')}",
            f"地点：雪来自{hometown.get('name', '栖河镇')}，现在住在{city.get('name', '临川市')}"
            f"{current.get('housing', {}).get('district', '')}的一间小开间。",
            f"当前处境：{current.get('timeline_position', '')}；积蓄正在减少，但具体余额不是固定事实。",
            f"家庭：{family.get('parents', '')}。{family.get('emotional_home', '')}",
            "除非设定明确写明，否则不要临时创造魔法、超能力、特殊法律或新的固定亲属。",
        ]
    )


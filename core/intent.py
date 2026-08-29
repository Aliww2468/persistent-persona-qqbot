"""轻量的结构化意图识别。"""

from __future__ import annotations

from datetime import datetime

from nonebot.log import logger

from core.llm import complete_json

INTENT_SYSTEM_PROMPT = """你是意图识别器。只判断最新用户消息是否包含提醒或结束对话意图。
严格返回 JSON，不要 Markdown：
提醒：{"intent":"remind","time":"HH:MM","content":"提醒内容"}
结束：{"intent":"end_chat","type":"sleep或leave"}
其他：{"intent":"none"}
相对时间要依据提供的当前时间换算；不确定时返回 none。"""


async def detect_intent(user_text: str, recent_context: str = "") -> dict:
    now = datetime.now()
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    prompt = (
        f"当前时间：{now:%Y-%m-%d %H:%M}（{weekday}）\n"
        f"最近语境：{recent_context or '无'}\n"
        f"最新消息：{user_text}"
    )
    try:
        result = await complete_json(
            prompt,
            system=INTENT_SYSTEM_PROMPT,
            max_tokens=180,
            background=True,
            timeout=20,
        )
        if not isinstance(result, dict) or result.get("intent") not in {"remind", "end_chat", "none"}:
            return {"intent": "none"}
        return result
    except Exception as exc:
        logger.error(f"意图识别失败: {exc}")
        return {"intent": "none"}


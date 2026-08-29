"""QQ 聊天入口：收消息、组装上下文、并行回复与意图识别。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from core.intent import detect_intent
from core.life_state import get_current_state
from core.llm import chat
from core.memory import (
    get_message_count,
    get_recent_messages,
    get_summary,
    maybe_compress,
    save_message,
)
from core.persona import build_persona_prompt, maybe_update_relationship

config = get_driver().config
BOT_SYSTEM_PROMPT = str(getattr(config, "bot_system_prompt", "你是一个友好的 AI 助手。"))
GROUP_CHAT_ENABLED = bool(getattr(config, "group_chat_enabled", False))
GROUP_AT_ONLY = bool(getattr(config, "group_at_only", True))
MAX_TOKENS = int(getattr(config, "max_tokens", 1024))
COOLDOWN_SECONDS = int(getattr(config, "cooldown_seconds", 2))

cooldown: dict[str, float] = {}
background_tasks: set[asyncio.Task] = set()


async def _is_group_message(event: MessageEvent) -> bool:
    return (
        GROUP_CHAT_ENABLED
        and isinstance(event, GroupMessageEvent)
        and (not GROUP_AT_ONLY or event.is_tome())
    )


async def _is_private_message(event: MessageEvent) -> bool:
    return isinstance(event, PrivateMessageEvent)


group_chat = on_message(rule=Rule(_is_group_message), priority=5, block=True)
private_chat = on_message(rule=Rule(_is_private_message), priority=5, block=True)

if GROUP_CHAT_ENABLED:
    reply_scope = "仅响应 @机器人的消息" if GROUP_AT_ONLY else "响应所有普通群消息"
    logger.info(f"群聊回复已开启：{reply_scope}")
else:
    logger.info("群聊回复已关闭")


def build_system_prompt(user_id: str, user_text: str, summary: str = "") -> str:
    persona_prompt = build_persona_prompt(user_id, query=user_text)
    from core.life_engine import get_recent_life_context

    life_context = get_recent_life_context()
    now = datetime.now()
    sections = [BOT_SYSTEM_PROMPT, persona_prompt, life_context]
    sections.append(
        "\n".join(
            [
                f"【当前真实时间】{now:%Y年%m月%d日 %H:%M}",
                f"【当前生活状态】{get_current_state(now)}",
                "回复要和真实时间、当前状态一致；不合理的邀约可以像真人一样指出来。",
            ]
        )
    )
    if summary:
        sections.append(
            "【与这个人的长期对话摘要】\n"
            f"{summary}\n"
            "摘要可能不完整；不要把摘要中没有的猜测补成事实。"
        )
    return "\n\n".join(section for section in sections if section)


async def call_ai(messages: list[dict], system: str | None = None) -> str:
    """保留原公开入口，实际调用统一核心客户端。"""
    return await chat(messages, system=system or BOT_SYSTEM_PROMPT, max_tokens=MAX_TOKENS)


async def _handle(bot: Bot, event: MessageEvent) -> None:
    user_id = str(event.user_id)
    now_timestamp = time.time()
    if now_timestamp - cooldown.get(user_id, 0) < COOLDOWN_SECONDS:
        return
    cooldown[user_id] = now_timestamp

    user_text = event.get_plaintext().strip()
    if not user_text:
        return

    from plugins.scheduler import schedule_followup, update_last_active

    update_last_active(user_id)
    summary = get_summary(user_id)
    recent = get_recent_messages(user_id)
    messages = recent + [{"role": "user", "content": user_text}]
    system = build_system_prompt(user_id, user_text, summary)
    save_message(user_id, "user", user_text)

    context = "\n".join(
        f"{'用户' if item['role'] == 'user' else '雪'}：{item['content'][:80]}"
        for item in recent[-2:]
    )
    reply_result, intent_result = await asyncio.gather(
        call_ai(messages, system),
        detect_intent(user_text, context),
        return_exceptions=True,
    )

    if isinstance(reply_result, Exception):
        logger.error(f"主模型回复失败: {type(reply_result).__name__}: {reply_result}")
        # 不把接口错误当作雪说过的话，也不据此更新关系、形成记忆或安排跟进。
        await bot.send(event, "我这边刚刚断线了……等一下再试试。")
        return
    else:
        reply = reply_result

    intent = intent_result if isinstance(intent_result, dict) else {"intent": "none"}
    chat_ended = _execute_intent(user_id, intent)

    save_message(user_id, "assistant", reply)
    if not chat_ended:
        schedule_followup(user_id)
    await bot.send(event, reply)
    _spawn_background(_post_chat_maintenance(user_id, force_appraisal=chat_ended))


async def _post_chat_maintenance(user_id: str, *, force_appraisal: bool) -> None:
    """回复发送后的后台维护，不增加用户等待时间。"""
    from core.appraisal import maybe_appraise_conversation

    # 先评价再压缩，确保评价器还能引用真实消息ID作为证据。
    await maybe_appraise_conversation(user_id, force=force_appraisal)
    await maybe_update_relationship(user_id, get_message_count(user_id))
    await maybe_compress(user_id)


def _spawn_background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    task.add_done_callback(_log_background_error)


def _log_background_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error:
        logger.error(f"聊天后台维护失败: {type(error).__name__}: {error}")


def _execute_intent(user_id: str, intent: dict) -> bool:
    intent_type = intent.get("intent", "none")
    if intent_type == "remind":
        time_text = str(intent.get("time", ""))
        content = str(intent.get("content", ""))
        if time_text and content:
            from plugins.scheduler import add_reminder

            add_reminder(user_id, time_text, content)
        return False
    if intent_type == "end_chat":
        from plugins.scheduler import cancel_followup, schedule_wakeup

        cancel_followup(user_id)
        if intent.get("type") == "sleep":
            schedule_wakeup(user_id)
        return True
    return False


@group_chat.handle()
async def handle_group(bot: Bot, event: GroupMessageEvent) -> None:
    await _handle(bot, event)


@private_chat.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent) -> None:
    await _handle(bot, event)

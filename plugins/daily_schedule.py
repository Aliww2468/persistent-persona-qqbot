"""
每日日程系统 - 让雪真正"活着"

工作流程：
1. 每天 00:05 AI 根据星期、性格、昨日聊天内容自动生成今天的日程
2. 每个日程节点到时间触发"内心独白"
3. AI 根据距离上次聊天时间、当前事件，决定要不要发消息
4. 睡前节点自动安排明早睡醒消息
5. core.life_state 从当前日程节点提供唯一的生活状态

所有幕后调用统一走 core.llm，可配置独立副模型，也可回退到主模型。
"""

import os
import random
from datetime import datetime

from nonebot import get_bot, get_driver
from nonebot.log import logger

from core.life_state import get_current_state, load_schedule, save_schedule
from core.llm import complete, complete_json
from core.memory import get_recent_messages
from core.persona import build_persona_prompt, load_persona

driver = get_driver()
config = driver.config

# 距离上次聊天的时间 → 主动发消息的概率
CONTACT_PROBABILITY = [
    (1,   0.10),   # < 1小时：10%
    (3,   0.50),   # 1-3小时：50%
    (6,   0.80),   # 3-6小时：80%
    (999, 0.95),   # > 6小时：95%
]

# 睡前节点关键词（触发必发 + 安排睡醒消息）
SLEEP_EVENT_KEYWORDS = ["睡觉", "准备睡", "要睡了", "睡前", "晚安"]


def get_current_event() -> str:
    """兼容旧调用；当前状态现在由 core.life_state 统一提供。"""
    return get_current_state()


# ── 日程生成 ──────────────────────────────────────────────

async def generate_daily_schedule() -> None:
    """每天凌晨生成今天的日程（走副模型）"""
    profile = load_persona()
    now = datetime.now()
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    is_weekend = now.weekday() >= 5

    targets = _get_all_targets()
    recent_chats = ""
    for uid in targets:
        msgs = get_recent_messages(uid, limit=6)
        if msgs:
            recent_chats += f"\n用户{uid}的最近对话：\n"
            recent_chats += "\n".join(
                f"  {'用户' if m['role'] == 'user' else '雪'}：{m['content'][:60]}"
                for m in msgs[-4:]
            )

    likes = ", ".join(profile.get("likes", []))
    personality = profile.get("personality", "")

    prompt = f"""今天是{weekday}，{'周末' if is_weekend else '工作日'}。
雪的性格：{personality}
雪喜欢：{likes}
{f'昨天和朋友的聊天内容：{recent_chats}' if recent_chats else ''}

请为雪生成今天一整天真实的生活日程，要求：
- {'周末可以睡到9-10点' if is_weekend else '平时8点左右起床'}
- 包含8-12个节点，覆盖起床到睡觉
- 事件要具体生动，体现她的性格和喜好
- 偶尔安排和昨天聊天内容相关的事（比如昨天聊到游戏今天就去玩）
- 睡觉时间安排在22:30-23:59之间
- 时间格式严格用 HH:MM

必须严格返回 JSON 数组，不要任何其他内容：
[
  {{"time": "08:30", "event": "起床，头发乱乱的，在喝热牛奶"}},
  {{"time": "10:00", "event": "玩解谜游戏，第三关卡住了有点烦"}},
  ...
  {{"time": "23:30", "event": "准备睡觉，换上睡衣，有点困"}}
]"""

    try:
        schedule = await complete_json(prompt, max_tokens=700, background=True, timeout=60)
        if not isinstance(schedule, list) or len(schedule) < 4:
            raise ValueError("模型返回的日程不是有效数组或节点太少")
        save_schedule(schedule)
        logger.info(f"今日日程已生成，共 {len(schedule)} 个节点")
        for item in schedule:
            logger.info(f"  {item['time']} - {item['event']}")

        await register_today_triggers()

    except Exception as e:
        logger.error(f"日程生成失败: {e}")
        _save_default_schedule()
        await register_today_triggers()


def _save_default_schedule() -> None:
    """生成失败时的备用默认日程"""
    now = datetime.now()
    is_weekend = now.weekday() >= 5
    default = [
        {"time": "08:30" if not is_weekend else "10:00", "event": "刚起床，头发乱乱的，在喝热牛奶"},
        {"time": "10:30", "event": "在玩手机，刷到了有趣的东西"},
        {"time": "12:00", "event": "吃午饭，今天想吃点甜的"},
        {"time": "14:00", "event": "午睡，趴在沙发上"},
        {"time": "16:00", "event": "窗边晒太阳，懒洋洋地发呆"},
        {"time": "18:30", "event": "吃晚饭，边吃边刷手机"},
        {"time": "20:00", "event": "玩游戏，玩得有点入迷"},
        {"time": "22:00", "event": "洗澡，洗完头发还没干"},
        {"time": "23:30", "event": "准备睡觉，换上睡衣，有点困"},
    ]
    save_schedule(default)
    logger.info("已使用默认日程")


async def register_today_triggers() -> None:
    """为今天剩余的日程节点注册定时触发器"""
    from plugins.scheduler import scheduler

    schedule = load_schedule()
    now = datetime.now()
    today = now.date()
    registered = 0

    for item in schedule:
        try:
            h, m = item["time"].split(":")
            run_time = datetime(today.year, today.month, today.day, int(h), int(m))

            if run_time <= now:
                continue

            event = item["event"]
            job_id = f"schedule_event_{h}_{m}"

            scheduler.add_job(
                trigger_event,
                "date",
                run_date=run_time,
                args=[event],
                id=job_id,
                replace_existing=True,
            )
            registered += 1
        except Exception as e:
            logger.warning(f"注册节点失败 {item}: {e}")

    logger.info(f"已注册今日剩余 {registered} 个日程触发器")


# ── 事件触发 ──────────────────────────────────────────────

async def trigger_event(event: str) -> None:
    """日程节点到时间时触发，决定要不要给每个用户发消息"""
    logger.info(f"日程触发：{event}")

    targets = _get_all_targets()
    for user_id in targets:
        await _think_about_user(user_id, event)


async def _think_about_user(user_id: str, event: str) -> None:
    """针对某个用户，决定要不要发消息"""
    from plugins.scheduler import last_active

    last = last_active.get(user_id)
    if last:
        hours_since = (datetime.now() - last).total_seconds() / 3600
    else:
        hours_since = 999

    is_sleep_event = any(kw in event for kw in SLEEP_EVENT_KEYWORDS)

    if is_sleep_event:
        probability = 1.0
    else:
        probability = 0.10
        for hours_threshold, prob in CONTACT_PROBABILITY:
            if hours_since < hours_threshold:
                probability = prob
                break

    if random.random() > probability:
        logger.info(f"用户 {user_id} 本次跳过（概率 {probability:.0%}，距上次 {hours_since:.1f}h）")
        return

    logger.info(f"用户 {user_id} 触发内心独白（概率 {probability:.0%}，距上次 {hours_since:.1f}h）")
    await _generate_and_send(user_id, event, hours_since, is_sleep_event)


async def _generate_and_send(user_id: str, event: str, hours_since: float, is_sleep: bool) -> None:
    """生成并发送消息（走副模型）"""
    from plugins.scheduler import schedule_wakeup

    recent = get_recent_messages(user_id, limit=6)
    dialog = ""
    if recent:
        dialog = "\n".join(
            f"{'用户' if m['role'] == 'user' else '雪'}：{m['content'][:60]}"
            for m in recent
        )

    now = datetime.now()

    if is_sleep:
        prompt = f"""现在是{now.hour}点{now.minute}分，你准备睡觉了。
你现在的状态：{event}
{f'和这个人上次聊天距今 {hours_since:.0f} 小时了。' if hours_since < 999 else '好久没联系这个人了。'}
{f'最近的对话内容：{dialog}' if dialog else ''}

睡前想到了他，发一条消息，要：
- 有睡前慵懒、困意的感觉
- 自然地表达想到他了或者睡前想说的话
- 结尾暗示要睡了
- 50字以内，直接输出消息内容"""
    else:
        hours_desc = f"{hours_since:.0f}小时" if hours_since < 999 else "很久"
        prompt = f"""现在是{now.hour}点{now.minute}分，你正在做的事：{event}
距离上次和这个人聊天已经过了{hours_desc}。
{f'最近的对话内容：{dialog}' if dialog else ''}

你在做这件事的时候，突然想到了他，想发条消息。
要求：
- 结合你正在做的事，自然地想到他
- 不要太刻意，就像真的突然想到
- 语气符合你的性格
- 50字以内，直接输出消息内容"""

    try:
        system = build_persona_prompt(user_id, query=event)
        msg = await complete(
            prompt,
            system=system,
            max_tokens=300,
            background=True,
            timeout=45,
        )
        bot = get_bot()
        await bot.send_private_msg(user_id=int(user_id), message=msg)
        logger.info(f"日程消息已发送给 {user_id}：{msg[:30]}...")

        if is_sleep:
            schedule_wakeup(user_id)

    except Exception as e:
        logger.error(f"日程消息发送失败: {e}")


# ── 工具函数 ──────────────────────────────────────────────

def _get_all_targets() -> list[str]:
    """从 scheduler_jobs.txt 读取所有目标 QQ 号"""
    targets = []
    file_path = os.path.join(os.path.dirname(__file__), "..", "scheduler_jobs.txt")
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if parts:
                    targets.append(parts[0].strip())
        return list(set(targets))
    except Exception:
        return []

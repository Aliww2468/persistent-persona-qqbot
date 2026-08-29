"""
定时主动发消息插件
- 定时任务配置在 scheduler_jobs.txt
- 随机主动消息由 daily_schedule.py 的日程系统接管
- 保留：跟进消息、睡醒消息、提醒、AI 行动指令
"""

import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nonebot import get_bot, get_driver
from nonebot.log import logger

from core.life_state import get_current_state, load_schedule
from core.llm import complete
from core.memory import get_recent_messages
from core.persona import build_persona_prompt

driver = get_driver()
config = driver.config

AWAY_TIMEOUT_MINUTES = int(getattr(config, "away_timeout_minutes", 30))
FOLLOWUP_MIN_MINUTES = int(getattr(config, "followup_min_minutes", 20))
FOLLOWUP_MAX_MINUTES = int(getattr(config, "followup_max_minutes", 60))
LIFE_ENGINE_ENABLED = bool(getattr(config, "life_engine_enabled", True))

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

last_active: dict[str, datetime] = {}
pending_followup: dict[str, bool] = {}


def get_time_period(hour: int) -> str:
    if 0 <= hour < 6:
        return "深夜/凌晨，正常人应该在睡觉"
    elif 6 <= hour < 9:
        return "清晨，刚起床的时间"
    elif 9 <= hour < 12:
        return "上午，工作学习时间"
    elif 12 <= hour < 14:
        return "中午，午饭午休时间"
    elif 14 <= hour < 17:
        return "下午"
    elif 17 <= hour < 19:
        return "傍晚，下班放学时间"
    elif 19 <= hour < 22:
        return "晚上，休闲时间"
    else:
        return "深夜，准备睡觉的时间"


def parse_jobs() -> list[dict]:
    import os
    jobs = []
    file_path = os.path.join(os.path.dirname(__file__), "..", "scheduler_jobs.txt")
    if not os.path.exists(file_path):
        logger.warning("未找到 scheduler_jobs.txt")
        return jobs
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) != 3:
                logger.warning(f"格式错误，跳过：{line}")
                continue
            qq, time_str, prompt = parts[0].strip(), parts[1].strip(), parts[2].strip()
            hour, minute = time_str.split(":")
            jobs.append({
                "qq": qq,
                "hour": int(hour),
                "minute": int(minute),
                "prompt": prompt,
                "time_str": time_str,
            })
    return jobs


async def _call_ai_simple(prompt: str, user_id: str = None, max_tokens: int = 150) -> str:
    """幕后消息入口，复用统一客户端和统一人格提示词。"""
    from core.life_engine import get_recent_life_context

    system = "\n\n".join(
        item
        for item in (
            build_persona_prompt(user_id or "", query=prompt),
            get_recent_life_context(),
        )
        if item
    )
    return await complete(
        prompt,
        system=system,
        max_tokens=max_tokens,
        background=True,
        timeout=60,
    )


# ── 定时消息 ──────────────────────────────────────────────
async def send_msg(qq: str, prompt: str) -> None:
    try:
        bot = get_bot()
        msg = await _call_ai_simple(prompt, user_id=qq)
        await bot.send_private_msg(user_id=int(qq), message=msg)
        logger.info(f"定时消息已发送给 {qq}")
    except Exception as e:
        logger.error(f"发送给 {qq} 失败: {e}")


@driver.on_startup
async def start_scheduler() -> None:
    jobs = parse_jobs()
    if not jobs:
        logger.info("没有配置定时任务")
    else:
        for i, job in enumerate(jobs):
            qq, prompt = job["qq"], job["prompt"]
            scheduler.add_job(
                send_msg,
                "cron",
                hour=job["hour"],
                minute=job["minute"],
                second=(i * 10) % 60,
                args=[qq, prompt],
            )
            logger.info(f"已注册定时任务：每天 {job['time_str']} 发送给 {qq}")

    if not scheduler.running:
        scheduler.start()

    if LIFE_ENGINE_ENABLED:
        logger.info("自主生活已启用：不再生成强制日程表")
    else:
        # 关闭自主生活时完整保留旧日程系统作为回退。
        scheduler.add_job(
            _generate_schedule_job,
            "cron",
            hour=0,
            minute=5,
            id="daily_schedule_gen",
            replace_existing=True,
        )
        logger.info("已注册每日日程生成任务（每天 00:05）")
        from plugins.daily_schedule import generate_daily_schedule, register_today_triggers
        schedule = load_schedule()
        if schedule:
            logger.info(f"发现今日已有日程（{len(schedule)} 个节点），直接注册触发器")
            await register_today_triggers()
        else:
            logger.info("今日日程为空，已安排后台生成；机器人启动不会等待模型")
            scheduler.add_job(
                generate_daily_schedule,
                "date",
                run_date=datetime.now() + timedelta(seconds=1),
                id="startup_schedule_gen",
                replace_existing=True,
            )


async def _generate_schedule_job() -> None:
    """每天 00:05 触发日程生成"""
    from plugins.daily_schedule import generate_daily_schedule
    await generate_daily_schedule()


# ── 离开后跟进消息 ────────────────────────────────────────
def update_last_active(user_id: str) -> None:
    last_active[user_id] = datetime.now()
    pending_followup[user_id] = False
    job_id = f"followup_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def schedule_followup(user_id: str) -> None:
    if pending_followup.get(user_id):
        return
    pending_followup[user_id] = True

    delay = random.randint(FOLLOWUP_MIN_MINUTES, FOLLOWUP_MAX_MINUTES)
    run_time = datetime.now() + timedelta(minutes=delay)

    scheduler.add_job(
        send_followup_message,
        "date",
        run_date=run_time,
        args=[user_id],
        id=f"followup_{user_id}",
        replace_existing=True,
    )
    logger.info(f"已安排跟进消息：{delay} 分钟后发送给 {user_id}")


async def send_followup_message(user_id: str) -> None:
    last = last_active.get(user_id)
    if last and (datetime.now() - last).total_seconds() < AWAY_TIMEOUT_MINUTES * 60:
        logger.info(f"用户 {user_id} 已回来，取消跟进消息")
        return

    pending_followup[user_id] = False

    recent = get_recent_messages(user_id, limit=6)
    if not recent:
        return

    dialog = "\n".join(
        f"{'用户' if m['role'] == 'user' else '雪'}：{m['content'][:50]}"
        for m in recent
    )

    state = get_current_state()
    now = datetime.now()
    period = get_time_period(now.hour)
    away_minutes = int((datetime.now() - last).total_seconds() / 60) if last else FOLLOWUP_MIN_MINUTES

    prompt = f"""你们刚才聊天，然后用户离开了，已经过去了约 {away_minutes} 分钟。
现在是{now.hour}点{now.minute}分（{period}），你的状态是：{state}

刚才的对话内容：
{dialog}

请根据以上内容，发一条自然的消息给用户，就像突然想到刚才聊的事情想继续说，或者想知道他去哪了。
要求：
- 结合刚才聊的内容，不要太生硬
- 必须符合当前真实时间
- 自然真实，像真的想念对方
- 50字以内，直接输出消息内容"""

    try:
        msg = await _call_ai_simple(prompt, user_id=user_id, max_tokens=300)
        bot = get_bot()
        await bot.send_private_msg(user_id=int(user_id), message=msg)
        logger.info(f"跟进消息已发送给 {user_id}：{msg[:20]}...")
    except Exception as e:
        logger.error(f"跟进消息发送失败: {e}")


# ── 睡醒消息 ──────────────────────────────────────────────
def schedule_wakeup(user_id: str) -> None:
    """根据当前时间决定睡醒时间，安排睡醒消息"""
    now = datetime.now()
    hour = now.hour

    if 0 <= hour < 4:
        sleep_hours = random.randint(7, 9)
    elif 4 <= hour < 7:
        sleep_hours = random.randint(6, 8)
    elif 7 <= hour < 12:
        sleep_hours = random.randint(5, 7)
    elif 12 <= hour < 15:
        sleep_hours = random.randint(1, 2)
    else:
        sleep_hours = random.randint(7, 9)

    sleep_minutes = sleep_hours * 60 + random.randint(-30, 30)
    wakeup_time = now + timedelta(minutes=sleep_minutes)

    scheduler.add_job(
        send_wakeup_message,
        "date",
        run_date=wakeup_time,
        args=[user_id],
        id=f"wakeup_{user_id}",
        replace_existing=True,
    )
    logger.info(f"已安排睡醒消息：预计 {wakeup_time.strftime('%H:%M')} 发送给 {user_id}")


async def send_wakeup_message(user_id: str) -> None:
    recent = get_recent_messages(user_id, limit=4)
    dialog = "\n".join(
        f"{'用户' if m['role'] == 'user' else '雪'}：{m['content'][:50]}"
        for m in recent
    ) if recent else ""

    now = datetime.now()

    prompt = f"""你刚刚睡醒，现在是{now.hour}点{now.minute}分。
睡前和这个人道了晚安，刚醒来迷迷糊糊的，突然想到他了。
{f'睡前的对话：{dialog}' if dialog else ''}

请发一条刚睡醒想到他的消息，要：
- 有睡醒的慵懒感，迷迷糊糊的
- 自然地提到想到他了或者想起睡前聊的事
- 50字以内，直接输出消息内容"""

    try:
        msg = await _call_ai_simple(prompt, user_id=user_id, max_tokens=300)
        bot = get_bot()
        await bot.send_private_msg(user_id=int(user_id), message=msg)
        logger.info(f"睡醒消息已发送给 {user_id}：{msg[:20]}...")

        # 睡醒后立刻重新注册今天剩余的日程触发器
        from plugins.daily_schedule import register_today_triggers
        await register_today_triggers()

    except Exception as e:
        logger.error(f"睡醒消息发送失败: {e}")


# ── AI 行动指令 ──────────────────────────────────────────
def cancel_followup(user_id: str) -> None:
    pending_followup[user_id] = False
    job_id = f"followup_{user_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"已取消用户 {user_id} 的跟进消息（对话已结束）")


def add_reminder(user_id: str, time_str: str, content: str) -> bool:
    try:
        hour, minute = time_str.split(":")
        hour, minute = int(hour), int(minute)

        now = datetime.now()
        run_time = datetime(now.year, now.month, now.day, hour, minute)
        if run_time <= now:
            run_time += timedelta(days=1)

        scheduler.add_job(
            send_reminder,
            "date",
            run_date=run_time,
            args=[user_id, content],
            id=f"reminder_{user_id}_{hour}_{minute}",
            replace_existing=True,
        )
        logger.info(f"已设置提醒：{run_time.strftime('%m-%d %H:%M')} 提醒 {user_id}：{content}")
        return True
    except Exception as e:
        logger.error(f"设置提醒失败: {e}")
        return False


async def send_reminder(user_id: str, content: str) -> None:
    prompt = f"""你之前答应过用户到时间提醒他这件事：{content}
现在时间到了，请用你的语气发一条提醒消息。
要求：自然、像朋友提醒一样，50字以内，直接输出消息内容。"""

    try:
        msg = await _call_ai_simple(prompt, user_id=user_id, max_tokens=300)
        print(f"[DEBUG] 提醒消息内容: [{msg}] 长度:{len(msg)}")
        if not msg or not msg.strip():
            raise ValueError("AI返回空消息")
        bot = get_bot()
        await bot.send_private_msg(user_id=int(user_id), message=msg)
        logger.info(f"提醒已发送给 {user_id}")
    except Exception as e:
        logger.error(f"提醒发送失败: {e}")
        try:
            bot = get_bot()
            await bot.send_private_msg(user_id=int(user_id), message=f"提醒你：{content}")
        except Exception:
            pass

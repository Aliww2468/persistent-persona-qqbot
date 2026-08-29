"""真实时间自主生活插件。定期让雪在没有用户介入时经历至多一个事件。"""

from __future__ import annotations

from datetime import datetime, timedelta

from nonebot import get_driver
from nonebot.log import logger

from core.life_engine import has_life_events, init_life_timeline, run_life_cycle
from core.life_state import load_dynamic_state

driver = get_driver()
config = driver.config

ENABLED = bool(getattr(config, "life_engine_enabled", True))
CYCLE_MINUTES = max(30, int(getattr(config, "life_cycle_minutes", 120)))
MAX_EVENTS_PER_DAY = max(1, int(getattr(config, "life_max_events_per_day", 6)))
OFFLINE_MAX_EVENTS = max(0, int(getattr(config, "life_offline_max_events", 3)))


@driver.on_startup
async def register_autonomous_life() -> None:
    if not ENABLED:
        logger.info("自主生活循环已关闭，将继续使用旧日程状态")
        return
    init_life_timeline()
    load_dynamic_state()

    from plugins.scheduler import scheduler

    scheduler.add_job(
        _run_regular_cycle,
        "interval",
        minutes=CYCLE_MINUTES,
        id="autonomous_life_cycle",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _run_startup_catchup,
        "date",
        run_date=datetime.now() + timedelta(seconds=3),
        id="autonomous_life_startup",
        replace_existing=True,
    )
    logger.info(
        f"已启用自主生活：每 {CYCLE_MINUTES} 分钟一次机会，"
        f"每天最多 {MAX_EVENTS_PER_DAY} 件事"
    )


async def _run_regular_cycle() -> None:
    await run_life_cycle(max_events_per_day=MAX_EVENTS_PER_DAY)


async def _run_startup_catchup() -> None:
    state = load_dynamic_state()
    last_tick_text = str(state.get("last_tick_at", ""))
    try:
        last_tick = datetime.fromisoformat(last_tick_text) if last_tick_text else None
    except ValueError:
        last_tick = None

    now = datetime.now()
    if not has_life_events():
        due = 1
    elif last_tick is None:
        due = 1
    else:
        elapsed_minutes = max(0.0, (now - last_tick).total_seconds() / 60)
        due = min(OFFLINE_MAX_EVENTS, int(elapsed_minutes // CYCLE_MINUTES))

    if due <= 0:
        logger.info("自主生活无需离线补算")
        return

    logger.info(f"自主生活开始离线补算：最多 {due} 件事")
    start = now - timedelta(minutes=CYCLE_MINUTES * max(0, due - 1))
    for index in range(due):
        event_time = start + timedelta(minutes=CYCLE_MINUTES * index)
        await run_life_cycle(
            now=event_time,
            offline_catchup=True,
            max_events_per_day=MAX_EVENTS_PER_DAY,
        )


"""查看雪的动态生活状态与自主时间线；可显式触发一次真实循环。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
nonebot.init(log_level="WARNING")

from core.life_engine import list_life_events, run_life_cycle
from core.life_state import load_dynamic_state


async def main(run_once: bool, limit: int) -> None:
    if run_once:
        event = await run_life_cycle()
        print("本次没有事件（睡眠时段或已达到每日上限）" if event is None else "已完成一次自主生活事件")

    state = load_dynamic_state()
    print("\n===== 当前生活状态 =====")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print("\n===== 最近人生时间线 =====")
    events = list_life_events(limit=limit)
    if not events:
        print("还没有自主生活事件")
        return
    for event in reversed(events):
        print(
            f"#{event['id']} {event['occurred_at']} [{event['memory_status']}] "
            f"{event['title']}\n"
            f"  选择：{event['chosen_action']}\n"
            f"  后果：{event['consequence']}\n"
            f"  想法：{event['inner_thought']}\n"
            f"  重要度：{event['importance']:.2f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="立即执行一次真实AI自主循环")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main(args.run, max(1, args.limit)))


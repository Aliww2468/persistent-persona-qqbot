"""查看影子模式产生的候选事件，或显式手动晋升一条事件。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
nonebot.init(log_level="WARNING")

from core.memory import init_db, list_event_candidates, promote_candidate_to_memory


def main() -> None:
    parser = argparse.ArgumentParser(description="查看雪的影子事件候选")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--status", default=None)
    parser.add_argument("--promote", type=int, metavar="ID")
    parser.add_argument("--confirm", action="store_true", help="确认手动晋升，不可省略")
    args = parser.parse_args()
    init_db()

    if args.promote is not None:
        if not args.confirm:
            raise SystemExit("手动晋升必须同时传入 --confirm")
        memory_id = promote_candidate_to_memory(args.promote, manual=True)
        print(f"候选 {args.promote} 已手动晋升为正式记忆 {memory_id}")
        return

    candidates = list_event_candidates(limit=max(1, args.limit), status=args.status)
    if not candidates:
        print("目前没有候选事件。")
        return

    for item in candidates:
        marker = " [心理转折]" if item["is_turning_point"] else ""
        print(
            f"#{item['id']} {item['status']} score={item['importance_score']:.3f}"
            f" confidence={item['confidence']:.3f}{marker}"
        )
        print(f"  事件：{item['observed_event']}")
        print(f"  含义：{item['subjective_meaning'] or '无'}")
        print(f"  证据：{item['evidence_message_ids']}")
        if item.get("rejection_reason"):
            print(f"  裁决：{item['rejection_reason']}")
        print()


if __name__ == "__main__":
    main()


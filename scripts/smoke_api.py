"""最小 API 连通性测试；不会输出密钥。"""

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
nonebot.init(log_level="WARNING")

from core.llm import complete_json


async def main(background: bool) -> None:
    result = await complete_json(
        '只返回 {"ok": true}',
        system="只返回合法 JSON。",
        max_tokens=30,
        background=background,
        timeout=20,
    )
    if result != {"ok": True}:
        raise RuntimeError(f"API 返回异常: {result}")
    print(f"{'BACKGROUND' if background else 'MAIN'} API SMOKE OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", action="store_true", help="测试主聊天模型，而不是幕后模型")
    args = parser.parse_args()
    asyncio.run(main(background=not args.main))

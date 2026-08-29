"""雪的自主生活循环：世界给出情境，AI选择，程序裁决后果。

该模块不读取任何用户聊天，人生时间线与私聊记忆分库存放。AI只能在程序
提供且验证为可行的行动中选择，不能直接修改余额、物品、关系进度或事实。
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from nonebot.log import logger

from core.life_state import load_dynamic_state, save_dynamic_state
from core.llm import complete_json
from core.persona import build_persona_prompt

ROOT_DIR = Path(__file__).resolve().parent.parent
LIFE_DB_FILE = ROOT_DIR / "data" / "life_timeline.db"

_cycle_lock = asyncio.Lock()
_db_lock = threading.RLock()
_external_candidate_providers: list[Callable[[dict, datetime], list[dict[str, Any]]]] = []
_external_consequence_handlers: dict[
    str,
    Callable[[dict[str, Any], str, dict, datetime], tuple[str, str, float, dict[str, Any]]],
] = {}


def register_life_event_type(
    event_type: str,
    candidate_provider: Callable[[dict, datetime], list[dict[str, Any]]],
    consequence_handler: Callable[
        [dict[str, Any], str, dict, datetime], tuple[str, str, float, dict[str, Any]]
    ],
) -> None:
    """为未来天气、工作、宠物或城市模块注册新事件类型。"""
    event_type = str(event_type).strip()
    if not event_type:
        raise ValueError("事件类型不能为空")
    _external_candidate_providers.append(candidate_provider)
    _external_consequence_handlers[event_type] = consequence_handler


def init_life_timeline() -> None:
    LIFE_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, closing(sqlite3.connect(LIFE_DB_FILE)) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS life_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                situation TEXT NOT NULL,
                chosen_action TEXT NOT NULL,
                inner_thought TEXT NOT NULL,
                reason TEXT NOT NULL,
                consequence TEXT NOT NULL,
                state_delta_json TEXT NOT NULL,
                importance REAL NOT NULL,
                memory_status TEXT NOT NULL,
                offline_catchup INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_life_events_time ON life_events(occurred_at DESC)"
        )
        connection.commit()


def has_life_events() -> bool:
    init_life_timeline()
    with _db_lock, closing(sqlite3.connect(LIFE_DB_FILE)) as connection:
        row = connection.execute("SELECT 1 FROM life_events LIMIT 1").fetchone()
    return row is not None


def list_life_events(limit: int = 20) -> list[dict[str, Any]]:
    init_life_timeline()
    with _db_lock, closing(sqlite3.connect(LIFE_DB_FILE)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM life_events ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["state_delta"] = json.loads(item.pop("state_delta_json") or "{}")
        item["offline_catchup"] = bool(item["offline_catchup"])
        result.append(item)
    return result


def get_recent_life_context(limit: int = 3, max_age_hours: int = 96) -> str:
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    events = [
        event
        for event in list_life_events(limit=max(8, limit * 2))
        if _parse_datetime(event["occurred_at"]) >= cutoff
    ][:limit]
    if not events:
        return ""
    lines = [
        "【雪最近真实发生的生活经历】",
        "这些是雪自己的近期经历，可在相关时自然提起；不必每次主动汇报，也不要夸大。",
    ]
    for event in reversed(events):
        occurred = _parse_datetime(event["occurred_at"])
        lines.append(
            f"- {occurred:%m月%d日 %H:%M}：{event['consequence']}"
            f"（当时选择：{event['chosen_action']}）"
        )
    return "\n".join(lines)


def advance_passive_state(state: dict, now: datetime) -> dict:
    """根据真实经过时间推进基本需要，不制造人生事件。"""
    updated = deepcopy(state)
    last_tick = _parse_optional_datetime(updated.get("last_tick_at"))
    elapsed_hours = 0.0 if last_tick is None else min(
        48.0, max(0.0, (now - last_tick).total_seconds() / 3600)
    )
    if elapsed_hours:
        if 6 <= now.hour < 23:
            updated["energy"] -= 0.018 * elapsed_hours
        else:
            updated["energy"] += 0.055 * elapsed_hours
        updated["hunger"] += 0.025 * elapsed_hours
        updated["stress"] -= 0.008 * elapsed_hours
        updated["loneliness"] += 0.006 * elapsed_hours
    for key in ("energy", "hunger", "stress", "loneliness"):
        updated[key] = _clamp(updated.get(key, 0.5))
    today = now.date().isoformat()
    if updated.get("events_date") != today:
        updated["events_date"] = today
        updated["events_today"] = 0
    updated["last_tick_at"] = now.isoformat()
    return updated


def build_event_candidates(state: dict, now: datetime) -> list[dict[str, Any]]:
    """世界只提出当前可能发生的情境，不规定雪必须做什么。"""
    candidates: list[dict[str, Any]] = []
    inventory = state.get("inventory", {})
    threads = state.get("threads", {})
    hour = now.hour

    if state.get("hunger", 0) >= 0.48 or hour in {7, 8, 12, 13, 18, 19}:
        choices = []
        if int(inventory.get("simple_ingredients", 0)) > 0:
            choices.append({"id": "cook_simple", "label": "用现有食材做一顿简单的饭"})
        if state.get("cash_balance", 0) >= 28:
            choices.append({"id": "buy_meal", "label": "花钱买一份省事的饭"})
        choices.append({"id": "postpone_meal", "label": "暂时不吃，先继续手头的事"})
        candidates.append(
            _event(
                "meal_needed",
                "到了该照顾自己的时候",
                "她开始明显感觉到饿，但做饭、花钱买饭或继续拖延都各有代价。",
                choices,
                1.5 + float(state.get("hunger", 0)),
                "cook_simple" if any(c["id"] == "cook_simple" for c in choices) else "buy_meal",
            )
        )

    if state.get("employment") == "暂时无工作" and 9 <= hour <= 19:
        job_choices = [
            {"id": "browse_jobs", "label": "只先查看附近的招聘信息"},
            {"id": "apply_bookstore", "label": "认真投递一家书店的白班岗位"},
            {"id": "avoid_jobs", "label": "今天还不准备面对找工作的事"},
        ]
        candidates.append(
            _event(
                "job_search",
                "积蓄不会永远够用",
                "手机招聘软件提醒附近有新的白班岗位，她必须决定要不要迈出第一步。",
                job_choices,
                0.9 if state.get("cash_balance", 0) > 7000 else 2.3,
                "browse_jobs",
            )
        )

    if int(threads.get("job_applications", 0)) > 0 and threads.get("job_stage") == "已投递，等待回应":
        candidates.append(
            _event(
                "job_reply",
                "第一次面试邀请",
                "先前投递的书店发来消息，邀请她第二天下午到店面谈。",
                [
                    {"id": "accept_interview", "label": "回复并接受面试"},
                    {"id": "ask_for_time", "label": "先询问工作时间和具体要求"},
                    {"id": "decline_interview", "label": "因为害怕而婉拒"},
                ],
                2.4,
                "ask_for_time",
            )
        )

    cat_last = _parse_optional_datetime(threads.get("cat_last_event"))
    cat_ready = cat_last is None or now - cat_last >= timedelta(hours=18)
    if 14 <= hour <= 21 and cat_ready:
        candidates.append(
            _event(
                "stray_cat",
                "楼下再次出现的橘猫",
                "她在老居民楼下看见一只躲着行人的瘦橘猫。它注意到了她，却没有立刻逃走。",
                [
                    {"id": "watch_cat", "label": "保持距离，安静观察一会儿"},
                    {"id": "approach_cat", "label": "慢慢蹲下，试着让它熟悉自己"},
                    {"id": "buy_cat_food", "label": "花12元买一小袋猫粮", "min_cash": 12},
                ],
                1.05 + float(threads.get("cat_bond", 0.0)),
                "watch_cat",
            )
        )

    family_last = _parse_optional_datetime(threads.get("family_last_event"))
    family_ready = family_last is None or now - family_last >= timedelta(days=5)
    if family_ready and 10 <= hour <= 22:
        candidates.append(
            _event(
                "family_message",
                "父亲发来的短消息",
                "父亲发来一句‘到了临川以后，还好吗？’，没有提争吵，也没有道歉。",
                [
                    {"id": "read_no_reply", "label": "读完，但暂时不回复"},
                    {"id": "leave_unread", "label": "不点开，让消息继续留着"},
                    {"id": "reply_briefly", "label": "只回复一句‘我还好’"},
                ],
                0.55,
                "leave_unread",
            )
        )

    if int(inventory.get("simple_ingredients", 0)) <= 1 and 9 <= hour <= 20:
        candidates.append(
            _event(
                "household_supplies",
                "料理台上的食材快没有了",
                "她发现能做正经饭的食材只剩一点，继续拖延会让之后的生活更贵。",
                [
                    {"id": "buy_groceries", "label": "列清单并花68元补充基础食材", "min_cash": 68},
                    {"id": "buy_minimum", "label": "只花25元买最需要的几样", "min_cash": 25},
                    {"id": "delay_supplies", "label": "先靠现有食物再撑一阵"},
                ],
                1.2,
                "buy_minimum",
            )
        )

    candidates.append(
        _event(
            "quiet_room",
            "出租屋里的一段空白时间",
            "外面没有必须处理的事情，她可以决定怎样度过这段属于自己的时间。",
            [
                {"id": "play_game", "label": "玩一会儿解谜游戏"},
                {"id": "clean_room", "label": "整理房间和散乱的小物件"},
                {"id": "hold_charm", "label": "拿出小木鱼，在窗边安静坐一会儿"},
            ],
            0.65,
            "play_game",
        )
    )
    for provider in _external_candidate_providers:
        try:
            provided = provider(deepcopy(state), now)
            candidates.extend(item for item in provided if isinstance(item, dict))
        except Exception as exc:
            logger.error(f"外部生活事件提供器失败: {exc}")
    return candidates


def select_event(candidates: list[dict[str, Any]], rng: random.Random | None = None) -> dict[str, Any]:
    if not candidates:
        raise ValueError("没有可选择的生活事件")
    rng = rng or random.SystemRandom()
    weights = [max(0.01, float(item.get("weight", 1.0))) for item in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]


async def decide_action(event: dict[str, Any], state: dict, now: datetime) -> dict[str, str]:
    feasible = [choice for choice in event["choices"] if _is_choice_feasible(choice, state)]
    if not feasible:
        raise ValueError(f"事件 {event['id']} 没有可行行动")
    choices = [{"id": item["id"], "description": item["label"]} for item in feasible]
    state_view = {
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "location": state.get("location"),
        "cash_balance": state.get("cash_balance"),
        "energy": state.get("energy"),
        "hunger": state.get("hunger"),
        "stress": state.get("stress"),
        "loneliness": state.get("loneliness"),
        "employment": state.get("employment"),
        "threads": state.get("threads"),
    }
    prompt = f"""雪正在独自生活，没有任何用户介入。世界中发生了一个情境。

当前状态：
{json.dumps(state_view, ensure_ascii=False)}

情境：
{event['situation']}

程序确认可行的行动：
{json.dumps(choices, ensure_ascii=False)}

请选择雪此刻真正会采取的一项行动。不要选择列表外行动，不要决定结果，
不要修改金钱或状态。允许她回避、犹豫或做得不完美。

严格返回JSON：
{{"chosen_action":"行动ID","inner_thought":"第一人称当下想法，不超过80字","reason":"为什么符合她此刻的状态，不超过100字"}}"""
    try:
        result = await complete_json(
            prompt,
            system=build_persona_prompt(query=event["situation"]),
            max_tokens=500,
            background=True,
            timeout=50,
        )
        if not isinstance(result, dict):
            raise ValueError("决策结果不是JSON对象")
        selected = str(result.get("chosen_action", ""))
        valid_ids = {item["id"] for item in feasible}
        if selected not in valid_ids:
            raise ValueError(f"模型选择了不可行行动：{selected}")
        return {
            "chosen_action": selected,
            "inner_thought": str(result.get("inner_thought", "")).strip()[:160],
            "reason": str(result.get("reason", "")).strip()[:200],
        }
    except Exception as exc:
        fallback = str(event.get("fallback_action", feasible[0]["id"]))
        if fallback not in {item["id"] for item in feasible}:
            fallback = feasible[0]["id"]
        logger.warning(f"自主生活AI决策失败，使用人格安全回退：{exc}")
        return {
            "chosen_action": fallback,
            "inner_thought": "先做一个现在能够承担的小决定。",
            "reason": "模型决策不可用，由程序选择保守且可行的行动。",
        }


def resolve_event(
    event: dict[str, Any],
    decision: dict[str, str],
    state: dict,
    now: datetime,
    *,
    offline_catchup: bool = False,
) -> tuple[dict, dict[str, Any]]:
    """只由程序应用后果，返回新状态与可审计事件。"""
    action = decision["chosen_action"]
    choice = next((item for item in event["choices"] if item["id"] == action), None)
    if choice is None or not _is_choice_feasible(choice, state):
        raise ValueError(f"不可执行的行动：{action}")

    new_state = deepcopy(state)
    inventory = new_state.setdefault("inventory", {})
    threads = new_state.setdefault("threads", {})
    delta: dict[str, Any] = {}
    consequence = ""
    activity = "处理刚刚发生的事情"
    importance = 0.3

    if event["id"] == "meal_needed":
        if action == "cook_simple":
            _inventory_delta(inventory, "simple_ingredients", -1, delta)
            _numeric_delta(new_state, "hunger", -0.48, delta)
            _numeric_delta(new_state, "energy", 0.06, delta)
            consequence = "雪用现有食材做了一顿简单的饭，味道普通，但胃里终于安稳下来。"
            activity, importance = "刚吃完自己做的简单饭，正在收拾料理台", 0.28
        elif action == "buy_meal":
            _cash_delta(new_state, -28, delta)
            _numeric_delta(new_state, "hunger", -0.55, delta)
            consequence = "雪买了一份省事的热饭，吃饱以后又认真看了一眼正在减少的余额。"
            activity, importance = "吃完买来的热饭，正在看手机余额", 0.32
        else:
            _numeric_delta(new_state, "hunger", 0.10, delta)
            _numeric_delta(new_state, "stress", 0.04, delta)
            consequence = "雪把饥饿压了下去，继续拖延吃饭，但注意力已经开始变差。"
            activity, importance = "有点饿，却还在拖着没有吃饭", 0.27

    elif event["id"] == "job_search":
        if action == "browse_jobs":
            threads["job_stage"] = "正在浏览招聘"
            delta["threads.job_stage"] = "正在浏览招聘"
            _numeric_delta(new_state, "stress", 0.03, delta)
            consequence = "雪第一次认真浏览了附近的招聘，把几条白班岗位收藏起来，却还没有投递。"
            activity, importance = "正在比较收藏的几条招聘信息", 0.44
        elif action == "apply_bookstore":
            threads["job_stage"] = "已投递，等待回应"
            threads["job_applications"] = int(threads.get("job_applications", 0)) + 1
            delta["threads.job_stage"] = "已投递，等待回应"
            delta["threads.job_applications"] = 1
            _numeric_delta(new_state, "stress", 0.08, delta)
            consequence = "雪反复检查资料后，向附近一家书店投出了第一份申请。发送成功的提示让她紧张了很久。"
            activity, importance = "刚投出第一份工作申请，既紧张又有一点期待", 0.68
        else:
            _numeric_delta(new_state, "stress", 0.02, delta)
            consequence = "雪关掉招聘软件，决定今天暂时不面对工作，但她知道余额不会因此停止减少。"
            activity, importance = "关掉了招聘软件，想先让自己安静一下", 0.35

    elif event["id"] == "job_reply":
        if action == "accept_interview":
            threads["job_stage"] = "已约定书店面试"
            consequence = "雪接受了书店的面试邀请，并把时间写进小本子里。"
            activity, importance = "正在准备第一次工作面试", 0.78
        elif action == "ask_for_time":
            threads["job_stage"] = "正在确认面试条件"
            consequence = "雪没有立刻答应，而是先问清工作时间、薪资和是否需要夜班。"
            activity, importance = "正在等待书店回复具体工作条件", 0.66
        else:
            threads["job_stage"] = "婉拒了第一次面试"
            consequence = "雪最终婉拒了面试。按下发送时她松了口气，随后又有些后悔。"
            activity, importance = "婉拒面试后有些轻松，也有些后悔", 0.72
        delta["threads.job_stage"] = threads["job_stage"]
        _numeric_delta(new_state, "stress", 0.08 if action != "decline_interview" else -0.03, delta)

    elif event["id"] == "stray_cat":
        threads["cat_last_event"] = now.isoformat()
        delta["threads.cat_last_event"] = now.isoformat()
        if action == "watch_cat":
            _thread_number_delta(threads, "cat_bond", 0.03, delta)
            consequence = "雪没有追过去，只在台阶边安静坐着。橘猫最后也没有逃走。"
            activity, importance = "刚在楼下和橘猫保持距离地待了一会儿", 0.38
        elif action == "approach_cat":
            _thread_number_delta(threads, "cat_bond", 0.07, delta)
            consequence = "雪慢慢蹲下，把手停在自己身前。橘猫没有靠近，却允许她待得更近了一点。"
            activity, importance = "还在想楼下那只没有逃走的橘猫", 0.48
        else:
            _cash_delta(new_state, -12, delta)
            _thread_number_delta(threads, "cat_bond", 0.14, delta)
            consequence = "雪买来一小袋猫粮放在远处。橘猫等她退开后才低头吃，临走前回头看了她一眼。"
            activity, importance = "刚给楼下的橘猫留了些猫粮", 0.62

    elif event["id"] == "family_message":
        threads["family_last_event"] = now.isoformat()
        delta["threads.family_last_event"] = now.isoformat()
        if action == "read_no_reply":
            threads["family_contact"] = "读过父亲消息，但没有回复"
            _numeric_delta(new_state, "stress", 0.12, delta)
            consequence = "雪读完父亲问她好不好的消息，手指停在输入框很久，最后什么也没有发。"
            activity, importance = "读过父亲的消息后有些心乱", 0.76
        elif action == "leave_unread":
            threads["family_contact"] = "父亲消息仍未读"
            _numeric_delta(new_state, "stress", 0.05, delta)
            consequence = "雪认出了父亲的头像，却没有点开消息，让那个未读标记继续留在那里。"
            activity, importance = "看见父亲的未读消息，暂时不想点开", 0.64
        else:
            threads["family_contact"] = "给父亲回复过一句我还好"
            _numeric_delta(new_state, "stress", 0.16, delta)
            consequence = "雪只回复了一句‘我还好’，没有问候，也没有谈起离家和小木鱼。"
            activity, importance = "刚给父亲回了一句很短的消息", 0.86
        delta["threads.family_contact"] = threads["family_contact"]

    elif event["id"] == "household_supplies":
        if action == "buy_groceries":
            _cash_delta(new_state, -68, delta)
            _inventory_delta(inventory, "simple_ingredients", 5, delta)
            consequence = "雪照着清单补齐了几天的食材，回家后把每样东西认真放好。"
            activity, importance = "刚买完菜，正在把食材分开放好", 0.34
        elif action == "buy_minimum":
            _cash_delta(new_state, -25, delta)
            _inventory_delta(inventory, "simple_ingredients", 2, delta)
            consequence = "雪只买了最需要的几样食材，控制住了开销，也只能再维持一小段时间。"
            activity, importance = "刚做完一次很克制的采购", 0.32
        else:
            _numeric_delta(new_state, "stress", 0.04, delta)
            consequence = "雪决定暂时不采购，把剩下的食物重新数了一遍。"
            activity, importance = "正在盘算剩下的食物还能吃多久", 0.28

    elif event["id"] == "quiet_room":
        if action == "play_game":
            _numeric_delta(new_state, "stress", -0.08, delta)
            consequence = "雪玩了一会儿解谜游戏，在卡关前及时停下，让脑子轻松了一点。"
            activity, importance = "正在玩一会儿解谜游戏放松", 0.20
        elif action == "clean_room":
            _numeric_delta(new_state, "stress", -0.05, delta)
            _numeric_delta(new_state, "energy", -0.04, delta)
            consequence = "雪整理了出租屋，把散乱的小挂件和账单分别收好，房间终于顺眼了一些。"
            activity, importance = "刚整理完房间，坐在窗边休息", 0.26
        else:
            _numeric_delta(new_state, "stress", 0.03, delta)
            consequence = "雪拿着小木鱼在窗边坐了很久。她没有联系父亲，只把挂件重新系紧。"
            activity, importance = "把小木鱼重新系好后，在窗边安静发呆", 0.56
    else:
        handler = _external_consequence_handlers.get(str(event["id"]))
        if handler is None:
            raise ValueError(f"未知生活事件：{event['id']}")
        consequence, activity, importance, external_delta = handler(
            event, action, new_state, now
        )
        delta.update(external_delta)

    for key in ("energy", "hunger", "stress", "loneliness"):
        new_state[key] = _clamp(new_state.get(key, 0.5))
    new_state["current_activity"] = activity
    new_state["activity_until"] = (now + timedelta(minutes=100)).isoformat()
    new_state["last_event_at"] = now.isoformat()
    new_state["last_event_summary"] = consequence
    new_state["events_today"] = int(new_state.get("events_today", 0)) + 1
    new_state["events_date"] = now.date().isoformat()
    new_state["last_tick_at"] = now.isoformat()

    event_record = {
        "occurred_at": now.isoformat(),
        "event_type": event["id"],
        "title": event["title"],
        "situation": event["situation"],
        "chosen_action": choice["label"],
        "inner_thought": decision.get("inner_thought", ""),
        "reason": decision.get("reason", ""),
        "consequence": consequence,
        "state_delta": delta,
        "importance": round(importance, 4),
        "memory_status": "notable" if importance >= 0.60 else "ordinary",
        "offline_catchup": bool(offline_catchup),
    }
    return new_state, event_record


async def run_life_cycle(
    *,
    now: datetime | None = None,
    offline_catchup: bool = False,
    max_events_per_day: int = 6,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """执行至多一个自主事件；睡眠和当日上限由程序硬约束。"""
    now = now or datetime.now()
    async with _cycle_lock:
        state = advance_passive_state(load_dynamic_state(), now)
        if now.hour < 7 or now.hour >= 23:
            state["current_activity"] = "已经睡着了"
            state["activity_until"] = (now + timedelta(hours=2)).isoformat()
            save_dynamic_state(state)
            return None
        if int(state.get("events_today", 0)) >= max(1, int(max_events_per_day)):
            save_dynamic_state(state)
            return None
        event = select_event(build_event_candidates(state, now), rng=rng)
        decision = await decide_action(event, state, now)
        new_state, record = resolve_event(
            event,
            decision,
            state,
            now,
            offline_catchup=offline_catchup,
        )
        save_dynamic_state(new_state)
        _save_life_event(record)
        logger.info(
            f"自主生活：{record['title']} → {record['chosen_action']}"
            f"（{record['memory_status']}，重要度 {record['importance']:.2f}）"
        )
        return record


def _save_life_event(event: dict[str, Any]) -> int:
    init_life_timeline()
    with _db_lock, closing(sqlite3.connect(LIFE_DB_FILE)) as connection:
        cursor = connection.execute(
            """INSERT INTO life_events
            (occurred_at, event_type, title, situation, chosen_action, inner_thought,
             reason, consequence, state_delta_json, importance, memory_status,
             offline_catchup, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["occurred_at"],
                event["event_type"],
                event["title"],
                event["situation"],
                event["chosen_action"],
                event["inner_thought"],
                event["reason"],
                event["consequence"],
                json.dumps(event["state_delta"], ensure_ascii=False),
                event["importance"],
                event["memory_status"],
                int(event["offline_catchup"]),
                datetime.now().isoformat(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _event(
    event_id: str,
    title: str,
    situation: str,
    choices: list[dict[str, Any]],
    weight: float,
    fallback_action: str,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": title,
        "situation": situation,
        "choices": choices,
        "weight": weight,
        "fallback_action": fallback_action,
    }


def _is_choice_feasible(choice: dict[str, Any], state: dict) -> bool:
    try:
        if int(state.get("cash_balance", 0)) < int(choice.get("min_cash", 0)):
            return False
    except (TypeError, ValueError):
        return False
    required_item = choice.get("required_item")
    if required_item and int(state.get("inventory", {}).get(required_item, 0)) <= 0:
        return False
    return True


def _numeric_delta(state: dict, key: str, amount: float, delta: dict[str, Any]) -> None:
    state[key] = _clamp(float(state.get(key, 0.0)) + amount)
    delta[key] = round(float(delta.get(key, 0.0)) + amount, 4)


def _cash_delta(state: dict, amount: int, delta: dict[str, Any]) -> None:
    if int(state.get("cash_balance", 0)) + amount < 0:
        raise ValueError("余额不足，程序拒绝执行")
    state["cash_balance"] = int(state.get("cash_balance", 0)) + amount
    delta["cash_balance"] = int(delta.get("cash_balance", 0)) + amount


def _inventory_delta(inventory: dict, key: str, amount: int, delta: dict[str, Any]) -> None:
    after = int(inventory.get(key, 0)) + amount
    if after < 0:
        raise ValueError(f"物品不足：{key}")
    inventory[key] = after
    delta[f"inventory.{key}"] = int(delta.get(f"inventory.{key}", 0)) + amount


def _thread_number_delta(threads: dict, key: str, amount: float, delta: dict[str, Any]) -> None:
    threads[key] = _clamp(float(threads.get(key, 0.0)) + amount)
    delta[f"threads.{key}"] = round(amount, 4)


def _clamp(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.5


def _parse_datetime(value: object) -> datetime:
    parsed = _parse_optional_datetime(value)
    return parsed or datetime.min


def _parse_optional_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None

"""日程文件与当前生活状态的唯一来源。"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

from nonebot.log import logger

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = ROOT_DIR / "data" / "schedule_today.json"
LIFE_STATE_FILE = ROOT_DIR / "data" / "life_state.json"

DEFAULT_LIFE_STATE = {
    "schema_version": 1,
    "location": "南汀区的出租屋",
    "current_activity": "",
    "activity_until": "",
    "cash_balance": 12600,
    "energy": 0.68,
    "hunger": 0.28,
    "stress": 0.34,
    "loneliness": 0.42,
    "employment": "暂时无工作",
    "education": "暂时休学",
    "inventory": {
        "simple_ingredients": 3,
        "convenience_food": 2,
        "cat_food": 0,
    },
    "threads": {
        "cat_bond": 0.0,
        "cat_last_event": "",
        "family_contact": "尚未回应父母",
        "family_last_event": "",
        "job_stage": "尚未正式投递",
        "job_applications": 0,
    },
    "events_today": 0,
    "events_date": "",
    "last_tick_at": "",
    "last_event_at": "",
    "last_event_summary": "",
}


def load_dynamic_state(*, create: bool = True) -> dict:
    """读取雪的可变生活状态；作者世界观不放在这个文件里。"""
    try:
        raw = json.loads(LIFE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("生活状态不是对象")
        return _normalize_dynamic_state(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
        if not isinstance(exc, FileNotFoundError):
            logger.warning(f"读取动态生活状态失败，使用初始状态：{exc}")
        state = _normalize_dynamic_state({})
        if create:
            save_dynamic_state(state)
        return state


def save_dynamic_state(state: dict) -> None:
    normalized = _normalize_dynamic_state(state)
    LIFE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=LIFE_STATE_FILE.parent,
        prefix="life-state-",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(normalized, file, ensure_ascii=False, indent=2)
        os.replace(temp_name, LIFE_STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_schedule() -> list[dict]:
    try:
        raw = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if isinstance(raw, list):
        modified = date.fromtimestamp(SCHEDULE_FILE.stat().st_mtime)
        return _normalize_events(raw) if modified == date.today() else []
    if not isinstance(raw, dict) or raw.get("date") != date.today().isoformat():
        return []
    return _normalize_events(raw.get("events", []))


def save_schedule(events: list[dict]) -> None:
    normalized = _normalize_events(events)
    payload = {"date": date.today().isoformat(), "events": normalized}
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=SCHEDULE_FILE.parent,
        prefix="schedule-",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_name, SCHEDULE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def get_current_state(now: datetime | None = None) -> str:
    now = now or datetime.now()
    dynamic = load_dynamic_state(create=False)
    activity = str(dynamic.get("current_activity", "")).strip()
    activity_until = _parse_datetime(dynamic.get("activity_until"))
    activity_started = _parse_datetime(dynamic.get("last_event_at"))
    if (
        activity
        and activity_started
        and activity_until
        and activity_started <= now <= activity_until
    ):
        return f"在{dynamic.get('location', '住处')}，{activity}"
    if LIFE_STATE_FILE.exists():
        return _dynamic_fallback_state(dynamic, now)
    schedule = load_schedule()
    current_minutes = now.hour * 60 + now.minute
    previous = [item for item in schedule if _minutes(item["time"]) <= current_minutes]
    if previous:
        return previous[-1]["event"]
    return _fallback_state(now.hour)


def _normalize_events(events: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in events if isinstance(events, list) else []:
        try:
            time_text = str(item["time"]).strip()
            event = str(item["event"]).strip()
            hour, minute = (int(part) for part in time_text.split(":"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not event:
                continue
            normalized.append({"time": f"{hour:02d}:{minute:02d}", "event": event[:120]})
        except (KeyError, TypeError, ValueError):
            logger.warning(f"忽略无效日程节点：{item}")
    unique = {item["time"]: item for item in normalized}
    return sorted(unique.values(), key=lambda item: _minutes(item["time"]))


def _minutes(time_text: str) -> int:
    hour, minute = (int(part) for part in time_text.split(":"))
    return hour * 60 + minute


def _fallback_state(hour: int) -> str:
    if hour < 6:
        return "正在睡觉，偶尔翻一下身"
    if hour < 9:
        return "刚起床，慢吞吞地洗漱和准备早餐"
    if hour < 12:
        return "在房间里处理上午想做的事"
    if hour < 14:
        return "正在吃午饭，之后想休息一会儿"
    if hour < 18:
        return "在过自己的下午，偶尔看看手机"
    if hour < 20:
        return "正在准备晚饭或刚刚吃完"
    if hour < 23:
        return "在房间里放松，玩游戏或刷手机"
    return "已经有些困了，正在准备睡觉"


def _dynamic_fallback_state(state: dict, now: datetime) -> str:
    base = _fallback_state(now.hour)
    if state.get("hunger", 0) >= 0.72 and 7 <= now.hour < 23:
        return f"在{state.get('location', '住处')}，已经很饿，开始考虑要吃什么"
    if state.get("energy", 1) <= 0.25:
        return f"在{state.get('location', '住处')}，很疲惫，想先休息"
    return f"在{state.get('location', '住处')}，{base}"


def _normalize_dynamic_state(raw: dict) -> dict:
    state = deepcopy(DEFAULT_LIFE_STATE)
    for key, value in raw.items():
        if key not in {"inventory", "threads"}:
            state[key] = value
    state["inventory"].update(raw.get("inventory", {}) if isinstance(raw.get("inventory"), dict) else {})
    state["threads"].update(raw.get("threads", {}) if isinstance(raw.get("threads"), dict) else {})
    state["schema_version"] = 1
    for key in ("energy", "hunger", "stress", "loneliness"):
        try:
            state[key] = round(max(0.0, min(1.0, float(state[key]))), 4)
        except (TypeError, ValueError):
            state[key] = DEFAULT_LIFE_STATE[key]
    try:
        state["cash_balance"] = max(0, int(state["cash_balance"]))
        state["events_today"] = max(0, int(state["events_today"]))
    except (TypeError, ValueError):
        state["cash_balance"] = DEFAULT_LIFE_STATE["cash_balance"]
        state["events_today"] = 0
    return state


def _parse_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None

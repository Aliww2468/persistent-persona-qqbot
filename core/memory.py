"""SQLite 对话、关系成长与心理事件数据。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from nonebot import get_driver
from nonebot.log import logger

from core.decay import decay_toward_baseline, elapsed_days
from core.llm import complete

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "bot.db"
MAX_MESSAGES = 20
KEEP_MESSAGES = 10


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with get_conn() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                emotion TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'conversation',
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS psychological_schemas (
                id TEXT PRIMARY KEY,
                belief TEXT NOT NULL,
                strength REAL NOT NULL,
                baseline_strength REAL NOT NULL,
                sensitivity REAL NOT NULL,
                stability REAL NOT NULL,
                half_life_days REAL NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                mutable INTEGER NOT NULL DEFAULT 1,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emotional_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mood TEXT NOT NULL,
                valence REAL NOT NULL,
                arousal REAL NOT NULL,
                stress REAL NOT NULL,
                social_safety REAL NOT NULL,
                loneliness REAL NOT NULL,
                need_for_closeness REAL NOT NULL,
                baseline_json TEXT NOT NULL,
                half_life_hours REAL NOT NULL,
                active_causes_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                observed_event TEXT NOT NULL,
                subjective_meaning TEXT NOT NULL,
                evidence_message_ids TEXT NOT NULL,
                evidence_snapshot_json TEXT NOT NULL DEFAULT '[]',
                activated_schemas_json TEXT NOT NULL,
                emotions_json TEXT NOT NULL,
                proposed_belief_changes_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                emotional_intensity REAL NOT NULL,
                relationship_weight REAL NOT NULL,
                vulnerability REAL NOT NULL,
                social_cost REAL NOT NULL,
                identity_relevance REAL NOT NULL,
                expectation_violation REAL NOT NULL,
                schema_activation REAL NOT NULL,
                breakthrough_score REAL NOT NULL,
                importance_score REAL NOT NULL,
                is_turning_point INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'shadow',
                rejection_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS belief_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_id TEXT NOT NULL,
                memory_id INTEGER,
                candidate_id INTEGER,
                before_strength REAL NOT NULL,
                proposed_delta REAL NOT NULL,
                applied_delta REAL NOT NULL,
                after_strength REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(schema_id) REFERENCES psychological_schemas(id)
            );

            CREATE TABLE IF NOT EXISTS appraisal_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                first_message_id INTEGER NOT NULL,
                last_message_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appraisal_checkpoints (
                user_id TEXT PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_candidates_user_created
                ON event_candidates(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_candidates_status
                ON event_candidates(status, importance_score DESC);
            """
        )
        _ensure_episodic_memory_columns(connection)
        _ensure_event_candidate_columns(connection)
        _seed_psychology(connection)
        connection.commit()
    logger.info(f"记忆与心理数据库已初始化：{DB_PATH}")


def _ensure_episodic_memory_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(episodic_memories)").fetchall()
    }
    additions = {
        "user_id": "TEXT NOT NULL DEFAULT ''",
        "candidate_id": "INTEGER",
        "raw_event": "TEXT NOT NULL DEFAULT ''",
        "meaning_at_time": "TEXT NOT NULL DEFAULT ''",
        "current_meaning": "TEXT NOT NULL DEFAULT ''",
        "memory_strength": "REAL NOT NULL DEFAULT 0.5",
        "accessibility": "REAL NOT NULL DEFAULT 0.5",
        "emotional_charge": "REAL NOT NULL DEFAULT 0.0",
        "relationship_impact": "REAL NOT NULL DEFAULT 0.0",
        "belief_impact": "REAL NOT NULL DEFAULT 0.0",
        "stability": "REAL NOT NULL DEFAULT 0.4",
        "memory_half_life_days": "REAL NOT NULL DEFAULT 90.0",
        "emotion_half_life_days": "REAL NOT NULL DEFAULT 14.0",
        "is_turning_point": "INTEGER NOT NULL DEFAULT 0",
        "evidence_message_ids": "TEXT NOT NULL DEFAULT '[]'",
        "evidence_snapshot_json": "TEXT NOT NULL DEFAULT '[]'",
        "activated_schemas_json": "TEXT NOT NULL DEFAULT '[]'",
        "last_recalled_at": "TEXT",
        "recall_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE episodic_memories ADD COLUMN {name} {definition}")


def _ensure_event_candidate_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(event_candidates)").fetchall()
    }
    if "evidence_snapshot_json" not in existing:
        connection.execute(
            "ALTER TABLE event_candidates ADD COLUMN evidence_snapshot_json "
            "TEXT NOT NULL DEFAULT '[]'"
        )


def _seed_psychology(connection: sqlite3.Connection) -> None:
    from core.persona import load_persona

    now = datetime.now().isoformat()
    persona = load_persona()
    for schema in persona.get("psychological_schemas", []):
        connection.execute(
            """INSERT OR IGNORE INTO psychological_schemas
            (id, belief, strength, baseline_strength, sensitivity, stability,
             half_life_days, source, mutable, evidence_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                schema["id"],
                schema["belief"],
                float(schema.get("strength", 0.5)),
                float(schema.get("strength", 0.5)),
                float(schema.get("sensitivity", 0.5)),
                float(schema.get("stability", 0.5)),
                float(schema.get("half_life_days", 180)),
                str(schema.get("source", "")),
                int(bool(schema.get("mutable", True))),
                now,
            ),
        )
        # 作者设定中的文案与规则可以迁移；已经由经历改变的强度不能被启动过程重置。
        connection.execute(
            """UPDATE psychological_schemas
            SET belief = ?, sensitivity = ?, stability = ?, half_life_days = ?,
                source = ?, mutable = ?
            WHERE id = ?""",
            (
                schema["belief"],
                float(schema.get("sensitivity", 0.5)),
                float(schema.get("stability", 0.5)),
                float(schema.get("half_life_days", 180)),
                schema.get("source", ""),
                int(bool(schema.get("mutable", True))),
                schema["id"],
            ),
        )

    baseline = {
        "valence": 0.55,
        "arousal": 0.42,
        "stress": 0.18,
        "social_safety": 0.62,
        "loneliness": 0.24,
        "need_for_closeness": 0.36,
    }
    connection.execute(
        """INSERT OR IGNORE INTO emotional_state
        (id, mood, valence, arousal, stress, social_safety, loneliness,
         need_for_closeness, baseline_json, half_life_hours,
         active_causes_json, updated_at)
        VALUES (1, '平静', ?, ?, ?, ?, ?, ?, ?, 18.0, '[]', ?)""",
        (
            baseline["valence"],
            baseline["arousal"],
            baseline["stress"],
            baseline["social_safety"],
            baseline["loneliness"],
            baseline["need_for_closeness"],
            json.dumps(baseline, ensure_ascii=False),
            now,
        ),
    )


def get_summary(user_id: str) -> str:
    with get_conn() as connection:
        row = connection.execute(
            "SELECT summary FROM summaries WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return row["summary"] if row else ""


def get_recent_messages(user_id: str, limit: int = KEEP_MESSAGES) -> list[dict]:
    return [
        {"role": row["role"], "content": row["content"]}
        for row in get_recent_messages_with_ids(user_id, limit)
    ]


def get_recent_messages_with_ids(user_id: str, limit: int = KEEP_MESSAGES) -> list[dict]:
    with get_conn() as connection:
        rows = connection.execute(
            """SELECT id, role, content, timestamp FROM messages
            WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
            (str(user_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def save_message(user_id: str, role: str, content: str) -> int:
    with get_conn() as connection:
        cursor = connection.execute(
            "INSERT INTO messages (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (str(user_id), role, content, datetime.now().isoformat()),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_message_count(user_id: str) -> int:
    with get_conn() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return int(row["count"])


def get_appraisal_checkpoint(user_id: str) -> int:
    with get_conn() as connection:
        row = connection.execute(
            "SELECT last_message_id FROM appraisal_checkpoints WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    return int(row["last_message_id"]) if row else 0


def set_appraisal_checkpoint(user_id: str, last_message_id: int) -> None:
    with get_conn() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO appraisal_checkpoints
            (user_id, last_message_id, updated_at) VALUES (?, ?, ?)""",
            (str(user_id), int(last_message_id), datetime.now().isoformat()),
        )
        connection.commit()


def get_active_schemas(limit: int = 12) -> list[dict]:
    with get_conn() as connection:
        rows = connection.execute(
            """SELECT * FROM psychological_schemas
            ORDER BY sensitivity * strength DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    now = datetime.now()
    result = []
    for row in rows:
        item = dict(row)
        updated = datetime.fromisoformat(item["updated_at"])
        effective_strength = decay_toward_baseline(
            item["strength"],
            item["baseline_strength"],
            elapsed_days(updated, now),
            item["half_life_days"],
        )
        item["effective_strength"] = round(effective_strength, 4)
        result.append(item)
    return result


def get_effective_emotional_state() -> dict[str, Any]:
    with get_conn() as connection:
        row = connection.execute("SELECT * FROM emotional_state WHERE id = 1").fetchone()
    if not row:
        return {}
    state = dict(row)
    baseline = json.loads(state["baseline_json"])
    updated = datetime.fromisoformat(state["updated_at"])
    days = elapsed_days(updated)
    half_life_days = max(0.01, float(state["half_life_hours"]) / 24.0)
    for key in baseline:
        state[key] = round(
            decay_toward_baseline(state[key], baseline[key], days, half_life_days),
            4,
        )
    state["active_causes"] = json.loads(state["active_causes_json"])
    return state


def save_appraisal_run(
    user_id: str,
    messages: list[dict],
    result: Any,
    candidate_count: int,
    error: str = "",
) -> int:
    if not messages:
        return 0
    with get_conn() as connection:
        cursor = connection.execute(
            """INSERT INTO appraisal_runs
            (user_id, first_message_id, last_message_id, message_count,
             result_json, candidate_count, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(user_id),
                int(messages[0]["id"]),
                int(messages[-1]["id"]),
                len(messages),
                json.dumps(result, ensure_ascii=False),
                int(candidate_count),
                error[:500],
                datetime.now().isoformat(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def save_event_candidate(candidate: dict[str, Any]) -> int:
    with get_conn() as connection:
        cursor = connection.execute(
            """INSERT INTO event_candidates
            (user_id, event_type, observed_event, subjective_meaning,
             evidence_message_ids, evidence_snapshot_json,
             activated_schemas_json, emotions_json,
             proposed_belief_changes_json, confidence, emotional_intensity,
             relationship_weight, vulnerability, social_cost, identity_relevance,
             expectation_violation, schema_activation, breakthrough_score,
             importance_score, is_turning_point, status, rejection_reason,
             created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate["user_id"],
                candidate["event_type"],
                candidate["observed_event"],
                candidate["subjective_meaning"],
                json.dumps(candidate["evidence_message_ids"], ensure_ascii=False),
                json.dumps(candidate.get("evidence_snapshot", []), ensure_ascii=False),
                json.dumps(candidate["activated_schemas"], ensure_ascii=False),
                json.dumps(candidate["emotions"], ensure_ascii=False),
                json.dumps(candidate["proposed_belief_changes"], ensure_ascii=False),
                candidate["confidence"],
                candidate["emotional_intensity"],
                candidate["relationship_weight"],
                candidate["vulnerability"],
                candidate["social_cost"],
                candidate["identity_relevance"],
                candidate["expectation_violation"],
                candidate["schema_activation"],
                candidate["breakthrough_score"],
                candidate["importance_score"],
                int(candidate["is_turning_point"]),
                candidate.get("status", "shadow"),
                candidate.get("rejection_reason", ""),
                datetime.now().isoformat(),
                candidate.get("expires_at"),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_event_candidate(candidate_id: int) -> dict[str, Any] | None:
    with get_conn() as connection:
        row = connection.execute(
            "SELECT * FROM event_candidates WHERE id = ?", (int(candidate_id),)
        ).fetchone()
    return _decode_candidate(dict(row)) if row else None


def list_event_candidates(limit: int = 20, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM event_candidates"
    parameters: list[Any] = []
    if status:
        query += " WHERE status = ?"
        parameters.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    parameters.append(int(limit))
    with get_conn() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [_decode_candidate(dict(row)) for row in rows]


def recent_candidate_events(user_id: str, limit: int = 20) -> list[str]:
    with get_conn() as connection:
        rows = connection.execute(
            """SELECT observed_event FROM event_candidates
            WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
            (str(user_id), int(limit)),
        ).fetchall()
    return [row["observed_event"] for row in rows]


def _decode_candidate(item: dict[str, Any]) -> dict[str, Any]:
    for source, target in (
        ("evidence_message_ids", "evidence_message_ids"),
        ("evidence_snapshot_json", "evidence_snapshot"),
        ("activated_schemas_json", "activated_schemas"),
        ("emotions_json", "emotions"),
        ("proposed_belief_changes_json", "proposed_belief_changes"),
    ):
        item[target] = json.loads(item[source] or "[]")
    item["is_turning_point"] = bool(item["is_turning_point"])
    return item


def promote_candidate_to_memory(candidate_id: int, *, manual: bool = False) -> int:
    """预留正式晋升接口。影子模式下只能显式 manual=True 调用。"""
    if not manual:
        raise PermissionError("影子模式禁止自动晋升候选事件")
    candidate = get_event_candidate(candidate_id)
    if not candidate:
        raise ValueError(f"候选事件不存在: {candidate_id}")
    with get_conn() as connection:
        cursor = connection.execute(
            """INSERT INTO episodic_memories
            (occurred_at, summary, emotion, importance, source, tags, created_at,
             user_id, candidate_id, raw_event, meaning_at_time, current_meaning,
             memory_strength, accessibility, emotional_charge, relationship_impact,
             belief_impact, stability, memory_half_life_days, emotion_half_life_days,
             is_turning_point, evidence_message_ids, evidence_snapshot_json,
             activated_schemas_json)
            VALUES (?, ?, ?, ?, 'conversation', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate["created_at"],
                candidate["observed_event"],
                json.dumps(candidate["emotions"], ensure_ascii=False),
                max(1, min(5, round(candidate["importance_score"] * 5))),
                datetime.now().isoformat(),
                candidate["user_id"],
                candidate["id"],
                candidate["observed_event"],
                candidate["subjective_meaning"],
                candidate["subjective_meaning"],
                candidate["importance_score"],
                candidate["importance_score"],
                candidate["emotional_intensity"],
                candidate["relationship_weight"],
                0.0,
                0.85 if candidate["is_turning_point"] else 0.45,
                720.0 if candidate["is_turning_point"] else 90.0,
                35.0 if candidate["is_turning_point"] else 14.0,
                int(candidate["is_turning_point"]),
                json.dumps(candidate["evidence_message_ids"], ensure_ascii=False),
                json.dumps(candidate.get("evidence_snapshot", []), ensure_ascii=False),
                json.dumps(candidate["activated_schemas"], ensure_ascii=False),
            ),
        )
        memory_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE event_candidates SET status = 'promoted' WHERE id = ?",
            (int(candidate_id),),
        )
        connection.commit()
    return memory_id


def _get_oldest_messages(user_id: str, limit: int) -> list[dict]:
    with get_conn() as connection:
        rows = connection.execute(
            "SELECT id, role, content FROM messages WHERE user_id = ? ORDER BY id ASC LIMIT ?",
            (str(user_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def _replace_summary_and_delete_messages(user_id: str, summary: str, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with get_conn() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO summaries (user_id, summary, updated_at) VALUES (?, ?, ?)",
            (str(user_id), summary, datetime.now().isoformat()),
        )
        connection.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
        connection.commit()


async def maybe_compress(user_id: str) -> None:
    count = get_message_count(user_id)
    if count <= MAX_MESSAGES:
        return
    messages = _get_oldest_messages(user_id, count - KEEP_MESSAGES)
    if not messages:
        return
    dialog = "\n".join(
        f"{'用户' if item['role'] == 'user' else '雪'}：{item['content']}" for item in messages
    )
    old_summary = get_summary(user_id)
    prompt = f"""把以下旧摘要与新对话整合为简洁的长期对话摘要。
只保留明确事实、约定、关系变化和重要共同经历；不要推测，不要加入原文没有的内容。

旧摘要：{old_summary or '无'}
新对话：
{dialog}"""
    try:
        summary = await complete(prompt, max_tokens=350, background=True, timeout=45)
        _replace_summary_and_delete_messages(user_id, summary, [item["id"] for item in messages])
        logger.info(f"用户 {user_id} 的对话摘要已更新")
    except Exception as exc:
        logger.error(f"压缩对话摘要失败: {exc}")


def add_episodic_memory(
    summary: str,
    *,
    emotion: str = "",
    importance: int = 1,
    source: str = "conversation",
    tags: list[str] | None = None,
    occurred_at: str | None = None,
) -> None:
    """兼容旧调用；新事件应先进入候选区，再显式晋升。"""
    with get_conn() as connection:
        connection.execute(
            """INSERT INTO episodic_memories
            (occurred_at, summary, emotion, importance, source, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                occurred_at or datetime.now().isoformat(),
                summary.strip(),
                emotion.strip(),
                max(1, min(int(importance), 5)),
                source,
                ",".join(tags or []),
                datetime.now().isoformat(),
            ),
        )
        connection.commit()


driver = get_driver()


@driver.on_startup
async def _initialize_memory() -> None:
    init_db()

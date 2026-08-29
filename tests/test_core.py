from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

nonebot.init(_env_file=None, log_level="WARNING", ai_provider="lmstudio")

from core.life_state import get_current_state
from core.llm import parse_json
import core.llm as llm
import core.life_engine as life_engine
import core.life_state as life_state
import core.memory as memory
import plugins.ai_chat as ai_chat
from core.appraisal import calculate_candidate
from core.decay import cue_reactivation, decay_toward_baseline, half_life_factor
from core.memory import DB_PATH, get_active_schemas, init_db
from core.persona import build_persona_prompt, get_relationship, load_persona
from core.world import build_world_prompt, load_world


def make_group_event(*, to_me: bool = False) -> GroupMessageEvent:
    message = Message("测试群消息")
    return GroupMessageEvent(
        time=0,
        self_id=10000,
        post_type="message",
        sub_type="normal",
        user_id=20000,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message="测试群消息",
        font=0,
        sender=Sender(user_id=20000, nickname="测试用户"),
        group_id=30000,
        to_me=to_me,
    )


class GroupReplyRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_reply_master_switch(self) -> None:
        with (
            patch.object(ai_chat, "GROUP_CHAT_ENABLED", False),
            patch.object(ai_chat, "GROUP_AT_ONLY", False),
        ):
            self.assertFalse(await ai_chat._is_group_message(make_group_event()))

        with (
            patch.object(ai_chat, "GROUP_CHAT_ENABLED", True),
            patch.object(ai_chat, "GROUP_AT_ONLY", False),
        ):
            self.assertTrue(await ai_chat._is_group_message(make_group_event()))

    async def test_group_at_only_switch(self) -> None:
        with (
            patch.object(ai_chat, "GROUP_CHAT_ENABLED", True),
            patch.object(ai_chat, "GROUP_AT_ONLY", True),
        ):
            self.assertFalse(await ai_chat._is_group_message(make_group_event()))
            self.assertTrue(await ai_chat._is_group_message(make_group_event(to_me=True)))


class CoreTests(unittest.TestCase):
    def test_persona_schema_and_relationship_migration(self) -> None:
        persona = load_persona()
        self.assertEqual(persona["schema_version"], 3)
        self.assertEqual(persona["identity"]["name"], "雪")
        self.assertTrue(persona["core_personality"]["contradictions"])
        self.assertEqual(get_relationship("unknown-test-user")["relationship"], "陌生人")

    def test_prompt_retrieves_relevant_memory(self) -> None:
        prompt = build_persona_prompt("test-user", "你今天要不要喝咖啡？")
        self.assertIn("苦咖啡", prompt)
        self.assertIn("只把上面的核心设定", prompt)
        self.assertLess(prompt.count("- "), 6)

    def test_world_canon_is_injected(self) -> None:
        world = load_world()
        prompt = build_world_prompt()
        self.assertEqual(world["schema_version"], 1)
        self.assertEqual(world["law_and_adulthood"]["legal_adulthood_age"], 16)
        self.assertIn("栖河镇", prompt)
        self.assertIn("临川市", prompt)
        self.assertIn("不存在被收养经历", prompt)
        self.assertIn("不存在已经被证实的魔法或超能力", prompt)

    def test_persona_has_no_retired_adoption_canon(self) -> None:
        persona = load_persona()
        serialized = str(persona)
        for retired_term in ("养父", "养母", "收养", "老夫妇"):
            self.assertNotIn(retired_term, serialized)
        self.assertEqual(persona["identity"]["actual_age"], 16)
        self.assertIn("没有因此原谅父亲", serialized)

    def test_json_parser_accepts_fenced_json(self) -> None:
        self.assertEqual(parse_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(
            parse_json('{"score": +0.5, // model note\n "items": [1, 2,],}'),
            {"score": 0.5, "items": [1, 2]},
        )
        self.assertEqual(
            parse_json('先举例 {坏格式}，最终结果是：{"ok": true, "items": []}'),
            {"ok": True, "items": []},
        )

    def test_life_state_has_time_appropriate_fallback(self) -> None:
        original_state_file = life_state.LIFE_STATE_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            life_state.LIFE_STATE_FILE = Path(temp_dir) / "missing-life-state.json"
            try:
                state = get_current_state(datetime(2026, 7, 31, 2, 0))
            finally:
                life_state.LIFE_STATE_FILE = original_state_file
        self.assertIn("睡", state)

    def test_database_schema_is_ready(self) -> None:
        init_db()
        connection = sqlite3.connect(DB_PATH)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("messages", tables)
        self.assertIn("summaries", tables)
        self.assertIn("episodic_memories", tables)
        self.assertIn("psychological_schemas", tables)
        self.assertIn("emotional_state", tables)
        self.assertIn("event_candidates", tables)
        self.assertIn("belief_history", tables)
        self.assertIn("appraisal_runs", tables)

    def test_decay_uses_half_life_and_baseline(self) -> None:
        self.assertAlmostEqual(half_life_factor(10, 10), 0.5)
        self.assertAlmostEqual(decay_toward_baseline(0.9, 0.5, 10, 10), 0.7)
        reactivated = cue_reactivation(
            accessibility=0.2,
            topic_similarity=0.9,
            schema_relevance=0.8,
            relationship_relevance=0.8,
            emotion_similarity=0.5,
        )
        self.assertGreater(reactivated, 0.6)

    def test_short_sentence_can_be_a_turning_point(self) -> None:
        schemas = get_active_schemas()
        raw = {
            "event_type": "relationship_event",
            "observed_event": "用户在其他人指责雪时明确说自己站在雪这边",
            "subjective_meaning": "原来有人会在需要付出代价时选择维护我",
            "evidence_message_ids": [101],
            "activated_schemas": [
                {
                    "schema_id": "fear_rejection",
                    "activation": 0.98,
                    "direction": "contradict",
                    "reason": "直接打破被嫌弃的预期",
                }
            ],
            "emotions": [{"name": "安心", "intensity": 0.9}],
            "proposed_belief_changes": [
                {"schema_id": "fear_rejection", "delta": -0.12, "reason": "被坚定维护"}
            ],
            "scores": {
                "confidence": 0.96,
                "emotional_intensity": 0.91,
                "relationship_weight": 0.88,
                "vulnerability": 0.86,
                "social_cost": 0.82,
                "identity_relevance": 0.84,
                "expectation_violation": 0.94,
            },
        }
        candidate = calculate_candidate(
            raw,
            user_id="test",
            valid_message_ids={101},
            schemas=schemas,
        )
        self.assertTrue(candidate["is_turning_point"])
        self.assertEqual(candidate["status"], "shadow")
        self.assertGreater(candidate["importance_score"], 0.7)

    def test_invalid_evidence_is_rejected(self) -> None:
        candidate = calculate_candidate(
            {
                "observed_event": "没有真实证据的事件",
                "evidence_message_ids": [999],
                "scores": {"confidence": 1.0, "emotional_intensity": 1.0},
            },
            user_id="test",
            valid_message_ids={1, 2},
            schemas=get_active_schemas(),
        )
        self.assertEqual(candidate["status"], "rejected")


class PsychologyStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = memory.DB_PATH
        memory.DB_PATH = Path(self.temp_dir.name) / "test.db"
        memory.init_db()

    def tearDown(self) -> None:
        memory.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_candidate_round_trip_and_manual_promotion(self) -> None:
        candidate = {
            "user_id": "test-user",
            "event_type": "relationship_event",
            "observed_event": "用户明确站在雪这边",
            "subjective_meaning": "有人愿意维护我",
            "evidence_message_ids": [1],
            "evidence_snapshot": [{"id": 1, "role": "user", "content": "我站你这边"}],
            "activated_schemas": [
                {"schema_id": "fear_rejection", "activation": 0.9, "direction": "contradict"}
            ],
            "emotions": [{"name": "安心", "intensity": 0.8}],
            "proposed_belief_changes": [],
            "confidence": 0.9,
            "emotional_intensity": 0.8,
            "relationship_weight": 0.8,
            "vulnerability": 0.8,
            "social_cost": 0.7,
            "identity_relevance": 0.7,
            "expectation_violation": 0.9,
            "schema_activation": 0.9,
            "breakthrough_score": 0.648,
            "importance_score": 0.75,
            "is_turning_point": True,
            "status": "shadow",
            "rejection_reason": "",
            "expires_at": None,
        }
        candidate_id = memory.save_event_candidate(candidate)
        loaded = memory.get_event_candidate(candidate_id)
        self.assertEqual(loaded["evidence_snapshot"][0]["content"], "我站你这边")
        with self.assertRaises(PermissionError):
            memory.promote_candidate_to_memory(candidate_id)
        memory_id = memory.promote_candidate_to_memory(candidate_id, manual=True)
        self.assertGreater(memory_id, 0)
        self.assertEqual(memory.get_event_candidate(candidate_id)["status"], "promoted")


class LLMTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_deepseek_background_call_disables_reasoning_structurally(self) -> None:
        response = type(
            "FakeResponse",
            (),
            {
                "is_error": False,
                "raise_for_status": lambda self: None,
                "json": lambda self: {
                    "choices": [{"message": {"content": '{"ok":true}'}}]
                },
            },
        )()
        post = AsyncMock(return_value=response)
        with (
            patch("core.llm._get_client", AsyncMock(return_value=object())),
            patch("core.llm._post_with_retry", post),
        ):
            result = await llm._call_openai_compatible(
                "https://api.deepseek.com/chat/completions",
                "",
                "deepseek-v4-flash",
                [{"role": "user", "content": "只返回JSON"}],
                "",
                30,
                10,
                no_think=True,
                json_mode=True,
            )
        payload = post.await_args.kwargs["json"]
        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertFalse(payload["messages"][0]["content"].startswith("/no_think"))


class AutonomousLifeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_state_file = life_state.LIFE_STATE_FILE
        self.original_db_file = life_engine.LIFE_DB_FILE
        life_state.LIFE_STATE_FILE = Path(self.temp_dir.name) / "life_state.json"
        life_engine.LIFE_DB_FILE = Path(self.temp_dir.name) / "life_timeline.db"

    def tearDown(self) -> None:
        life_state.LIFE_STATE_FILE = self.original_state_file
        life_engine.LIFE_DB_FILE = self.original_db_file
        self.temp_dir.cleanup()

    async def test_cycle_uses_ai_choice_but_program_applies_consequence(self) -> None:
        state = copy.deepcopy(life_state.DEFAULT_LIFE_STATE)
        state["hunger"] = 0.82
        state["last_tick_at"] = "2026-08-01T16:00:00"
        life_state.save_dynamic_state(state)
        now = datetime(2026, 8, 1, 18, 0)
        meal = next(
            item
            for item in life_engine.build_event_candidates(state, now)
            if item["id"] == "meal_needed"
        )
        with (
            patch("core.life_engine.select_event", return_value=meal),
            patch(
                "core.life_engine.decide_action",
                AsyncMock(
                    return_value={
                        "chosen_action": "cook_simple",
                        "inner_thought": "先给自己做点吃的。",
                        "reason": "已经很饿，而且家里有食材。",
                    }
                ),
            ),
        ):
            record = await life_engine.run_life_cycle(now=now)

        updated = life_state.load_dynamic_state()
        self.assertIsNotNone(record)
        self.assertEqual(updated["inventory"]["simple_ingredients"], 2)
        self.assertLess(updated["hunger"], 0.5)
        self.assertEqual(len(life_engine.list_life_events()), 1)

    def test_unaffordable_choice_is_rejected_by_program(self) -> None:
        state = copy.deepcopy(life_state.DEFAULT_LIFE_STATE)
        state["cash_balance"] = 0
        now = datetime(2026, 8, 1, 18, 0)
        event = next(
            item
            for item in life_engine.build_event_candidates(state, now)
            if item["id"] == "stray_cat"
        )
        with self.assertRaises(ValueError):
            life_engine.resolve_event(
                event,
                {"chosen_action": "buy_cat_food", "inner_thought": "", "reason": ""},
                state,
                now,
            )

    def test_life_timeline_has_no_user_identity_column(self) -> None:
        life_engine.init_life_timeline()
        connection = sqlite3.connect(life_engine.LIFE_DB_FILE)
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(life_events)").fetchall()
            }
        finally:
            connection.close()
        self.assertNotIn("user_id", columns)
        self.assertIn("consequence", columns)


if __name__ == "__main__":
    unittest.main()

# 心理成长系统路线与接口

## 当前已实现

- `core.appraisal.analyze_messages`：只分析，不写数据库。
- `core.appraisal.calculate_candidate`：纯程序评分、证据校验和心理转折判断。
- `core.appraisal.maybe_appraise_conversation`：增量检查点与影子模式写入。
- `core.decay`：半衰期、基线回归、记忆有效值、线索唤醒和信念变化限幅。
- `core.memory.get_active_schemas`：读取经过时间回弹计算的心理图式。
- `core.memory.get_effective_emotional_state`：读取经过时间淡化的当前情绪。
- `core.memory.promote_candidate_to_memory`：显式手动晋升接口。

## 数据不变量

1. `config/persona.json` 是作者事实层，自动系统不可写。
2. `messages` 与候选事件中的 `evidence_snapshot_json` 是事件证据。
3. `episodic_memories.raw_event` 保存事实；`current_meaning` 才允许重新解释。
4. AI只能提交候选；程序必须验证消息编号、图式ID、范围、阈值和重复。
5. 影子模式下不得调用自动晋升、情绪写入或信念写入。

## 下一阶段接口

### 正式记忆晋升

当前已有：

```python
promote_candidate_to_memory(candidate_id, manual=True)
```

未来增加自动晋升时，应新建策略函数，而不是绕过该入口：

```python
decide_promotion(candidate, repeated_evidence, human_rules) -> PromotionDecision
```

### 情绪更新

计划增加：

```python
apply_emotional_effect(candidate_id) -> EmotionalStateChange
```

该函数必须保存修改前、修改后、原因和恢复半衰期。

### 信念更新

计划增加：

```python
propose_belief_changes(candidate_id) -> list[BeliefProposal]
apply_belief_change(proposal, approved_policy) -> BeliefHistory
```

所有变化必须经过 `core.decay.safe_belief_delta` 限幅并写入 `belief_history`。

### 正式记忆检索

计划增加：

```python
retrieve_episodic_memories(query, user_id, current_state, limit=4)
```

排序由当前可访问性、话题相似、心理图式、关系和情绪线索共同决定。

### 重新巩固

计划增加：

```python
reconsolidate_memories(now) -> ConsolidationReport
```

只能合并重复模式、延长半衰期和更新 `current_meaning`，不能修改原始事实。

## 从影子模式转入正式模式前的条件

- 至少积累30到100条评价运行记录。
- 抽查普通寒暄、承诺、争吵、玩笑、支持和告别场景。
- 重大事件漏判率与普通事件误判率达到可接受程度。
- 无不存在的证据消息、未知心理图式或事实编造。
- 确认评分阈值后，再单独实现正式晋升策略。


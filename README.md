# 雪：QQ 角色机器人

基于 NoneBot2 + OneBot V11 的单角色聊天机器人。角色设定、关系记忆、生活状态与模型调用彼此分层，避免同一份人设散落在多个插件中。

项目能力、架构、隐私边界与后续路线见 [项目总结](项目总结.md)。

定时主动消息请复制 `scheduler_jobs.example.txt` 为 `scheduler_jobs.txt` 后填写；实际文件包含 QQ 号，已从 Git 排除。

## 当前结构

```text
aiqqbot/
├─ bot.py
├─ config/
│  ├─ world.json            # 社会、法律、地点与家庭背景的世界硬规则
│  └─ persona.json          # 雪的人格、经历与表达方式
├─ data/
│  ├─ bot.db                # 对话摘要与后天经历
│  ├─ life_state.json       # 雪当前的余额、需要、物品和人生线状态
│  ├─ life_timeline.db      # 不依赖任何用户的自主人生时间线
│  ├─ relationships.json    # 雪与不同用户的关系档案
│  └─ schedule_today.json   # 带日期的当日生活状态
├─ core/
│  ├─ llm.py                # 主模型/幕后模型统一客户端
│  ├─ persona.py            # 人格提示词、相关经历选择、关系更新
│  ├─ world.py              # 世界观读取与提示词压缩
│  ├─ memory.py             # SQLite 对话、记忆与心理数据
│  ├─ appraisal.py          # AI主观评价 + 程序证据裁决
│  ├─ decay.py              # 情绪、记忆和信念的时间淡化算法
│  ├─ life_state.py         # 当前生活状态
│  ├─ life_engine.py        # 世界情境、AI选择、程序裁决与人生时间线
│  └─ intent.py             # 提醒与结束对话识别
└─ plugins/
   ├─ ai_chat/              # QQ 消息入口
   ├─ scheduler.py          # 唯一 APScheduler 实例
   ├─ autonomous_life.py    # 真实时间自主生活循环
   └─ daily_schedule.py     # 每日生活日程与主动消息
```

## 自主生活系统

默认启用真实时间自主生活。它与用户聊天完全分开：世界根据余额、时间、饥饿、物品和未完成人生线提出情境；AI只能从程序确认可行的行动中选择；最终余额、物品与后果由程序裁决。

第一版包含三条连续人生线：找工作与经济、楼下的流浪橘猫、父母与小木鱼。普通吃饭、采购、整理房间等生活事件用于维持真实感。每天默认最多发生6件事，机器人停机后最多补算3件，不会把停机数日膨胀成几十段经历。

```env
LIFE_ENGINE_ENABLED=true
LIFE_CYCLE_MINUTES=120
LIFE_MAX_EVENTS_PER_DAY=6
LIFE_OFFLINE_MAX_EVENTS=3
```

查看当前状态和人生时间线：

```bash
python scripts/review_life.py
```

显式要求雪立刻自主经历一次事件（会调用幕后模型并改变真实生活状态）：

```bash
python scripts/review_life.py --run
```

启用自主生活后，旧的AI强制日程不再生成；关闭 `LIFE_ENGINE_ENABLED` 时仍可完整回退到原日程系统。

## 世界观与角色资料原则

- `config/world.json` 是社会规则、地点和家庭事实的唯一来源。
- `config/persona.json` 保存雪的人格与个人经历；作者设定不能被聊天自动覆盖。
- 世界硬规则会自动注入角色提示词，模型不得把兽征解释为魔法，也不得改写16岁成年、亲生父母和当前独居等事实。
- `canon_memories` 是雪确定发生过的人生经历，并带有影响与主题标签。
- 每次聊天只选择与当前话题最相关的少量经历，不再注入全部记忆。
- `data/relationships.json` 只保存雪与用户之间明确发生的事情。
- `data/bot.db` 的 `episodic_memories` 表用于以后记录雪后天真实经历，不替代作者设定。

## 心理事件影子模式

当前已经启用事件评价器，但处于安全的影子模式：

```text
最近对话
→ AI提出带消息证据的候选事件
→ 程序验证证据、去重并计算重要性
→ 保存到 event_candidates
→ 不影响回复、不修改正式记忆、不修改人格
```

事件评价在回复发送后异步运行，不增加用户等待时间。默认累计6条新消息后评价一次，结束对话时会立即评价。

查看最近候选事件：

```bash
python scripts/review_candidates.py --limit 20
```

查看某种状态：

```bash
python scripts/review_candidates.py --status shadow
python scripts/review_candidates.py --status below_threshold
python scripts/review_candidates.py --status rejected
```

代码已预留正式记忆晋升接口。观察期内不建议使用；确实确认某条候选正确时，可以显式执行：

```bash
python scripts/review_candidates.py --promote 候选ID --confirm
```

## 安装与启动

Windows 下可直接双击根目录的 `一键启动.bat`，它会同时启动机器人和相邻目录中的 `NapCat.Shell`。首次启动时会自动创建 `.venv` 并安装依赖。

仅检查环境而不启动：

```powershell
.\一键启动.bat -Check
```

也可以手动启动：

```bash
python -m venv venv
pip install -r requirements.txt
python bot.py
```

NapCat 反向 WebSocket 地址通常设置为：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

## 模型配置

配置仍保存在 `.env`，原有变量保持兼容：

```env
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的主模型Key
DEEPSEEK_MODEL=deepseek-chat

# 可选：日程、摘要和意图识别使用独立的 OpenAI 兼容接口
BACKGROUND_API_URL=https://api.deepseek.com/chat/completions
BACKGROUND_API_KEY=你的副模型Key
BACKGROUND_MODEL=deepseek-chat

# 群聊开关
GROUP_CHAT_ENABLED=true
# true：只响应 @机器人；false：响应所有普通群消息
GROUP_AT_ONLY=false

# 心理事件评价：当前必须保持影子模式
APPRAISAL_SHADOW_MODE=true
APPRAISAL_EVERY=6
APPRAISAL_WINDOW=14
APPRAISAL_MAX_CANDIDATES=3
LLM_MAX_CONCURRENCY=4
```

如果不设置完整的幕后模型地址和模型名，幕后任务会正确回退到当前主模型。Claude、DeepSeek 与 LM Studio 的选择逻辑统一在 `core/llm.py`。

## 修改雪的人格

只编辑 `config/persona.json`：

- `core_personality`：信念、需要、恐惧、矛盾和应对方式。
- `speech_style`：表达习惯与限制。
- `boundaries`：关系和行为边界。
- `canon_memories`：作者确定的人生经历。
- `psychological_schemas`：雪已有的心理图式、强度、敏感度与稳定度。
- `growth_policy`：候选阈值、心理转折条件与单次变化上限。

不要把人设重新写进 `.env`；`BOT_SYSTEM_PROMPT` 只应保存通用行为规则。

如果要修改时代、兽征者规则、成年法律、栖河镇、临川市或雪当前的客观生活条件，请编辑 `config/world.json`，不要在 `persona.json` 里复制另一份相互冲突的世界观。

## 测试

以下测试完全离线，不调用模型 API：

```bash
python -m compileall -q bot.py core plugins
python -m unittest discover -s tests -v
python scripts/smoke_api.py
python scripts/smoke_api.py --main
python scripts/smoke_appraisal.py
```

前两条完全离线；后三条分别测试幕后模型、主聊天模型和完整事件评价 API。

## 数据与备份

停止机器人后备份整个 `data/` 目录即可保存聊天和关系进度。`.env` 包含密钥，不应上传到公开仓库。

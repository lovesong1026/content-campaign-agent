# 轻量 TodoList

Agent 继续采用原生工具调用的 ReAct 循环。TodoList 保存计划及执行进度，供模型参考；模型选择下一步动作，执行器根据真实结果更新状态。没有增加模型调用、独立 Agent 或收费的计划工具。

## 当前能力

- 每个新任务初始化“检索 → 生成 → 估价 → 保存”的建议计划。
- 每轮 context 包含 `todo_list`、当前 `plan_revision`、修订摘要 `plan_changes` 和待处理观察 `todo_replan`。
- 模型可在工具参数中提供 `remaining_steps`，提出本次动作之后的工具顺序。
- 执行器维护待办状态，绑定实际 `call_id`，保存失败与重试尝试，不覆盖已执行记录。
- 任务结果、本地快照和 Langfuse 根节点输出均包含待办及修订历史；规划 generation 的输入也包含对应轮次的计划状态。

实现集中在 `src/content_campaign_agent/todos.py`；规划提示版本为 `planner-v2`。

## 提案规则

| `remaining_steps` | 行为 |
| --- | --- |
| 省略或 `null` | 保留尚未执行的后续待办 |
| `[]` | 将后续待办标记为 `skipped`，原因 `PLAN_REVISED` |
| 工具名数组 | 按模型提案调整顺序，可增加重试或重写动作；未保留的待办标记为跳过 |

提案只允许四类工具名，最多 12 项，不能提交完成状态。提案在本次动作通过参数、依赖、预算及时间门禁并开始实际执行时生效；被门禁阻止的动作不能借此改写后续计划。仅列入待办不会自动执行，也不会绕过工具依赖。模型选择了待办之外的动作时，执行器记录计划调整，并照常验证该动作。

每条待办具有任务内稳定的 `todo_id`。没有被移除的待执行条目尽量保留原 ID；已结束的尝试保留其状态，重试创建新条目和新 ID。模型清空待办后仍可提出新的有效动作，完成与否以实际保存结果为准。

## 状态和历史

| 状态 | 依据 |
| --- | --- |
| `pending` | 已计划，尚未开始 |
| `running` | 实际工具执行开始，已进入计费边界 |
| `succeeded` | 收到成功的工具结果 |
| `failed` | 实际执行失败；取消或中断时可带 `outcome_unknown=true` |
| `blocked` | 参数、依赖或约束检查拒绝执行，不产生该次工具费用 |
| `skipped` | 计划修订移除了该条目，或任务已结束而该条目未执行 |

`steps` 保留实际工具请求与观察，`todo_list` 表示计划条目及各次尝试。两者以 `todo_id` / `call_id` 关联。多工具批次中因前项失败而跳过的调用，仅作为未执行的 `step` 记录，`todo_id=null`；它不会提前把仍需执行的待办标为完成或取消。

`plan_revisions` 是追加保存的计划快照，记录轮次、简短理由、触发动作及来源观察。初始化、计划调整、响应故障或超预算的重规划，以及任务结束时记录修订；普通进度变化记为 `todo_updated` 事件，不必每次创建一个新计划版本。模型每轮接收精简修订摘要，完整历史保存在任务和 Trace 中。

例如：估价工具成功返回“超预算”，该待办仍是 `succeeded`，同时保存 `within_budget=false`。下一轮模型选择重写，新增“生成 → 估价”，并保留尚未执行的保存条目。修订的 `source_observation` 关联到实际超预算估算。失败重试同样保留原失败条目及费用，由模型决定是否重试。

任务停止时，剩余 `pending` 标为跳过；尚在执行的条目标为失败并注明结果未知。失败后成功重试的任务仍保留失败历史，不能用“所有待办均成功”判断任务是否完成。旧任务快照不伪造回填，返回 `todo_tracking=not_recorded` 和空待办历史；同 ID 复用也不创建新的修订或追踪。

`pending_plan` 作为兼容字段保留，投影当前 `pending` / `running` 条目；任务结束后为空。

## 查看与验证

```bash
# 完全离线，复跑 33 组场景及待办一致性检查。
uv run python scripts/run_synthetic.py
# 针对计划语义、重试、取消、恢复及旧快照的测试。
uv run pytest tests/test_todos.py -q
# 在新目录执行一个演示，结果 JSON 包含 todo_list / plan_revisions。
LANGFUSE_ENABLED=false uv run campaign-agent run --mode mock --input examples/inputs/overbudget.json --scenario data/scenarios/fixed-clock.json --output-dir .runs/todo-demo
```

加入 TodoList 后全量 **93 项测试通过**，包括新增 9 项待办测试；33 组模拟场景新增了 ID 唯一、终态无运行中待办，以及完成状态有真实步骤支撑的检查。结果见 [JUnit](../examples/todos/test-results.xml) 和 [离线汇总](../examples/todos/synthetic-report.json)。

本轮另外运行了三组真实 DeepSeek 场景，并通过 Langfuse observations API 读回核查。以下为本轮结果；首版真实模型归档仍保留原有路径，不能混为同一次执行。

| 场景 | 结果 | 工具次数 | 工具费 / 方案费 / 总模拟费用 | 计划版本数 |
| --- | --- | ---: | --- | ---: |
| 正常 | 保存成功 | 4 | 4 / 1 / 5 | 2 |
| 超预算后重写 | 保存成功，修订关联超预算观察 | 6 | 7 / 1 / 8 | 3 |
| 脚本首次失败 | 保存成功，保留失败并重试 | 5 | 6 / 2 / 8 | 3 |

实际待办和完整修订快照收录于 [真实运行汇总](../examples/todos/real-report.json)。完整本地输入输出、模型及工具事件仍在本轮 `.runs/todos-real-*/` 中；汇总内附可访问的云端 Trace 链接，不依赖本机目录定位。云端已核查根节点的待办与修订、规划输入、提示版本、工具结果、模型用量及父子关系；未额外进行 UI 人工审阅或文案质量评分。

三组样本验证功能闭环，不代表 TodoList 已提高真实模型成功率。完整历史仍会增加上下文 Token；本次没有做上下文压缩或长期记忆。后续显式续跑实现及其离线验证如下，不与这三组真实模型记录混为同一次执行。

## 显式续跑

普通 `run` 的幂等行为不变；同 ID 再次提交不会隐式重试。对已停止、取消或中断的合法任务，使用原输出目录和相同运行模式：

```bash
uv run campaign-agent resume --task-id your-task-id --output-dir .runs
LANGFUSE_ENABLED=false uv run campaign-agent resume --mode mock --task-id your-task-id --output-dir .runs/offline
```

续跑入口为 `Agent.resume_task(task_id)`。CLI 先取得原输出目录写锁，再将遗留 `running` 快照标为中断；只有显式 `resume` 才重新进入模型循环。库调用者也应在持有写锁时先调用 `Agent.start()`，同一输出目录复用一个 Agent 实例，避免把遗留状态误当作仍在执行；不支持多个 Agent 实例共享写入该目录。

- 复用已确认的素材、当前方案及估算。只有生成失败时才需要重新生成；估算失败不重跑已成功生成，保存失败不重跑已成功估算。下一步仍由模型选择，不由执行器固定重试。
- 保持任务 ID、`run_id`、业务输入、账本、模拟起点及累计耗时；使用原材料、价格、工具费用/耗时及场景快照。API 连接和密钥使用当前配置，运行模式不能在真实与 Mock 之间切换。
- 原有失败、阻止、跳过和成功条目不重新打开；为剩余步骤追加新待办，记录 `TASK_RESUMED` 修订。旧快照只添加续跑后的计划，不伪造过去的待办。
- 续跑首轮的 context 带 `resume` 与 `previous_steps`，包含原结束原因、累计计数和历史真实工具观察。新会话不重放未闭合的工具调用协议。
- `resume_count` 和 `resume_history` 对外展示续跑次数及每次续跑前的原因、轮次、工具数和模拟时间；本地 Trace 追加 `task_resumed`，不重建或删除历史事件。
- 12 轮决策、12 次实际工具的上限按任务累计，不因续跑增加。预算或时间仍不足、执行额度耗尽时不能靠续跑绕过；需要调整业务输入时创建新任务。
- 成功或当前正在执行的任务返回 `reused=true, resumed=false`，不新增调用、费用或修订；同一 Agent 实例的并发续跑只启动一次。缺输入、任务不存在或模式不符时明确返回错误，不修改原任务。续跑启动快照持久化失败时不发布新的内存运行态，同一实例可在存储恢复后重新尝试。

### 保存中断的核对

工具已经开始但结果尚未确认时，保留原收费和耗时，历史尝试标记 `outcome_unknown=true`，账本保留 `unknown`。

若保存文件已写入但终态快照未落盘，续跑先核对整个交付文件与快照中的方案、估算、内容哈希、费用、模拟保存时刻及稳定保存 ID。完全一致时确认原调用成功并记录 `save_reconciled`，不再次请求模型、保存或收费；这属于对未知结果的确认，不是把真实失败改成成功。文件不存在时允许模型发起新的保存尝试；文件损坏或内容不符则返回 `SAVED_RESULT_CONFLICT`，不覆盖文件。其他结果未知的生成尝试没有跨服务幂等保证，重新执行仍消耗新的费用和时间。

### 费用下界与预算缺口

未取得估算时，`costs.plan_cost`、`costs.total_cost` 和 `costs.budget_gap` 仍为 `null`，不以预测冒充真实估算。结果额外提供：

- `gaps.minimum_total_cost`：累计工具费、剩余必要工具费用下界和最低方案费用之和。
- `gaps.minimum_total_budget_gap`：上述总费用下界超过预算的部分。

例如尚未执行工具、预算为 4 时，最短工具链需要 4，最低方案费用为 1，因此总费用下界为 5、预算至少差 1。下界不是可行方案报价；素材、内容规格和后续重试可能使实际费用更高。

新增离线回归见 [test_resume.py](../tests/test_resume.py)、[预算缺口测试](../tests/test_agent.py) 和 [CLI 测试](../tests/test_cli.py)，当前全量结果见 [JUnit](../examples/resume/test-results.xml)。未为本次新增能力重新调用真实模型或上传 Langfuse。

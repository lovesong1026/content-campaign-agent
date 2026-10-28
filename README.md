# 内容任务规划 Agent

基于 DeepSeek Flash 的异步单 Agent，根据主题、预算和方案交付截止时间检索素材、生成短视频脚本、估算成本并保存方案。模型观察真实工具结果并调整动作，程序维护费用、时间、状态及执行上限。

已实现交互式 CLI、四工具、真实模型与离线 Mock、轻量 TodoList、持久化、幂等、显式失败续跑、批量限流并发、本地 Trace 及可选 Langfuse 追踪。当前验证：141 项测试通过；原有真实模型 9 组、Mock 9 组及扩展离线模拟 33 组场景通过各自检查。TodoList 另完成 3 组真实模型与云端追踪核查；新增续跑、缺口输出和交互入口使用离线测试验证，未重新调用真实模型，证据及限制见 [评估报告](docs/03-evaluation.md)。

## 安装

支持 macOS / Linux，Python 3.12。安装 uv 可参考 [官方文档](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
git clone https://github.com/lovesong1026/content-campaign-agent.git
cd content-campaign-agent
uv sync --locked
```

本地已有 `.env` 时直接使用；首次克隆时复制模板并编辑密钥：

```bash
cp .env.example .env
```

```dotenv
DEEPSEEK_API_KEY=填入你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

`.env` 已由 Git 忽略。Mock 无需 DeepSeek 密钥；关闭 Langfuse 时不访问网络。精确依赖见 `uv.lock`；本次使用 Python 3.12.13、OpenAI SDK 3.40.0、Pydantic 2.14.1。

可选的 Langfuse 5.0.4 接入已完成：填写 `.env` 中的 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 后，普通 CLI 执行会上传追踪。Mock 启用遥测后也会上报工具链；需要完全离线时设置 `LANGFUSE_ENABLED=false`。配置和云端自检见 [Langfuse 指南](docs/06-langfuse.md)。

## 运行

### 交互式输入

填写 `.env` 中的 DeepSeek 密钥后，直接启动即可按提示输入，不需要准备 JSON 文件或业务参数：

```bash
uv run campaign-agent
```

等价的显式命令为 `uv run campaign-agent interactive`。先离线体验、不调用模型或上传追踪：

```bash
LANGFUSE_ENABLED=false uv run campaign-agent interactive --mode mock
```

启动后依次输入活动主题、预算和截止时间；截止时间留空时默认使用任务起点后 30 分钟。金额或时间无效时允许重填，执行时展示真实工具进度，完成后展示费用、历史步骤状态及保存位置，并继续等待下一项任务。

- 每次输入新主题都创建独立任务，即使主题相同也会重新执行、计费。
- 在主题提示处输入 `resume`，再输入原任务 ID，可按原输出目录和模式续跑；任务 ID 留空时使用本会话上一次任务。
- 任意输入阶段输入 `exit` 或按 Ctrl+D 正常退出；Ctrl+C 取消当前操作并退出，已确认结果保留。
- `--output-dir`、`--scenario`、`--mode` 等选项放在 `interactive` 后面。整个会话持有输出目录写锁，不要同时从另一个进程写入该目录。
- 交互模式输出适合人阅读；自动化脚本继续使用下方一次性命令的 JSON 输出。

### 参数与文件输入

真实模型，使用固定模拟起点避免示例随当前日期失效：

```bash
uv run campaign-agent run --input examples/inputs/normal.json --scenario data/scenarios/fixed-clock.json
uv run campaign-agent show --task-id demo-normal
```

直接传参；省略时区时按北京时间解释：

```bash
uv run campaign-agent run --theme "春季新品推广" --budget 20 --deadline "2030-01-01T10:30:00+08:00"
```

离线与批量并发：

```bash
uv run campaign-agent run --mode mock --input examples/inputs/normal.json --output-dir .runs/offline
uv run campaign-agent batch --mode mock --input examples/inputs/batch.json --concurrency 3 --output-dir .runs/batch
```

超预算与一次性工具失败：

```bash
uv run campaign-agent run --input examples/inputs/overbudget.json --scenario data/scenarios/fixed-clock.json
uv run campaign-agent run --theme "春季新品推广" --budget 20 --deadline "2030-01-01T10:30:00+08:00" --scenario data/scenarios/tool-failure.json
```

同一输出目录中，同 ID、同输入的 `run` 返回已有任务，不重复执行或收费；同 ID 改输入返回冲突。未完成任务可显式续跑，必须使用原输出目录和原模式：

```bash
uv run campaign-agent resume --task-id your-task-id
LANGFUSE_ENABLED=false uv run campaign-agent resume --mode mock --task-id your-task-id --output-dir .runs/offline
```

`resume` 复用已确认素材、方案和估算，将历史错误交给模型重新决策，不重置费用、模拟时间或 12 轮/12 次工具上限。成功或正在执行的任务只返回已有结果；缺失输入需补全后使用新 ID。续跑沿用原任务的材料、价格、耗时与场景快照；真实模式仍使用当前模型连接配置和密钥。保存结果未知时先校验已有交付文件，一致则确认原保存，不一致则停止且不覆盖。完整语义见 [续跑说明](docs/07-todolist.md#显式续跑)。

需要改变业务输入或从头执行时使用新 ID 或新 `--output-dir`。归档目录 `examples/real/`、`examples/mock/` 用于评审，运行记录默认写入 `.runs/tasks/<任务ID哈希>/`。

结果 JSON 包含计划及步骤状态、真实工具结果、费用账本、模拟时间、保存结果或未完成原因、Trace 路径和续跑历史。未取得估算时实际方案费用保持未知，`gaps.minimum_total_cost` 与 `gaps.minimum_total_budget_gap` 单独给出费用下界及最低预算缺口；例如预算 4 时，下界为 5、缺口至少为 1，并不伪造估算。`show` 不发起模型请求。单任务退出码：成功 0、未完成 1、输入不足、冲突或续跑模式不符 2；批量有未成功任务返回 1。结果写 stdout，诊断写 stderr。

`todo_list` 展示整体进度与历史尝试，`plan_revisions` 保留修订快照。模型每轮读取计划并可提出调整，执行器按真实结果更新状态；失败重试保留原失败条目和费用。字段语义、旧快照兼容及运行示例见 [TodoList 指南](docs/07-todolist.md)。

## 业务约定

- 总模拟费用为累计工具费用加当前方案预计费用；失败、重试和替代方案不清空历史费用。真实 API 用量单列，实际账单未知时不虚报货币费用。
- 截止时间约束方案保存，按工具耗时表累计模拟时间，不实际等待几分钟；网络耗时不混入模拟时间。
- 最多 12 轮决策、12 次实际工具执行。多调用逐个串行检查，失败、拒绝或超限后交回模型重新决定。
- 独立任务默认最多 3 个并发；模型请求有独立限流，同一输出目录同时只允许一个写入进程。
- 使用合成素材和价格，只生成方案及脚本，不执行真实拍摄或发布。

规则见 [需求](docs/01-requirements.md)，模块和接口见 [开发方案](docs/02-solution.md)。

## 测试与证据

```bash
uv run pytest -q --junitxml=examples/test-results.xml
uv run ruff check .
uv run ruff format --check .
uv build
```

重新执行场景并导出新时间戳目录；真实模式产生模型 API 用量，脚本拒绝覆盖已有证据：

```bash
uv run python scripts/run_examples.py --mode mock
uv run python scripts/run_examples.py --mode real
uv run python scripts/run_synthetic.py
```

- [真实运行汇总](examples/real/report.json)：9 组场景，覆盖五类必验场景及额外边界。
- [超预算 Trace](examples/real/overbudget/trace.jsonl)：估算 12 → 调整后估算 8 → 额外估算后最终 9，全部如实计费。
- [工具失败 Trace](examples/real/tool-failure/trace.jsonl)：模型观察故障后选择重试并保存。
- [Mock 汇总](examples/mock/report.json)：只证明受控工程流程，不能证明真实模型决策能力。
- [扩展模拟数据集](examples/synthetic/README.md)：33 组边界、输入校验、故障和重规划用例，附预期结果及离线复跑命令。
- [Langfuse 云端核查](examples/langfuse/report.json)：Mock 5 个节点、真实 DeepSeek 10 个节点，包含完整 generation 计数、父子关系及 Token 验证。
- [Langfuse 接入后的全量测试](examples/langfuse-test-results.xml)：84 项通过；原有 JUnit 文件保留首版记录。
- [TodoList 验证](examples/todos/real-report.json)：3 组真实模型与 Langfuse 核查；[TodoList 接入测试](examples/todos/test-results.xml) 为当时 93 项通过。
- [续跑接入测试](examples/resume/test-results.xml)：当时 126 项通过，覆盖预算缺口、四工具失败续跑、中断恢复、原规则快照、幂等、并发、执行上限及保存落盘核对。
- [当前全量测试](examples/interactive/test-results.xml)：141 项通过，新增交互输入重填、默认时间、连续任务、退出清理、实时进度及会话内续跑验证。
- [测试结果](examples/test-results.xml)：依赖、费用、版本、保存、时间、上限、幂等及并发隔离。

## 评审入口

| 材料 | 内容 |
| --- | --- |
| [题目归档](docs/00-assignment.md) | 开发前任务说明及交付清单 |
| [需求](docs/01-requirements.md) | 用户目标、业务规则及 A01—A12 |
| [开发方案](docs/02-solution.md) | 模型、工具、异步、存储及工程取舍 |
| [评估](docs/03-evaluation.md) | 实际结果、证据及限制 |
| [AI 使用](docs/04-ai-usage.md) | AI 辅助范围、已知个人贡献及验证 |
| [复盘](docs/05-retrospective.md) | 实际发现、投入与后续工作 |
| [Langfuse](docs/06-langfuse.md) | 配置、Trace 结构、脱敏及云端核查 |
| [TodoList](docs/07-todolist.md) | 计划上下文、真实状态、失败重试及修订历史 |

本次平台为 macOS；Linux 尚未独立实测，Windows 的文件锁实现不支持。不自动续跑，也不支持跨进程共享写入；强制终止后仅复用已确认结果，无法确认的生成尝试可能需要重新执行并计费。真实模型输出具有变动性，单次通过不代表未来所有输入都能成功。

题号尚未提供，提交时由作者补充。最终 SHA 通过 `git rev-parse HEAD` 获取；不能以旧提交代表尚未提交的新实现。

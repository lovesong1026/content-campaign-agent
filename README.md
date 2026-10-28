# 内容任务规划 Agent

基于 DeepSeek Flash 的异步单 Agent，根据主题、预算和方案交付截止时间检索素材、生成短视频脚本、估算成本并保存方案。模型观察真实工具结果并调整动作，程序维护费用、时间、状态及执行上限。

已实现 CLI、四工具、真实模型与离线 Mock、持久化、幂等、批量限流并发、本地 Trace 及可选 Langfuse 追踪。当前验证：84 项测试通过；原有真实模型 9 组、Mock 9 组及新增离线模拟 33 组场景通过各自检查。Langfuse 的 Mock 与真实 DeepSeek Trace 均已发送并读回核查，证据及限制见 [评估报告](docs/03-evaluation.md)。

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

同一输出目录中，同 ID、同输入返回已有任务，不重复执行或收费；同 ID 改输入返回冲突。重新执行模型须用新 ID 或新 `--output-dir`。归档目录 `examples/real/`、`examples/mock/` 用于评审，运行记录默认写入 `.runs/tasks/<任务ID哈希>/`。

结果 JSON 包含计划及步骤状态、真实工具结果、费用账本、模拟时间、保存结果或未完成原因、Trace 路径。`show` 不发起模型请求。单任务退出码：成功 0、未完成 1、输入不足或冲突 2；批量有未成功任务返回 1。结果写 stdout，诊断写 stderr。

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

本次平台为 macOS；Linux 尚未独立实测，Windows 的文件锁实现不支持。不提供失败步骤续跑和跨进程共享写入。真实模型输出具有变动性，单次通过不代表未来所有输入都能成功。

题号尚未提供，提交时由作者补充。最终 SHA 通过 `git rev-parse HEAD` 获取；不能以旧提交代表尚未提交的新实现。

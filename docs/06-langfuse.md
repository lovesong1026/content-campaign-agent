# Langfuse 接入

项目已接入 Langfuse Python SDK **5.0.4**（依赖范围 `>=5.0.4,<6`，精确版本见 `uv.lock`）。支持真实 DeepSeek 请求、Mock 工具链和任务并发。本地 SDK 导出、回归测试及 Cloud 发送与读回验证均已通过，见 [核查报告](../examples/langfuse/report.json)。

## 配置与运行

在 Langfuse 项目的 Settings → API Keys 中创建项目密钥，填入项目根目录 `.env`；文件已被 Git 忽略。不要把密钥写进命令参数或提交到仓库。

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=填写项目公钥
LANGFUSE_SECRET_KEY=填写项目私钥
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_ENVIRONMENT=development
LANGFUSE_PROJECT_ID=
```

`LANGFUSE_BASE_URL` 必须与项目所在区域或自部署地址一致。上例为欧盟区域，美国为 `https://us.cloud.langfuse.com`，日本为 `https://jp.cloud.langfuse.com`。本次验证项目位于日本区域。可选的 `LANGFUSE_PROJECT_ID` 用于在结果中拼接 Trace 链接，留空时仍返回 `trace_id`；可以从项目 URL 的 `/project/<ID>/` 中取得。本地 `.env` 已填写认证后读回的项目 ID。

密钥均非空且 `LANGFUSE_ENABLED=true` 时启用；未配置凭据时自动跳过初始化；`LANGFUSE_ENABLED=false` 强制关闭。配置直接通过 Settings 传入 SDK，无需将 `.env` 导出到进程全局环境。

官方兼容性说明：Python SDK v5 需要 Langfuse v4；自部署最低版本为 **4.59.0**。Cloud 满足该要求；自部署 v3 不能直接使用本次锁定版本。[官方 SDK 兼容性文档](https://langfuse.com/docs/observability/sdk/overview)

```bash
uv sync --locked
# Mock：发送追踪，但不调用 DeepSeek。
uv run campaign-agent run --mode mock --input examples/inputs/normal.json --scenario data/scenarios/fixed-clock.json --output-dir .runs/langfuse-demo
# 真实模型：会产生 DeepSeek API 用量。
uv run campaign-agent run --mode real --input examples/inputs/normal.json --scenario data/scenarios/fixed-clock.json --output-dir .runs/langfuse-real-demo
```

同 ID、同输入在同一输出目录中复用旧结果，不再创建 Trace。要验证一次新执行，请使用新的任务 ID 或输出目录。

## Trace 结构

```text
plan-content-campaign                 agent，每次新任务一个 Trace
├── decide-next-action               generation，每轮真实规划请求
├── search_materials                 retriever
├── decide-next-action               generation
├── generate_script                  tool
│   └── write-video-scripts           generation，真实脚本请求
├── decide-next-action               generation
├── estimate_cost                    tool
├── decide-next-action               generation
└── save_plan                        tool
```

任务的 `run_id` 作为 32 位十六进制 `trace_id`；`task_id` 的哈希作为 `session_id`。同一逻辑任务的多次独立执行可按 session 聚合。名称保持固定，轮次、工具调用 ID、模式与场景放在 metadata，便于查询和比较。[官方 Trace 设计建议](https://langfuse.com/docs/observability/best-practices)

任务记录业务输入、最终状态、原因、候选脚本、保存结果、模拟费用和耗时。模型 generation 记录完整消息、工具定义、响应、模型名、Prompt 版本与哈希、Token、实际延迟及安全错误代码。工具记录参数、结果、故障、被阻止或跳过的调用。Mock 不创建虚假的模型 generation。

TodoList 的当前进度和修订摘要进入每轮规划输入，根节点输出保留 `todo_list` 与 `plan_revisions`；工具结果中的 `todo_id` 可关联实际尝试。三组真实运行及云端读回验证见 [TodoList 指南](07-todolist.md)。

模型请求集中在 `DeepSeekClient.request`，所以使用 SDK 的显式 generation 埋点，以控制输出及异常脱敏，并复用现有安全错误处理。没有同时开启 OpenAI 自动埋点，避免同一次请求重复记录。独立任务使用 SDK 的上下文传播，脚本调用嵌套在生成工具内，规划请求与工具为同级。

## 费用、上报与数据

- `simulated_costs` 与 `simulated_elapsed_seconds` 是本地业务规则的模拟指标，不作为模型 USD 费用写入 `cost_details`。
- 模型 Token 来自供应商 response；原始 usage（含可能的缓存字段）保留在 generation metadata。Langfuse 货币估价取决于平台模型定价是否正确匹配 `deepseek-flash`，不能当作供应商实际账单。
- Langfuse 的延迟为实际执行时间，模拟工具耗时单独显示。
- 上报包含主题、Prompt、素材及脚本文本；已配置密钥和常见凭据字段会脱敏。Settings 快照不包含 DeepSeek 或 Langfuse 密钥，远端错误正文不上传。
- 业务结果、费用及 Trace JSONL 仍在本地保存；追踪初始化、更新或导出失败不改变业务计费与成功状态。CLI 收尾在后台线程执行 `flush()`。
- 输出中的 `telemetry.status=ready` 只代表 SDK 初始化成功，`delivery=unverified` 表示尚未确认远端接收。SDK 可能异步报告网络错误，必须通过远端读回来确认上报。

## 云端自检

配置凭据后执行，默认 Mock 模式不产生 DeepSeek 请求。脚本需要 Node.js / `npx`；首次运行会下载 Langfuse CLI。

```bash
uv run python scripts/verify_langfuse.py
# 进一步核查真实模型 generation、模型名及 usage。
uv run python scripts/verify_langfuse.py --mode real
```

脚本会检查认证、创建新任务、flush，然后通过 `langfuse-cli api observations list --trace-id ...` 读取新 Trace，检查根节点、输入输出、四工具、父子关系及 generation 的模型和 usage。结果、读回记录和 `audit.json` 存入 `.runs/langfuse-<模式>-<时间戳>/`；输出包含 Trace 链接。退出码 0 表示检查通过，1 表示执行或核查失败，2 表示配置不足。

还应在 UI 打开 Trace，检查脚本内容是否便于阅读，并结合 [最新官方规范](https://langfuse.com/docs/observability/best-practices) 复核。只有发送和读回均完成，才能确认云端接入有效。

本次已完成 API 核查，并按最新官方规范检查名称、输入输出、层级、模型名、用量、模式标签及 session。模型输出调整为带 `role` 的消息数组，便于会话视图展示。Mock 共 5 个节点，无 generation；真实任务共 10 个节点，含 4 个规划 generation、1 个脚本 generation，合计 18,291 Token。两者均成功保存方案。实际 API 账单未查询；UI 尚未独立人工审阅。

- [真实 DeepSeek Trace](https://jp.cloud.langfuse.com/project/cmtgrjba70008ad0fq7rajgmg/traces/2f7ae3d6369b4753b8779bfaab555f90)
- [Mock Trace](https://jp.cloud.langfuse.com/project/cmtgrjba70008ad0fq7rajgmg/traces/fbdff5ba6c44473695705c6dcc2eead1)

首次自检曾因 CLI 响应的 `body` 包装未解析而失败；已修正并再次发送、读回验证。早期调试 Trace 仍可能出现在项目中，正式通过记录以上述链接及报告为准。

## 本地验证

```bash
uv run pytest tests/test_telemetry.py -q
uv run pytest -q --junitxml=examples/langfuse-test-results.xml
```

12 项追踪测试使用真实 Langfuse SDK 和内存导出器，验证父子结构、三个并发任务隔离、幂等不重复上报、模型名和 usage、失败计费、脱敏、SDK 故障隔离、输入不足、供应商错误、取消收尾及跳过调用。测试中的模型响应使用受控替身，所有自动化测试禁止云端上报。

扩展模拟数据集仍强制关闭遥测，保证它的“完全离线”约定。33 组用例尚未上传为 Langfuse Dataset；数据集、实验及 Prompt 平台管理属于后续接入范围。

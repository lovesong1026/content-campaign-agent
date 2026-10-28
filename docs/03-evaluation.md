# 评估报告

验证日期：2026-10-28（北京时间）。真实 DeepSeek 与离线 Mock 分开评估，引用的输出及 Trace 均由实际执行生成。

## 1. 环境与命令

环境为 macOS arm64、Python 3.12.13、uv 0.11.7、OpenAI SDK 3.40.0、Pydantic 2.14.1，精确依赖见 `uv.lock`。最终检查及墙钟计时见 [核验记录](../examples/verification.json)。

实际执行：

```bash
uv sync
uv run pytest -q --junitxml=examples/test-results.xml
uv run ruff check .
uv run ruff format --check .
uv build
uv run python scripts/run_examples.py --mode real --destination examples/real
uv run python scripts/run_examples.py --mode mock --destination examples/mock
```

依赖首次下载超时，重试官方 PyPI 后安装成功；一次镜像尝试返回 403，未用作最终依赖来源。

## 2. 自动化结果与覆盖

37 项测试全部通过，见 [JUnit 记录](../examples/test-results.xml)。Ruff 检查及格式检查通过，sdist 和 wheel 构建成功，已检查 wheel 包含合成数据。

| 验收点 | 自动化证据 | 真实模型证据 |
| --- | --- | --- |
| A01 正常完成 | `test_normal_and_repeat_survive_restart` | [输出](../examples/real/normal/output.json)、[Trace](../examples/real/normal/trace.jsonl) |
| A02 缺必要信息 | 缺主题、预算、时间及 CLI 缺信息测试 | [缺预算](../examples/real/missing-budget/output.json)、[缺时间](../examples/real/missing-deadline/output.json)，均无模型请求 |
| A03 工具失败 | `test_tool_failure_preserves_success_and_charges` | [观察故障后重试](../examples/real/tool-failure/trace.jsonl) |
| A04 超预算调整 | `test_overbudget_replans_preserving_old_artifacts` | [超预算 Trace](../examples/real/overbudget/trace.jsonl) |
| A05 预算不可行 | `test_infeasible` 的预算分支 | [输出](../examples/real/budget-infeasible/output.json) |
| A06 重复执行 | 重启读取、同 ID 冲突、新 ID 独立执行 | [重复读取](../examples/real/repeat/output.json)，无新增调用或费用 |
| A07 时间不足 | 不可行、已到期、时间门禁与精确边界 | [输出](../examples/real/time-infeasible/output.json) |
| A08 素材不足 | `test_no_materials` | [Trace](../examples/real/no-materials/trace.jsonl) |
| A09 执行上限 | 循环检索、无效动作、单轮 12 调用测试 | 通过受控客户端验证工程行为，不计为真实模型效果 |
| A10 版本与边界 | 旧估算、最新账本、依赖、失败保存、精确费用与时间 | 真实路径提供实例，其余由确定性测试验证 |
| A11 并发隔离 | 批量同 ID、冲突、任务上限、请求信号量和进程写锁测试 | 未进行真实并发压测 |
| A12 单轮多调用 | 正常串行、故障后跳过、保存后跳过、超大批次、全局上限 | 通过受控客户端验证协议行为 |

测试源文件见 [tests](../tests/)，受控客户端的动作不冒充真实模型决策。

## 3. 真实模型结果

[真实运行汇总](../examples/real/report.json) 中 9 组场景均通过各自检查。固定模拟起点为 `2030-01-01T10:00:00+08:00`；Trace 的事件时间戳为实际运行时间，二者含义不同。

| 场景 | 实际结果 | 工具费 C | 方案费 P | 总费用 | 工具数 / 模拟耗时 |
| --- | --- | ---: | ---: | ---: | --- |
| 正常 | 成功保存两条 economy 视频 | 4 | 2 | 6 | 4 次 / 9 分钟 |
| 超预算 | 改写后保存一条 economy 视频 | 8 | 1 | 9 | 7 次 / 16 分钟 |
| 工具失败 | 生成失败后模型重试，保存两条视频 | 6 | 2 | 8 | 5 次 / 14 分钟 |
| 缺预算、缺时间 | `needs_input` | 0 | 未知 | 未知 | 无模型及业务工具调用 |
| 预算不可行 | `incomplete`，预算不足及工具费用下界缺口 | 0 | 未知 | 未知 | 无业务工具调用 |
| 时间不足 | `incomplete`，最短链路至少超时 1 分钟 | 0 | 未知 | 未知 | 无业务工具调用 |
| 素材不足 | 检索无可用结果后明确停止 | 以实际输出为准 | 未知 | 未知 | 见 Trace |
| 重复执行 | `reused=true`，返回原结果 | 沿用 | 沿用 | 6 | 无新增调用 |

### 超预算路径

检索 → 高级素材与 premium 初稿 → 估算总费用 12 → 新的模型响应选择基础素材及 economy → 改写后估算 8 → 模型额外估算一次，费用增加到 9 → 保存。

额外估算实际发生，增加 1 单位和 1 分钟，未从记录删除。不能将此路径描述成“最终以 8 保存”。模型能根据观察调整，但仍可能产生冗余调用。

### 工具失败路径

首次脚本生成注入可恢复故障，计费 2、耗时 5 分钟；此前检索状态保持成功。下一轮模型选择重新生成，随后估算、保存。业务执行器没有自动重试，Trace 可核对观察及后续模型响应的轮次。

### 内容与实际 API 用量

AI 检查了归档脚本的字段、引用、规格和基本主题相关性；尚未进行独立人工质量评分。内容围绕合成材料中的自然光、桌面及日常场景展开，不代表商业投放效果。

正常、超预算及失败路径的 token 合计分别为 19,454、51,009、26,598，来自各自 `model_response.usage`，包含规划和脚本请求。缓存等计价因素不同，不能仅据总数推断货币费用；未查询真实账单，实际金额保持未知。

## 4. Mock 结果

[Mock 汇总](../examples/mock/report.json) 的 9 组场景通过。正常总费用 5，超预算调整后 8，工具失败重试后 7。它选择的条数与动作不同于真实模型，符合允许多条有效路径的设计。

Mock 仅验证工具、消息、账本及状态等工程行为，不证明真实模型的规划效果。

## 5. 限制

- 本次真实模型样本不等于大规模成功率；模型别名的后端可更新，路径与内容有变动性。
- 超预算场景在用户主题中要求先生成高规格初稿并实际估价，以触发观察；没有强制执行器选择高价或预写日志。一般输入可能直接选便宜方案。
- 全量历史与重复状态摘要增加 token；本次出现额外估算，尚未做上下文压缩或冗余调用优化。
- 仅生成方案，不执行真实内容制作、发布或授权核验；素材均为合成数据。
- 单输出目录只支持一个写进程，其内部支持任务并发。跨文件无数据库事务，异常中断可能留下未确认文件。
- 不支持失败步骤续跑，强制终止不能保证收尾。验证平台为 macOS，Linux 尚未独立实测，Windows 文件锁不支持。

证据导出只调整目录定位信息以便移植，不改写实际工具参数、返回、费用、事件及模型动作。重跑脚本默认使用新目录，既有证据不会被覆盖。

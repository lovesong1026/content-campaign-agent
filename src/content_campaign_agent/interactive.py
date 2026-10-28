import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .schemas import parse_time, validate_input

TOOL_LABELS = {
    "search_materials": "素材检索",
    "generate_script": "脚本生成",
    "estimate_cost": "成本估算",
    "save_plan": "方案保存",
}
STATUS_LABELS = {
    "succeeded": "成功",
    "failed": "失败",
    "blocked": "被阻止",
    "skipped": "未执行",
    "running": "执行中",
    "pending": "待执行",
}


class EndSession(Exception):
    pass


def ask(prompt):
    value = input(prompt).strip()
    if value.casefold() in {"exit", "quit", "q", "退出"}:
        raise EndSession
    return value


def collect_task(agent, theme):
    raw = {"theme": theme}
    for field, label in (("budget", "预算"), ("deadline", "截止时间")):
        while True:
            start = (
                parse_time(agent.scenario.fixed_start)
                if agent.scenario.fixed_start
                else datetime.now(ZoneInfo("Asia/Shanghai"))
            )
            default = (start + timedelta(minutes=30)).isoformat(timespec="minutes")
            prompt = (
                f"请输入截止时间（默认北京时间，回车使用 {default}）："
                if field == "deadline"
                else "请输入预算（模拟单位，最多两位小数）："
            )
            value = ask(prompt)
            raw[field] = (value or default) if field == "deadline" else value
            normalized, errors = validate_input(raw)
            if field in errors:
                print(f"{label}无效：{errors[field]}。请重新输入。", flush=True)
                continue
            if field == "deadline" and parse_time(normalized[field]) <= start:
                print(f"截止时间必须晚于任务起点 {start.isoformat()}。请重新输入。", flush=True)
                continue
            raw[field] = normalized[field]
            break
    return raw


def show_progress(entry):
    kind = entry["kind"]
    if kind == "decision_context":
        print(f"第 {entry['round']} 轮：模型正在决定下一步……", flush=True)
        return
    if kind not in {"tool_started", "tool_result"}:
        return
    label = TOOL_LABELS.get(entry["tool_name"], entry["tool_name"])
    if kind == "tool_started":
        print(f"[执行] {label}……", flush=True)
        return
    status = entry["status"]
    if status == "succeeded":
        print(f"[完成] {label}；累计工具费用 {entry['cumulative_tool_cost']}。", flush=True)
        if entry["tool_name"] == "estimate_cost":
            print(f"当前估算总费用：{entry['data']['total_at_estimate']}。", flush=True)
    else:
        message = (entry.get("error") or {}).get("message", "无结果")
        print(f"[{STATUS_LABELS.get(status, status)}] {label}：{message}", flush=True)
    if entry.get("requires_replan"):
        print("需要调整计划，真实结果已交回模型，由模型决定重试、替代或停止。", flush=True)


def show_result(result):
    print("\n" + "─" * 40)
    if result.get("resume_error"):
        print(f"续跑未启动：{result['resume_error']}。原任务状态保持不变。")
    elif result["status"] == "succeeded":
        print("任务完成：方案已实际保存。")
    else:
        print(f"任务未完成：{result.get('reason', result['status'])}")
    if result.get("reused"):
        print("已复用已有记录，没有新增执行或费用。")
    print(f"任务 ID：{result['task_id']}")
    if result.get("summary"):
        print(f"说明：{result['summary']}")
    costs = result.get("costs")
    if costs:
        print(f"累计工具费用：{costs['tool_cost']}")
        print(f"方案预计费用：{costs['plan_cost'] or '未知（尚未取得估算）'}")
        print(f"预计总费用：{costs['total_cost'] or '未知（尚未取得估算）'}")
        if costs.get("budget_gap") and Decimal(costs["budget_gap"]) > 0:
            print(f"当前方案预算缺口：{costs['budget_gap']}。")
    gaps = result.get("gaps", {})
    if Decimal(gaps.get("minimum_total_budget_gap", "0")) > 0:
        print(
            f"总费用下界：{gaps['minimum_total_cost']}；"
            f"预算至少还差：{gaps['minimum_total_budget_gap']}。"
        )
    if gaps.get("minimum_time_gap_seconds", 0) > 0:
        print(f"最短剩余路径至少超时：{gaps['minimum_time_gap_seconds']:g} 秒。")
    steps = result.get("steps", [])
    if steps:
        print("实际步骤记录（包括历史失败）：")
        for step in steps:
            label = TOOL_LABELS.get(step["tool_name"], step["tool_name"])
            status = STATUS_LABELS.get(step["status"], step["status"])
            unknown = "，结果未知" if step.get("outcome_unknown") else ""
            print(f"  - {label}：{status}{unknown}")
    saved = result.get("saved")
    if saved:
        path = Path(result["task_directory"]) / saved["path"]
        print(f"方案保存位置：{path.resolve()}")
    if result.get("trace_path"):
        print(f"执行记录：{Path(result['trace_path']).resolve()}")
    print("─" * 40, flush=True)


def run_interactive(agent):
    previous_progress = agent.executor.progress
    agent.executor.progress = show_progress
    with asyncio.Runner() as runner:
        try:
            with agent.storage.writer():
                runner.run(agent.start())
                if (
                    agent.model.mode == "real"
                    and not agent.settings.deepseek_api_key.get_secret_value()
                ):
                    print(
                        "未配置 DEEPSEEK_API_KEY，请填写本地 .env 或使用 interactive --mode mock。"
                    )
                    return 2
                mode = (
                    "真实 DeepSeek（会产生 API 用量）"
                    if agent.model.mode == "real"
                    else "离线 Mock"
                )
                print(f"\n内容活动规划 Agent 已启动：{mode}")
                print(f"输出目录：{agent.storage.root.resolve()}")
                if agent.telemetry.status == "ready":
                    print("Langfuse 已配置，执行追踪将上传；可设置 LANGFUSE_ENABLED=false 关闭。")
                print("输入活动主题开始；输入 resume 续跑已有任务；任意输入阶段可输入 exit 退出。")
                if agent.scenario.fixed_start:
                    print(f"当前使用固定模拟起点：{agent.scenario.fixed_start}")
                last_task_id = None
                while True:
                    theme = ask("\n请输入活动主题（resume 续跑，exit 退出）：")
                    if theme.casefold() in {"resume", "续跑"}:
                        hint = f"，回车使用 {last_task_id}" if last_task_id else ""
                        task_id = (
                            ask(f"请输入原任务 ID（保持原目录与模式{hint}）：") or last_task_id
                        )
                        if not task_id:
                            print("任务 ID 不能为空。", flush=True)
                            continue
                        print("正在读取原任务并尝试续跑……", flush=True)
                        result = runner.run(agent.resume_task(task_id))
                    elif not theme:
                        print("活动主题不能为空，请重新输入。", flush=True)
                        continue
                    else:
                        raw = collect_task(agent, theme)
                        print(
                            f"\n开始执行：{raw['theme']}；预算 {raw['budget']}；"
                            f"截止时间 {raw['deadline']}。",
                            flush=True,
                        )
                        result = runner.run(agent.run_task(raw))
                    last_task_id = result["task_id"]
                    show_result(result)
        except (EndSession, EOFError):
            print("\n已退出，任务结果已保存在输出目录。", flush=True)
            return 0
        except KeyboardInterrupt:
            print("\n已取消并退出，已确认结果保留；未完成任务可使用 resume 续跑。", flush=True)
            return 130
        finally:
            agent.executor.progress = previous_progress
            runner.run(agent.close())

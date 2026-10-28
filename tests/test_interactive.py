import asyncio
import json
import subprocess

import pytest

from content_campaign_agent.cli import main, parser
from content_campaign_agent.interactive import run_interactive
from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import ModelError, Scenario


def feed(monkeypatch, values):
    replies = iter(values)
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        try:
            value = next(replies)
        except StopIteration as exc:
            raise EOFError from exc
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", read)
    return prompts


def snapshots(agent):
    return [json.loads(path.read_text()) for path in agent.storage.root.glob("tasks/*/task.json")]


def test_default_command_is_interactive():
    args = parser().parse_args([])
    assert args.command == "interactive" and args.mode == "real"
    assert args.output_dir is None and args.scenario is None
    args = parser().parse_args(["interactive", "--mode", "mock", "--output-dir", "custom"])
    assert args.command == "interactive" and args.mode == "mock"
    assert str(args.output_dir) == "custom"


def test_bare_cli_enters_interactive_session(make_agent, monkeypatch, capsys):
    agent = make_agent()
    feed(monkeypatch, ["exit"])
    monkeypatch.setattr("sys.argv", ["campaign-agent"])
    monkeypatch.setattr("content_campaign_agent.cli.settings_for", lambda args: agent.settings)
    monkeypatch.setattr("content_campaign_agent.cli.create_agent", lambda args, settings: agent)
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    assert "内容活动规划 Agent 已启动" in capsys.readouterr().out
    assert not snapshots(agent)


def test_interactive_reprompts_invalid_fields_without_creating_failed_tasks(
    make_agent, monkeypatch, capsys
):
    prompts = feed(
        monkeypatch,
        [
            " ",
            "春季新品推广",
            "",
            "wrong",
            "-1",
            "0.001",
            "20",
            "not-a-date",
            "2020-01-01T10:00:00+08:00",
            "2030-01-01T10:00:00+08:00",
            "2030-01-01 10:30",
            "exit",
        ],
    )
    agent = make_agent()
    assert run_interactive(agent) == 0
    states = snapshots(agent)
    assert len(states) == 1 and states[0]["status"] == "succeeded"
    assert states[0]["input"]["budget"] == "20.00"
    assert states[0]["input"]["deadline"] == "2030-01-01T10:30:00+08:00"
    assert states[0]["tool_calls"] == 4 and states[0]["tool_cost"] == "4.00"
    assert len(prompts) == 12
    output = capsys.readouterr().out
    assert "活动主题不能为空" in output
    assert output.count("预算无效") == 4 and output.count("截止时间必须晚于") == 2
    assert "[执行] 素材检索" in output and "[完成] 方案保存" in output
    assert "任务完成：方案已实际保存" in output and "预计总费用：5.00" in output
    assert "方案保存位置：" in output and "已退出" in output
    assert agent.executor.progress is None
    with agent.storage.writer():
        pass


def test_interactive_default_deadline_and_repeated_theme_are_independent(make_agent, monkeypatch):
    feed(monkeypatch, ["春季新品推广", "20", "", "春季新品推广", "20", "", "exit"])
    agent = make_agent()
    assert run_interactive(agent) == 0
    states = snapshots(agent)
    assert len(states) == 2 and len({state["task_id"] for state in states}) == 2
    assert all(state["status"] == "succeeded" for state in states)
    assert all(state["input"]["deadline"] == "2030-01-01T10:30:00+08:00" for state in states)
    assert all(state["tool_cost"] == "4.00" for state in states)


@pytest.mark.parametrize(
    "replies", [[], ["exit"], ["春季新品", "exit"], ["春季新品", "20", "exit"]]
)
def test_interactive_exit_or_eof_closes_resources_without_starting_task(
    make_agent, monkeypatch, replies, capsys
):
    class ClosingModel(OfflineMockClient):
        closed = False

        async def close(self):
            self.closed = True

    feed(monkeypatch, replies)
    model = ClosingModel()
    agent = make_agent(model)
    assert run_interactive(agent) == 0
    assert model.closed and snapshots(agent) == []
    assert "已退出" in capsys.readouterr().out
    with agent.storage.writer():
        pass


def test_interactive_keyboard_interrupt_at_prompt_exits_cleanly(make_agent, monkeypatch, capsys):
    feed(monkeypatch, [KeyboardInterrupt()])
    agent = make_agent()
    assert run_interactive(agent) == 130
    assert snapshots(agent) == [] and "已取消并退出" in capsys.readouterr().out
    with agent.storage.writer():
        pass


def test_interactive_missing_real_key_fails_before_input(make_agent, monkeypatch, capsys):
    model = OfflineMockClient()
    model.mode = "real"
    agent = make_agent(model, deepseek_api_key="")
    prompts = feed(monkeypatch, [])
    assert run_interactive(agent) == 2
    assert not prompts and not snapshots(agent)
    assert "未配置 DEEPSEEK_API_KEY" in capsys.readouterr().out


def test_interactive_failed_task_reports_real_gap_and_keeps_session_open(
    make_agent, monkeypatch, capsys
):
    feed(monkeypatch, ["春季新品", "4", "", "春季新品", "20", "", "exit"])
    agent = make_agent()
    assert run_interactive(agent) == 0
    assert {state["status"] for state in snapshots(agent)} == {"incomplete", "succeeded"}
    output = capsys.readouterr().out
    assert "任务未完成：BUDGET_INFEASIBLE" in output
    assert "总费用下界：5.00；预算至少还差：1.00" in output
    assert "预计总费用：未知" in output
    assert output.count("任务完成：方案已实际保存") == 1


def test_interactive_failure_progress_never_hides_failed_attempt(make_agent, monkeypatch, capsys):
    feed(monkeypatch, ["春季新品", "20", "", "exit"])
    agent = make_agent(
        scenario=Scenario(
            fixed_start="2030-01-01T10:00:00+08:00", failures={"generate_script": [1]}
        )
    )
    assert run_interactive(agent) == 0
    state = snapshots(agent)[0]
    assert state["status"] == "succeeded" and state["tool_cost"] == "6.00"
    assert state["steps"][1]["status"] == "failed"
    output = capsys.readouterr().out
    assert "[失败] 脚本生成" in output
    assert "真实结果已交回模型" in output
    assert "脚本生成：失败" in output and "预计总费用：7.00" in output


def test_interactive_resume_missing_and_completed_tasks(make_agent, monkeypatch, task, capsys):
    class PlannerFailure(OfflineMockClient):
        async def decide(self, context, messages, record):
            if context["searched"]:
                raise ModelError("MODEL_TIMEOUT", "测试规划超时")
            return await super().decide(context, messages, record)

    original = make_agent(PlannerFailure())
    first = asyncio.run(original.run_task(task))
    assert first["status"] == "incomplete"
    feed(monkeypatch, ["resume", "missing", "resume", task["task_id"], "resume", "", "exit"])
    agent = make_agent()
    assert run_interactive(agent) == 0
    state = snapshots(agent)[0]
    assert state["status"] == "succeeded" and state["resume_count"] == 1
    assert state["tool_calls"] == 4 and state["tool_cost"] == "4.00"
    output = capsys.readouterr().out
    assert "TASK_NOT_FOUND" in output
    assert "已复用已有记录，没有新增执行或费用" in output


async def test_progress_display_failure_does_not_change_task_outcome(make_agent, task, caplog):
    agent = make_agent()

    def broken(entry):
        raise RuntimeError("测试展示异常")

    agent.executor.progress = broken
    result = await agent.run_task(task)
    assert result["status"] == "succeeded" and result["costs"]["total_cost"] == "5.00"
    assert "进度展示失败，任务继续执行" in caplog.text


def test_interactive_cli_subprocess_without_json_input(tmp_path):
    output = tmp_path / "interactive"
    process = subprocess.run(
        [
            ".venv/bin/python",
            "-m",
            "content_campaign_agent",
            "interactive",
            "--mode",
            "mock",
            "--scenario",
            "data/scenarios/fixed-clock.json",
            "--output-dir",
            str(output),
        ],
        input="春季新品推广\n20\n\nexit\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "内容活动规划 Agent 已启动：离线 Mock" in process.stdout
    assert "请输入活动主题" in process.stdout and "请输入预算" in process.stdout
    assert "任务完成：方案已实际保存" in process.stdout and "已退出" in process.stdout
    assert '"status":' not in process.stdout
    path = next(output.glob("tasks/*/task.json"))
    assert json.loads(path.read_text())["status"] == "succeeded"

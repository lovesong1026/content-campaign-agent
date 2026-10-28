import json
import subprocess

from content_campaign_agent.cli import execute, parser
from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import ModelError


async def test_cli_missing_input_before_api_key_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    args = parser().parse_args(
        ["run", "--theme", "春季新品", "--output-dir", str(tmp_path / "runs")]
    )
    result, code = await execute(args)
    assert code == 2 and result["status"] == "needs_input"
    assert set(result["input_errors"]) == {"budget", "deadline"}


def test_cli_module_entrypoint_and_show(tmp_path):
    command = [".venv/bin/python", "-m", "content_campaign_agent"]
    output = str(tmp_path / "cli")
    process = subprocess.run(
        command
        + [
            "run",
            "--mode",
            "mock",
            "--theme",
            "春季新品",
            "--budget",
            "20",
            "--deadline",
            "2030-01-01T10:30:00+08:00",
            "--task-id",
            "cli-test",
            "--output-dir",
            output,
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stdout
    first = json.loads(process.stdout)
    shown = subprocess.run(
        command + ["show", "--task-id", "cli-test", "--output-dir", output],
        capture_output=True,
        text=True,
    )
    assert shown.returncode == 0
    assert json.loads(shown.stdout)["run_id"] == first["run_id"]


async def test_cli_resume_persisted_task_and_repeated_resume(make_agent, task):
    class PlannerFailure(OfflineMockClient):
        async def decide(self, context, messages, record):
            if context["searched"]:
                raise ModelError("MODEL_TIMEOUT", "测试规划超时")
            return await super().decide(context, messages, record)

    agent = make_agent(PlannerFailure())
    first = await agent.run_task(task)
    assert first["reason"] == "MODEL_TIMEOUT"
    command = [
        ".venv/bin/python",
        "-m",
        "content_campaign_agent",
        "resume",
        "--task-id",
        task["task_id"],
        "--mode",
        "mock",
        "--output-dir",
        str(agent.settings.output_dir),
    ]
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["resumed"] and result["status"] == "succeeded"
    assert result["resume_count"] == 1 and result["tool_calls"] == 4
    assert result["costs"]["total_cost"] == "5.00"
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert not json.loads(repeated.stdout)["resumed"]
    assert json.loads(repeated.stdout)["ledger"] == result["ledger"]


async def test_cli_resume_errors_have_input_error_exit_code(make_agent, task):
    agent = make_agent()
    missing, code = await execute(
        parser().parse_args(
            [
                "resume",
                "--task-id",
                "missing",
                "--mode",
                "mock",
                "--output-dir",
                str(agent.settings.output_dir),
            ]
        )
    )
    assert code == 2 and missing["reason"] == "TASK_NOT_FOUND"
    await agent.run_task({**task, "budget": None})
    invalid, code = await execute(
        parser().parse_args(
            [
                "resume",
                "--task-id",
                task["task_id"],
                "--mode",
                "mock",
                "--output-dir",
                str(agent.settings.output_dir),
            ]
        )
    )
    assert code == 2 and invalid["resume_error"] == "INPUT_REQUIRED"

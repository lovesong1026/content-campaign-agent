import json
import subprocess

from content_campaign_agent.cli import execute, parser


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

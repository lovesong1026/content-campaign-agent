"""Send a fresh task and read back its observations using the Langfuse CLI."""

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from content_campaign_agent.agent import Agent
from content_campaign_agent.config import Settings
from content_campaign_agent.model import DeepSeekClient, OfflineMockClient
from content_campaign_agent.schemas import Scenario, dumps
from content_campaign_agent.storage import atomic_text
from content_campaign_agent.telemetry import Telemetry


def fetch_observations(settings, trace_id):
    environment = {
        **os.environ,
        "LANGFUSE_PUBLIC_KEY": settings.langfuse_public_key.get_secret_value(),
        "LANGFUSE_SECRET_KEY": settings.langfuse_secret_key.get_secret_value(),
        "LANGFUSE_BASE_URL": settings.langfuse_base_url,
        "LANGFUSE_HOST": settings.langfuse_base_url,
    }
    response = subprocess.run(
        [
            "npx",
            "-y",
            "langfuse-cli",
            "api",
            "observations",
            "list",
            "--trace-id",
            trace_id,
            "--fields",
            "core,basic,io,metadata,model,usage,trace_context",
            "--limit",
            "1000",
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if response.returncode:
        raise RuntimeError("OBSERVATIONS_QUERY_FAILED")
    payload = json.loads(response.stdout)
    # CLI releases may wrap the API response in a data/result envelope.
    for _ in range(4):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            break
        payload = payload.get("data", payload.get("result", payload.get("body")))
    raise RuntimeError("OBSERVATIONS_RESPONSE_INVALID")


def audit(observations, result):
    roots = [row for row in observations if row.get("name") == "plan-content-campaign"]
    root = roots[0] if len(roots) == 1 else {}
    generations = [row for row in observations if str(row.get("type")).upper() == "GENERATION"]
    tools = [
        row
        for row in observations
        if row.get("name") in {"search_materials", "generate_script", "estimate_cost", "save_plan"}
    ]
    output = root.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = None
    script_tools = {row["id"] for row in tools if row["name"] == "generate_script"}
    expected_scripts = sum(
        step["tool_name"] == "generate_script" and step["status"] in ("succeeded", "failed")
        for step in result["steps"]
    )
    expected_planners = result["decision_rounds"] if result["mode"] == "real" else 0
    expected_scripts = expected_scripts if result["mode"] == "real" else 0
    return {
        "one_root": len(roots) == 1,
        "same_trace": bool(observations)
        and all(row.get("traceId") == result["run_id"] for row in observations),
        "root_input": bool(root.get("input")),
        "root_output": isinstance(output, dict) and output.get("status") == result["status"],
        "four_tool_types": {row["name"] for row in tools}
        == {"search_materials", "generate_script", "estimate_cost", "save_plan"},
        "all_tool_observations": len(tools) == len(result["steps"]),
        "all_planner_generations": sum(
            row.get("name") == "decide-next-action" for row in generations
        )
        == expected_planners,
        "all_script_generations": sum(
            row.get("name") == "write-video-scripts" for row in generations
        )
        == expected_scripts,
        "tool_parents": bool(tools)
        and all(row.get("parentObservationId") == root.get("id") for row in tools),
        "planner_parents": all(
            row.get("parentObservationId") == root.get("id")
            for row in generations
            if row.get("name") == "decide-next-action"
        ),
        "script_parents": all(
            row.get("parentObservationId") in script_tools
            for row in generations
            if row.get("name") == "write-video-scripts"
        ),
        "generations": bool(generations) if result["mode"] == "real" else not generations,
        "model_and_usage": all(row.get("model") and row.get("usageDetails") for row in generations),
        "task_succeeded": result["status"] == "succeeded",
    }


async def run(mode):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    settings = Settings(output_dir=Path(".runs") / f"langfuse-{mode}-{stamp}")
    telemetry = Telemetry(settings)
    if telemetry.client is None:
        raise ValueError(
            "请在 .env 配置 LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY、"
            "LANGFUSE_BASE_URL，并启用 LANGFUSE_ENABLED"
        )
    if mode == "real" and not settings.deepseek_api_key.get_secret_value():
        raise ValueError("真实模式还需要 DEEPSEEK_API_KEY")
    try:
        valid = await asyncio.to_thread(telemetry.client.auth_check)
    except Exception:
        raise ValueError("Langfuse 认证检查失败，请检查区域地址和项目密钥") from None
    if not valid:
        raise ValueError("Langfuse 认证未通过")
    model = DeepSeekClient(settings, telemetry) if mode == "real" else OfflineMockClient()
    agent = Agent(
        settings,
        model,
        Scenario(name="langfuse-smoke", fixed_start="2030-01-01T10:00:00+08:00"),
        telemetry=telemetry,
    )
    try:
        with agent.storage.writer():
            await agent.start()
            result = await agent.run_task(
                {
                    "task_id": f"langfuse-{mode}-{stamp}",
                    "theme": "春季新品推广，优先采用基础素材与经济制作规格，生成一条短视频方案",
                    "budget": "20",
                    "deadline": "2030-01-01T10:30:00+08:00",
                }
            )
    finally:
        await model.close()
        await telemetry.flush()
    atomic_text(settings.output_dir / "output.json", dumps(result, indent=2) + "\n")
    checks = {}
    rows = []
    for _ in range(10):
        rows = await asyncio.to_thread(fetch_observations, settings, result["run_id"])
        checks = audit(rows, result)
        if all(checks.values()):
            break
        await asyncio.sleep(2)
    report = {
        "mode": mode,
        "trace_id": result["run_id"],
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
        "trace_url": await asyncio.to_thread(
            telemetry.client.get_trace_url, trace_id=result["run_id"]
        ),
    }
    atomic_text(
        settings.output_dir / "observations.json", dumps(telemetry.mask(rows), indent=2) + "\n"
    )
    atomic_text(settings.output_dir / "audit.json", dumps(report, indent=2) + "\n")
    print(dumps(report, indent=2))
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    args = parser.parse_args()
    try:
        passed = asyncio.run(run(args.mode))
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2) from None
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("Langfuse 核查失败；请检查网络、npx 和服务器版本。本地记录位于 .runs/langfuse-*。")
        raise SystemExit(1) from None
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

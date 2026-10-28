"""Replay synthetic fixtures offline; expectations are authored before execution."""

import argparse
import asyncio
import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from content_campaign_agent.agent import Agent
from content_campaign_agent.config import Settings
from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Scenario, dumps, parse_time
from content_campaign_agent.storage import atomic_text

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "examples/synthetic/cases.json"


def check_case(case, result, state):
    expected = case["expectedOutput"]
    actual = {key: result[key] for key in ("status", "reason", "tool_calls", "elapsed_seconds")}
    actual.update(
        tool_cost=result["costs"]["tool_cost"],
        total_cost=result["costs"]["total_cost"],
        input_error_fields=sorted(result["input_errors"]),
    )
    checks = {
        key: actual[key] == (sorted(value) if key == "input_error_fields" else value)
        for key, value in expected.items()
    }
    ledger = result["ledger"]
    checks.update(
        ledger_cost=sum((Decimal(row["fee"]) for row in ledger), Decimal(0))
        == Decimal(result["costs"]["tool_cost"]),
        ledger_time=sum(row["duration_seconds"] for row in ledger) == result["elapsed_seconds"],
        ledger_calls=len(ledger) == result["tool_calls"],
        execution_limits=result["tool_calls"] <= 12 and result["decision_rounds"] <= 12,
        offline_only=result["mode"] == "mock"
        and not any(event["kind"] == "model_request" for event in state["events"]),
        saved_iff_success=(result["saved"] is not None) == (result["status"] == "succeeded"),
        unique_todo_ids=len({item["todo_id"] for item in result["todo_list"]})
        == len(result["todo_list"]),
        todos_finalized=not any(
            item["status"] in ("pending", "running") for item in result["todo_list"]
        ),
        todo_results_backed_by_steps=all(
            any(
                step["call_id"] == item["call_id"] and step["status"] == item["status"]
                for step in result["steps"]
            )
            for item in result["todo_list"]
            if item["status"] in ("succeeded", "failed", "blocked")
        ),
    )
    injected = case["metadata"]["scenario"]["failures"]
    ordinals = {}
    for row in ledger:
        name = row["tool_name"]
        ordinals[name] = ordinals.get(name, 0) + 1
        should_fail = ordinals[name] in injected.get(name, [])
        checks[f"fault_{name}_{ordinals[name]}"] = row["outcome"] == (
            "failed" if should_fail else "succeeded"
        )
    if result["status"] == "needs_input":
        checks["no_decisions_for_invalid_input"] = result["decision_rounds"] == 0
    if result["saved"]:
        checks["saved_within_budget"] = Decimal(result["saved"]["total_cost"]) <= Decimal(
            result["input"]["budget"]
        )
        checks["saved_on_time"] = parse_time(result["saved"]["simulated_saved_at"]) <= parse_time(
            result["input"]["deadline"]
        )
    return {"actual": actual, "checks": checks, "passed": all(checks.values())}


async def run_case(case, directory):
    settings = Settings(
        _env_file=None,
        deepseek_api_key="",
        langfuse_enabled=False,
        output_dir=directory,
        data_dir=ROOT / "data",
    )
    agent = Agent(
        settings,
        OfflineMockClient(case["metadata"]["mock_policy"]),
        Scenario.model_validate(case["metadata"]["scenario"]),
    )
    with agent.storage.writer():
        await agent.start()
        result = await agent.run_task(case["input"])
    state = await agent.storage.load(result["task_id"])
    checked = check_case(case, result, state)
    if result["saved"]:
        saved_path = agent.storage.directory(result["task_id"]) / result["saved"]["path"]
        saved = json.loads(saved_path.read_text())
        checked["checks"]["saved_artifact_matches"] = (
            saved["content_hash"] == result["saved"]["content_hash"]
            and saved["total_cost"] == result["saved"]["total_cost"]
        )
        checked["passed"] = all(checked["checks"].values())
    return agent, result, checked


async def run(destination, dataset=DATASET):
    cases = json.loads(dataset.read_text())["cases"]
    ids = [case["id"] for case in cases]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("用例 ID 必须非空且唯一")
    if any(Path(name).name != name or name in (".", "..") for name in ids):
        raise ValueError("用例 ID 必须是单层目录名称")
    destination.mkdir(parents=True, exist_ok=False)
    report = {"mode": "mock", "dataset_version": 1, "cases": []}
    for case in cases:
        case_dir = destination / case["id"]
        agent, result, checked = await run_case(case, case_dir / "runtime")
        # Keep each evidence directory self-contained and portable.
        shutil.copytree(agent.storage.directory(result["task_id"]), case_dir / "evidence")
        shutil.rmtree(case_dir / "runtime")
        result.update(task_directory="evidence", trace_path="evidence/trace.jsonl")
        atomic_text(case_dir / "case.json", dumps(case, indent=2) + "\n")
        atomic_text(case_dir / "output.json", dumps(result, indent=2) + "\n")
        row = {"id": case["id"], "category": case["metadata"]["category"], **checked}
        report["cases"].append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'} {case['id']}", flush=True)
    report.update(total=len(cases), passed=sum(row["passed"] for row in report["cases"]))
    atomic_text(destination / "report.json", dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    destination = args.destination or ROOT / ".runs" / (
        "synthetic-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    report = asyncio.run(run(destination, args.dataset))
    print(f"{report['passed']}/{report['total']} passed; evidence: {destination}")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()

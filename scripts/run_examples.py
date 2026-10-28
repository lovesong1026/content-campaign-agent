"""Run actual real/mock scenarios and export self-contained, inspectable evidence."""

import argparse
import asyncio
import copy
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from content_campaign_agent.agent import Agent
from content_campaign_agent.config import Settings
from content_campaign_agent.model import DeepSeekClient, OfflineMockClient
from content_campaign_agent.schemas import Scenario, dumps
from content_campaign_agent.storage import atomic_text


async def run(mode, destination):
    if destination.exists():
        raise ValueError("证据目录已存在；请选择新目录，避免覆盖历史记录")
    destination.mkdir(parents=True)
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings = Settings(output_dir=Path(".runs") / f"evidence-{mode}-{session}")
    shared_model = DeepSeekClient(settings) if mode == "real" else None
    fixed = "2030-01-01T10:00:00+08:00"
    base = json.loads(Path("examples/inputs/normal.json").read_text())
    over = json.loads(Path("examples/inputs/overbudget.json").read_text())
    cases = [
        ("normal", base, Scenario(fixed_start=fixed)),
        ("overbudget", over, Scenario(fixed_start=fixed)),
        (
            "tool-failure",
            {**base, "task_id": "demo-failure"},
            Scenario(fixed_start=fixed, failures={"generate_script": [1]}),
        ),
        (
            "missing-budget",
            {k: v for k, v in {**base, "task_id": "demo-missing-budget"}.items() if k != "budget"},
            Scenario(fixed_start=fixed),
        ),
        (
            "missing-deadline",
            {
                k: v
                for k, v in {**base, "task_id": "demo-missing-deadline"}.items()
                if k != "deadline"
            },
            Scenario(fixed_start=fixed),
        ),
        (
            "budget-infeasible",
            {**base, "task_id": "demo-budget-infeasible", "budget": "3"},
            Scenario(fixed_start=fixed),
        ),
        (
            "time-infeasible",
            {**base, "task_id": "demo-time-infeasible", "deadline": "2030-01-01T10:08:00+08:00"},
            Scenario(fixed_start=fixed),
        ),
        (
            "no-materials",
            {**base, "task_id": "demo-no-materials"},
            Scenario(fixed_start=fixed, empty_materials=True),
        ),
        ("repeat", base, Scenario(fixed_start=fixed)),
    ]
    report = {"mode": mode, "session": session, "cases": []}
    normal_counts = None
    try:
        for name, raw, scenario in cases:
            model = shared_model or OfflineMockClient(
                "premium_first" if name == "overbudget" else "normal"
            )
            agent = Agent(settings, model, scenario)
            with agent.storage.writer():
                await agent.start()
                result = await agent.run_task(raw)
            state = await agent.storage.load(result["task_id"])
            case_dir = destination / name
            shutil.copytree(agent.storage.directory(result["task_id"]), case_dir)
            exported = copy.deepcopy(result)
            exported.update(task_directory=".", trace_path="trace.jsonl")
            atomic_text(case_dir / "input.json", dumps(raw, indent=2) + "\n")
            atomic_text(case_dir / "scenario.json", scenario.model_dump_json(indent=2) + "\n")
            atomic_text(case_dir / "output.json", dumps(exported, indent=2) + "\n")
            if name == "normal":
                passed = result["status"] == "succeeded"
                normal_counts = (result["run_id"], result["tool_calls"], len(state["events"]))
            elif name == "overbudget":
                results = [e for e in state["events"] if e["kind"] == "tool_result"]
                over_events = [
                    e
                    for e in results
                    if e["tool_name"] == "estimate_cost"
                    and e["status"] == "succeeded"
                    and not e["data"]["within_budget"]
                ]
                passed = (
                    result["status"] == "succeeded"
                    and bool(over_events)
                    and any(
                        e["kind"] == "model_decision" and e["round"] > over_events[0]["round"]
                        for e in state["events"]
                    )
                )
            elif name == "tool-failure":
                failed = [
                    e
                    for e in state["events"]
                    if e["kind"] == "tool_result" and e["status"] == "failed"
                ]
                passed = (
                    bool(failed)
                    and any(
                        e["kind"] == "model_decision" and e["round"] > failed[0]["round"]
                        for e in state["events"]
                    )
                    and result["status"] in ("succeeded", "incomplete")
                )
            elif name.startswith("missing"):
                passed = (
                    result["status"] == "needs_input"
                    and result["tool_calls"] == result["decision_rounds"] == 0
                )
            elif name == "repeat":
                passed = result.get("reused") and normal_counts == (
                    result["run_id"],
                    result["tool_calls"],
                    len(state["events"]),
                )
            else:
                passed = result["status"] == "incomplete" and result["saved"] is None
            row = {
                "case": name,
                "passed": bool(passed),
                "status": result["status"],
                "reason": result["reason"],
                "costs": result["costs"],
                "decision_rounds": result["decision_rounds"],
                "tool_calls": result["tool_calls"],
                "evidence": name,
            }
            report["cases"].append(row)
            atomic_text(destination / "report.json", dumps(report, indent=2) + "\n")
            print(
                dumps(
                    {
                        "case": name,
                        "passed": bool(passed),
                        "status": result["status"],
                        "total_cost": result["costs"]["total_cost"],
                    }
                ),
                flush=True,
            )
    finally:
        if shared_model:
            await shared_model.close()
    return all(row["passed"] for row in report["cases"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["real", "mock"], default="mock")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    destination = (
        args.destination
        or Path("examples/runs") / f"{args.mode}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    raise SystemExit(0 if asyncio.run(run(args.mode, destination)) else 1)


if __name__ == "__main__":
    main()

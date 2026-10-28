import argparse
import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from .agent import Agent
from .config import Settings
from .model import DeepSeekClient, OfflineMockClient
from .schemas import Scenario, dumps
from .storage import Storage


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)


def parser():
    root = argparse.ArgumentParser(description="预算与截止时间约束的内容活动规划 Agent")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("run", "batch", "show"):
        command = commands.add_parser(name)
        command.add_argument("--output-dir", type=Path)
        if name == "show":
            command.add_argument("--task-id", required=True)
            continue
        command.add_argument("--input", type=Path)
        command.add_argument("--mode", choices=["real", "mock"], default="real")
        command.add_argument("--scenario", type=Path)
        if name == "batch":
            command.add_argument("--concurrency", type=int)
        else:
            for field in ("theme", "budget", "deadline", "task-id"):
                command.add_argument("--" + field)
    return root


async def execute(args):
    settings = Settings()
    if args.output_dir:
        settings.output_dir = args.output_dir
    if args.command == "show":
        storage = Storage(settings.output_dir)
        state = await storage.load(args.task_id)
        if not state:
            return {"status": "needs_input", "reason": "TASK_NOT_FOUND"}, 2
        model = OfflineMockClient()
        agent = Agent(settings, model)
        return agent.result(state), 0
    if args.command == "batch":
        if not args.input:
            raise ValueError("batch 需要 --input JSON 数组文件")
        inputs = read_json(args.input)
        if not isinstance(inputs, list) or any(not isinstance(i, dict) for i in inputs):
            raise ValueError("batch 输入必须为 JSON 对象数组")
        if args.concurrency is not None:
            if not 1 <= args.concurrency <= 32:
                raise ValueError("并发数必须在 1—32 之间")
            settings.max_concurrent_tasks = args.concurrency
    else:
        direct = {key: getattr(args, key) for key in ("theme", "budget", "deadline", "task_id")}
        if args.input and any(value is not None for value in direct.values()):
            raise ValueError("--input 与直接业务参数不能同时使用")
        inputs = read_json(args.input) if args.input else direct
        if not isinstance(inputs, dict):
            raise ValueError("run 输入必须为 JSON 对象")
    scenario = Scenario.model_validate(read_json(args.scenario)) if args.scenario else Scenario()
    model = DeepSeekClient(settings) if args.mode == "real" else OfflineMockClient()
    agent = Agent(settings, model, scenario)
    try:
        with agent.storage.writer():
            await agent.start()
            result = (
                await agent.run_batch(inputs)
                if args.command == "batch"
                else await agent.run_task(inputs)
            )
    finally:
        try:
            await model.close()
        finally:
            if getattr(model, "telemetry", None) is not agent.telemetry:
                await agent.telemetry.flush()
    if args.command == "batch":
        code = 0 if all(r["status"] == "succeeded" for r in result["results"]) else 1
    else:
        code = (
            0 if result["status"] == "succeeded" else 2 if result["status"] == "needs_input" else 1
        )
    return result, code


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        result, code = asyncio.run(execute(args))
    except (OSError, ValueError, ValidationError, RuntimeError) as exc:
        # Validation/provider details may contain user input; keep errors bounded.
        result, code = (
            {"status": "needs_input", "reason": "CLI_ERROR", "summary": type(exc).__name__},
            2,
        )
    except KeyboardInterrupt:
        result, code = {"status": "incomplete", "reason": "CANCELLED"}, 1
    print(dumps(result, indent=2))
    raise SystemExit(code)

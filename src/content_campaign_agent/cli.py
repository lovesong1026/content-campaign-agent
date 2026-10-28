import argparse
import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from .agent import Agent
from .config import Settings
from .interactive import run_interactive
from .model import DeepSeekClient, OfflineMockClient
from .schemas import Scenario, dumps
from .storage import Storage


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)


def parser():
    root = argparse.ArgumentParser(description="预算与截止时间约束的内容活动规划 Agent")
    commands = root.add_subparsers(dest="command")
    root.set_defaults(command="interactive", mode="real", output_dir=None, scenario=None)
    for name in ("run", "batch", "show", "resume", "interactive"):
        command = commands.add_parser(name)
        command.add_argument("--output-dir", type=Path)
        if name in ("show", "resume"):
            command.add_argument("--task-id", required=True)
            if name == "resume":
                command.add_argument("--mode", choices=["real", "mock"], default="real")
            continue
        command.add_argument("--mode", choices=["real", "mock"], default="real")
        command.add_argument("--scenario", type=Path)
        if name == "interactive":
            continue
        command.add_argument("--input", type=Path)
        if name == "batch":
            command.add_argument("--concurrency", type=int)
        else:
            for field in ("theme", "budget", "deadline", "task-id"):
                command.add_argument("--" + field)
    return root


def settings_for(args):
    settings = Settings()
    if args.output_dir:
        settings.output_dir = args.output_dir
    return settings


def create_agent(args, settings):
    scenario_path = getattr(args, "scenario", None)
    scenario = Scenario.model_validate(read_json(scenario_path)) if scenario_path else Scenario()
    model = DeepSeekClient(settings) if args.mode == "real" else OfflineMockClient()
    return Agent(settings, model, scenario)


async def execute(args):
    if args.command == "interactive":
        raise ValueError("交互模式请通过 campaign-agent 的终端入口启动")
    settings = settings_for(args)
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
    elif args.command == "run":
        direct = {key: getattr(args, key) for key in ("theme", "budget", "deadline", "task_id")}
        if args.input and any(value is not None for value in direct.values()):
            raise ValueError("--input 与直接业务参数不能同时使用")
        inputs = read_json(args.input) if args.input else direct
        if not isinstance(inputs, dict):
            raise ValueError("run 输入必须为 JSON 对象")
    agent = create_agent(args, settings)
    try:
        with agent.storage.writer():
            await agent.start()
            if args.command == "resume":
                result = await agent.resume_task(args.task_id)
            elif args.command == "batch":
                result = await agent.run_batch(inputs)
            else:
                result = await agent.run_task(inputs)
    finally:
        await agent.close()
    if args.command == "batch":
        code = 0 if all(r["status"] == "succeeded" for r in result["results"]) else 1
    elif result.get("resume_error"):
        code = 2
    else:
        code = (
            0 if result["status"] == "succeeded" else 2 if result["status"] == "needs_input" else 1
        )
    return result, code


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        if args.command == "interactive":
            agent = create_agent(args, settings_for(args))
            raise SystemExit(run_interactive(agent))
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

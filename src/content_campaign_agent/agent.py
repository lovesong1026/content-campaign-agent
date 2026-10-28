import asyncio
import copy
import json
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import todos
from .executor import Executor
from .prompts import PLANNER_PROMPT
from .schemas import (
    ModelError,
    Scenario,
    ToolError,
    dumps,
    fingerprint,
    money,
    parse_time,
    validate_input,
)
from .storage import Storage
from .telemetry import Telemetry, task_output
from .tools import BusinessTools
from .trace import event


class Agent:
    def __init__(self, settings, model, scenario=None, telemetry=None):
        self.settings = settings
        self.model = model
        self.scenario = scenario or Scenario()
        self.storage = Storage(settings.output_dir)
        self.tools = BusinessTools(settings.data_dir, model, self.storage)
        self.telemetry = telemetry or getattr(model, "telemetry", None) or Telemetry(settings)
        self.executor = Executor(self.tools, self.storage, self.telemetry)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
        self.locks = {}
        self.states = {}
        self.active_tasks = 0
        self.peak_active_tasks = 0

    async def start(self):
        await self.storage.recover_interrupted()

    async def close(self):
        try:
            await self.model.close()
        finally:
            if getattr(self.model, "telemetry", None) is not self.telemetry:
                await self.telemetry.flush()

    def remaining_tools(self, state):
        if state["status"] == "succeeded":
            return []
        plan = state["plans"].get(state["current_plan_ref"])
        estimate = state["estimates"].get(state["current_estimate_id"])
        if estimate and Decimal(state["tool_cost"]) + Decimal(estimate["plan_cost"]) <= Decimal(
            state["input"]["budget"]
        ):
            names = ["save_plan"]
        elif plan and estimate is None:
            names = ["estimate_cost", "save_plan"]
        else:
            names = ["generate_script", "estimate_cost", "save_plan"]
            if not state["retrieved_materials"]:
                names.insert(0, "search_materials")
        return names

    def minimum_remaining(self, state):
        data = state.get("data_snapshot", self.tools.data)
        names = self.remaining_tools(state)
        fees = sum((Decimal(data["tools"][n]["fee"]) for n in names), Decimal(0))
        seconds = sum(data["tools"][n]["duration_seconds"] for n in names)
        return fees, seconds

    def minimum_total_cost(self, state, minimum_fee):
        data = state.get("data_snapshot", self.tools.data)
        pricing = data["pricing"]
        cheapest = min(Decimal(p) for p in pricing["production"].values())
        cheapest += min(Decimal(p) for p in pricing["materials"].values())
        cheapest += Decimal(pricing["publishing"])
        return Decimal(state["tool_cost"]) + minimum_fee + cheapest

    def context(self, state):
        minimum_fee, minimum_seconds = self.minimum_remaining(state)
        data = state.get("data_snapshot", self.tools.data)
        estimate = copy.deepcopy(state["estimates"].get(state["current_estimate_id"]))
        if estimate:
            estimate["within_budget"] = Decimal(state["tool_cost"]) + Decimal(
                estimate["plan_cost"]
            ) <= Decimal(state["input"]["budget"])
        now = parse_time(state["simulated_now"])
        return {
            "input": state["input"],
            "tool_cost": state["tool_cost"],
            "pricing": data["pricing"],
            "tool_rules": data["tools"],
            "retrieved_materials": state["retrieved_materials"],
            "searched": state["searched"],
            "current_plan": state["plans"].get(state["current_plan_ref"]),
            "current_estimate": estimate,
            "plans_count": len(state["plans"]),
            "known_plan_refs": list(state["plans"]),
            "simulated_now": state["simulated_now"],
            "remaining_seconds": (parse_time(state["input"]["deadline"]) - now).total_seconds(),
            "minimum_remaining_seconds": minimum_seconds,
            "minimum_remaining_tool_cost": money(minimum_fee),
            "minimum_total_cost": money(self.minimum_total_cost(state, minimum_fee)),
            "remaining_decisions": 12 - state["decision_rounds"],
            "remaining_tool_calls": 12 - state["tool_calls"],
            **todos.context(state),
        }

    def result(self, state, **extra):
        estimate = state["estimates"].get(state["current_estimate_id"])
        plan_cost = estimate["plan_cost"] if estimate else None
        total = money(Decimal(state["tool_cost"]) + Decimal(plan_cost)) if plan_cost else None
        budget = state["input"].get("budget")
        costs = {
            "tool_cost": state["tool_cost"],
            "plan_cost": plan_cost,
            "total_cost": total,
            "remaining_budget_after_tools": money(Decimal(budget) - Decimal(state["tool_cost"]))
            if "budget" not in state["input_errors"]
            else None,
            "budget_gap": money(max(Decimal(0), Decimal(total) - Decimal(budget)))
            if total
            else None,
        }
        gaps = {}
        if state["started_at"] and not state["input_errors"]:
            fees, seconds = self.minimum_remaining(state)
            minimum_total = self.minimum_total_cost(state, fees)
            gaps["minimum_total_cost"] = money(minimum_total)
            gaps["minimum_total_budget_gap"] = money(
                max(Decimal(0), minimum_total - Decimal(budget))
            )
            gaps["minimum_remaining_tool_cost"] = money(fees)
            gaps["tool_budget_lower_bound_gap"] = money(
                max(Decimal(0), Decimal(state["tool_cost"]) + fees - Decimal(budget))
            )
            available = (
                parse_time(state["input"]["deadline"]) - parse_time(state["simulated_now"])
            ).total_seconds()
            gaps["minimum_time_gap_seconds"] = max(0, seconds - available)
        return {
            "task_id": state["task_id"],
            "run_id": state["run_id"],
            "mode": state["mode"],
            "status": state["status"],
            "phase": state["phase"],
            "reason": state["reason"],
            "summary": state.get("summary"),
            "input": state["input"],
            "input_errors": state["input_errors"],
            "decision_rounds": state["decision_rounds"],
            "tool_calls": state["tool_calls"],
            "resume_count": state.get("resume_count", 0),
            "resume_history": copy.deepcopy(state.get("resume_history", [])),
            "costs": costs,
            "gaps": gaps,
            "ledger": state["ledger"],
            "started_at": state["started_at"],
            "elapsed_seconds": state["elapsed_seconds"],
            "simulated_now": state["simulated_now"],
            "steps": state["steps"],
            "pending_plan": state["pending_plan"],
            "todo_list": copy.deepcopy(state.get("todo_list", [])),
            "plan_revisions": copy.deepcopy(state.get("plan_revisions", [])),
            "todo_tracking": "recorded" if "todo_list" in state else "not_recorded",
            "saved": state["saved"],
            "task_directory": str(self.storage.directory(state["task_id"])),
            "trace_path": str(self.storage.directory(state["task_id"]) / "trace.jsonl"),
            "telemetry": state.get("telemetry", {"status": "not_recorded"}),
            **extra,
        }

    def mark_unconfirmed(self, state):
        for step in state["steps"]:
            if step["status"] == "running":
                step.update(status="failed", outcome_unknown=True)
        for entry in state["ledger"]:
            if entry["outcome"] == "running":
                entry["outcome"] = "unknown"

    async def finish(self, state, reason, summary=None):
        self.mark_unconfirmed(state)
        if state["status"] != "succeeded":
            state["status"] = "incomplete"
        state.update(phase="finished", reason=reason, summary=summary)
        todos.close(state, reason)
        event(state, "task_finished", status=state["status"], reason=reason, summary=summary)
        await self.storage.save(state)
        return self.result(state)

    async def run_task(self, raw):
        if not isinstance(raw, dict):
            raw = {}
        task_id = raw.get("task_id") or uuid4().hex
        if not isinstance(task_id, str):
            task_id = uuid4().hex
        normalized, errors = validate_input(raw)
        identity = fingerprint(normalized)
        lock = self.locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            existing = self.states.get(task_id) or await self.storage.load(task_id)
            if existing:
                if existing["input_fingerprint"] != identity:
                    return {
                        "task_id": task_id,
                        "status": "needs_input",
                        "reason": "TASK_INPUT_CONFLICT",
                        "reused": False,
                    }
                return self.result(existing, reused=True)
            state = {
                "task_id": task_id,
                "run_id": uuid4().hex,
                "input": normalized,
                "input_fingerprint": identity,
                "input_errors": errors,
                "mode": self.model.mode,
                "status": "needs_input" if errors else "running",
                "phase": "finished" if errors else "queued",
                "reason": "INPUT_REQUIRED" if errors else None,
                "started_at": None,
                "simulated_now": None,
                "elapsed_seconds": 0,
                "decision_rounds": 0,
                "tool_calls": 0,
                "tool_cost": "0.00",
                "tool_ordinals": {},
                "retrieved_materials": {},
                "searched": False,
                "plans": {},
                "estimates": {},
                "current_plan_ref": None,
                "current_estimate_id": None,
                "saved": None,
                "steps": [],
                "pending_plan": [],
                "ledger": [],
                "events": [],
                "seen_call_ids": [],
                "scenario": self.scenario.model_dump(),
                "config": self.settings.public_snapshot(),
                "data_snapshot": copy.deepcopy(self.tools.data),
            }
            self.states[task_id] = state
            event(state, "task_created", mode=state["mode"], input=normalized, input_errors=errors)
            todos.initialize(state)
            await self.storage.save(state)
        return await self._execute_task(state)

    async def resume_task(self, task_id):
        lock = self.locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            state = self.states.get(task_id) or await self.storage.load(task_id)
            if state is None:
                return {
                    "task_id": task_id,
                    "status": "needs_input",
                    "reason": "TASK_NOT_FOUND",
                    "resumed": False,
                }
            if state["status"] in ("running", "succeeded"):
                return self.result(state, reused=True, resumed=False)
            error = None
            if state["input_errors"]:
                error = "INPUT_REQUIRED"
            elif state["mode"] != self.model.mode:
                error = "RESUME_MODE_MISMATCH"
            if error:
                return self.result(state, reused=True, resumed=False, resume_error=error)
            original = state
            state = copy.deepcopy(state)
            history = state.setdefault("resume_history", [])
            history.append(
                {
                    "previous_reason": state["reason"],
                    "decision_rounds": state["decision_rounds"],
                    "tool_calls": state["tool_calls"],
                    "simulated_now": state["simulated_now"],
                }
            )
            state["resume_count"] = len(history)
            state.update(status="running", phase="queued", reason=None, summary=None)
            todos.resume(state, self.remaining_tools(state))
            event(state, "task_resumed", resume_count=state["resume_count"], **history[-1])
            try:
                await self.storage.save(state)
            except BaseException:
                self.states[task_id] = original
                raise
            self.states[task_id] = state
        return await self._execute_task(state, resumed=True)

    async def _execute_task(self, state, resumed=False):
        state["telemetry"] = self.telemetry.reference(state["run_id"])
        with self.telemetry.task(state) as observation:
            try:
                result = await self._execute_scheduled(state, resumed)
                if resumed:
                    result["resumed"] = True
            except BaseException:
                observation.update(output=task_output(self.result(state), state))
                raise
            observation.update(
                output=task_output(result, state),
                level="DEFAULT" if result["status"] == "succeeded" else "WARNING",
                status_message=result["reason"],
            )
            return result

    async def _execute_scheduled(self, state, resumed=False):
        if state["input_errors"]:
            todos.close(state, "INPUT_REQUIRED")
            await self.storage.save(state)
            return self.result(state)
        try:
            async with self.semaphore:
                self.active_tasks += 1
                self.peak_active_tasks = max(self.peak_active_tasks, self.active_tasks)
                try:
                    if resumed and await self.executor.reconcile_save(state):
                        return await self.finish(state, "SAVED", "已核对中断前实际保存的方案")
                    return await self._run(state, resumed)
                finally:
                    self.active_tasks -= 1
        except asyncio.CancelledError:
            await asyncio.shield(self.finish(state, "CANCELLED", "用户取消，保留已确认结果"))
            raise
        except OSError:
            # 无法确认持久化时，不宣称任务成功。
            self.mark_unconfirmed(state)
            state.update(status="incomplete", phase="finished", reason="STORAGE_ERROR")
            todos.close(state, "STORAGE_ERROR")
            return self.result(state)
        except ToolError as exc:
            return await self.finish(state, exc.code, str(exc))
        except Exception as exc:
            return await self.finish(state, "INTERNAL_ERROR", f"运行异常：{type(exc).__name__}")

    async def _run(self, state, resumed=False):
        if state["started_at"] is None:
            fixed_start = state["scenario"]["fixed_start"]
            start = (
                parse_time(fixed_start) if fixed_start else datetime.now(ZoneInfo("Asia/Shanghai"))
            )
            state.update(started_at=start.isoformat(), simulated_now=start.isoformat())
        state["phase"] = "executing"
        if parse_time(state["input"]["deadline"]) <= parse_time(state["simulated_now"]):
            return await self.finish(state, "DEADLINE_EXCEEDED", "截止时间已到")
        if self.model.mode == "real" and not self.settings.deepseek_api_key.get_secret_value():
            return await self.finish(state, "CONFIGURATION_ERROR", "请配置 DEEPSEEK_API_KEY")
        messages = [{"role": "system", "content": PLANNER_PROMPT}]
        while state["decision_rounds"] < 12 and state["tool_calls"] < 12:
            state["decision_rounds"] += 1
            context = self.context(state)
            if resumed and len(messages) == 1:
                context["resume"] = copy.deepcopy(state["resume_history"][-1])
                context["previous_steps"] = copy.deepcopy(state["steps"])
            messages.append(
                {"role": "user", "content": "<context>" + dumps(context) + "</context>"}
            )
            await self.executor.record(state, "decision_context", context=context)
            try:
                decision = await self.model.decide(
                    context, messages, lambda kind, **kw: self.executor.record(state, kind, **kw)
                )
            except ModelError as exc:
                return await self.finish(state, exc.code, str(exc))
            await self.executor.record(
                state,
                "model_decision",
                content=decision.content,
                tool_calls=[call.message() for call in decision.calls],
            )
            if not decision.calls:
                reason, summary = "MODEL_STOPPED_WITHOUT_SAVED_PLAN", decision.content
                try:
                    stopped = json.loads(decision.content or "{}")
                    if isinstance(stopped, dict):
                        summary = stopped.get("summary", summary)
                except (ValueError, TypeError):
                    pass
                # Machine-readable reason is derived from observable constraints.
                if context["remaining_seconds"] < context["minimum_remaining_seconds"]:
                    reason = "DEADLINE_INFEASIBLE"
                elif Decimal(context["minimum_total_cost"]) > Decimal(state["input"]["budget"]):
                    reason = "BUDGET_INFEASIBLE"
                elif state["searched"] and not state["retrieved_materials"]:
                    reason = "MATERIALS_INSUFFICIENT"
                return await self.finish(state, reason, summary)
            ids = [call.id for call in decision.calls]
            if (
                any(not isinstance(i, str) or not i for i in ids)
                or len(set(ids)) != len(ids)
                or any(i in state["seen_call_ids"] for i in ids)
            ):
                return await self.finish(state, "MODEL_PROTOCOL_ERROR", "工具调用 ID 缺失或重复")
            state["seen_call_ids"].extend(ids)
            messages.append(
                {
                    "role": "assistant",
                    "content": decision.content,
                    "tool_calls": [call.message() for call in decision.calls],
                }
            )
            barrier = "BATCH_TOO_LARGE" if len(ids) > 12 else None
            for call in decision.calls:
                if state["status"] == "succeeded":
                    barrier = "TASK_ALREADY_SAVED"
                elif state["tool_calls"] >= 12:
                    barrier = "EXECUTION_LIMIT_REACHED"
                if barrier:
                    observation = await self.executor.skipped(state, call, barrier)
                else:
                    observation = await self.executor.execute(state, call)
                    if observation["requires_replan"]:
                        barrier = "REPLAN_REQUIRED"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": dumps(observation)}
                )
            if state["status"] == "succeeded":
                return await self.finish(state, "SAVED", "方案已按约束保存")
        return await self.finish(state, "EXECUTION_LIMIT_REACHED", "达到执行上限，尚未保存可行方案")

    async def run_batch(self, inputs):
        results = await asyncio.gather(*(self.run_task(raw) for raw in inputs))
        for index, result in enumerate(results):
            if result.get("reused"):
                state = self.states.get(result["task_id"]) or await self.storage.load(
                    result["task_id"]
                )
                results[index] = self.result(state, reused=True)
        return {"results": results, "peak_active_tasks": self.peak_active_tasks}

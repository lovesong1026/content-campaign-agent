import json
from datetime import timedelta
from decimal import Decimal

from pydantic import ValidationError

from .schemas import ARGUMENTS, ModelError, ToolError, fingerprint, money, parse_time
from .telemetry import tool_input
from .trace import event


class Executor:
    def __init__(self, tools, storage, telemetry=None):
        self.tools = tools
        self.storage = storage
        self.telemetry = telemetry

    async def record(self, state, kind, **data):
        event(state, kind, **data)
        await self.storage.save(state)

    async def observe(
        self, state, call, status, *, data=None, error=None, fee="0.00", duration=0, replan=False
    ):
        observation = {
            "call_id": call.id,
            "tool_name": call.name,
            "status": status,
            "data": data,
            "error": error,
            "fee": fee,
            "duration_seconds": duration,
            "cumulative_tool_cost": state["tool_cost"],
            "simulated_now": state["simulated_now"],
            "requires_replan": replan,
        }
        step = next((step for step in state["steps"] if step["call_id"] == call.id), None)
        if step is None:
            step = {
                "call_id": call.id,
                "tool_name": call.name,
                "arguments": call.arguments,
                "depends_on": [],
            }
            state["steps"].append(step)
        step.update(status=status, observation=observation)
        await self.storage.artifact(state, f"results/{fingerprint(call.id)}.json", observation)
        await self.record(state, "tool_result", **observation)
        return observation

    async def skipped(self, state, call, reason):
        if self.telemetry is None:
            return await self._skipped(state, call, reason)
        with self.telemetry.observation(
            call.name if call.name in ARGUMENTS else "unknown-tool",
            "retriever" if call.name == "search_materials" else "tool",
            input=tool_input(call.arguments),
            metadata={"call_id": call.id, "decision_round": state["decision_rounds"]},
        ) as observation:
            result = await self._skipped(state, call, reason)
            observation.update(output=result, level="WARNING", status_message=reason)
            return result

    async def _skipped(self, state, call, reason):
        return await self.observe(
            state,
            call,
            "skipped",
            error={"code": reason, "message": "未执行该调用", "retryable": False},
        )

    async def execute(self, state, call):
        if self.telemetry is None:
            return await self._execute(state, call)
        with self.telemetry.observation(
            call.name if call.name in ARGUMENTS else "unknown-tool",
            "retriever" if call.name == "search_materials" else "tool",
            input=tool_input(call.arguments),
            metadata={"call_id": call.id, "decision_round": state["decision_rounds"]},
        ) as observation:
            result = await self._execute(state, call)
            observation.update(
                output=result,
                level="DEFAULT" if result["status"] == "succeeded" else "WARNING",
                status_message=(result.get("error") or {}).get("code"),
            )
            return result

    async def _execute(self, state, call):
        try:
            contract = ARGUMENTS.get(call.name)
            if contract is None:
                raise ToolError("UNKNOWN_TOOL", "未知业务工具")
            try:
                args = contract.model_validate_json(call.arguments)
            except (ValidationError, ValueError) as exc:
                raise ToolError("INVALID_TOOL_ARGUMENTS", "工具参数不符合 JSON Schema") from exc
            self.tools.dependencies(call.name, args, state)
            rule = self.tools.data["tools"][call.name]
            fee, duration = rule["fee"], rule["duration_seconds"]
            if Decimal(state["tool_cost"]) + Decimal(fee) > Decimal(state["input"]["budget"]):
                raise ToolError("BUDGET_BLOCKED", "本次工具费用将超出预算")
            completed = parse_time(state["simulated_now"]) + timedelta(seconds=duration)
            if completed > parse_time(state["input"]["deadline"]):
                raise ToolError("DEADLINE_BLOCKED", "本工具无法在截止时间前完成")
            if call.name == "save_plan":
                estimate = state["estimates"][args.estimate_id]
                if Decimal(state["tool_cost"]) + Decimal(fee) + Decimal(
                    estimate["plan_cost"]
                ) > Decimal(state["input"]["budget"]):
                    raise ToolError("PLAN_OVER_BUDGET", "最新工具账本与方案费用之和超预算")
        except ToolError as exc:
            return await self.observe(
                state,
                call,
                "blocked",
                error={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
                replan=True,
            )

        arguments = json.loads(call.arguments)
        dependencies = self.tools.dependency_ids(call.name, args, state)
        state["steps"].append(
            {
                "call_id": call.id,
                "tool_name": call.name,
                "arguments": arguments,
                "depends_on": dependencies,
                "status": "running",
            }
        )
        state["pending_plan"] = [
            {"tool_name": name, "status": "pending"} for name in args.remaining_steps
        ]
        state["tool_calls"] += 1
        state["tool_cost"] = money(Decimal(state["tool_cost"]) + Decimal(fee))
        state["elapsed_seconds"] += duration
        state["simulated_now"] = completed.isoformat()
        ordinal = state["tool_ordinals"].get(call.name, 0) + 1
        state["tool_ordinals"][call.name] = ordinal
        entry = {
            "call_id": call.id,
            "tool_name": call.name,
            "fee": fee,
            "duration_seconds": duration,
            "outcome": "running",
        }
        state["ledger"].append(entry)
        await self.record(
            state,
            "tool_started",
            call_id=call.id,
            tool_name=call.name,
            arguments=arguments,
            depends_on=dependencies,
            fee=fee,
            duration_seconds=duration,
        )
        try:
            if ordinal in state["scenario"]["failures"].get(call.name, []):
                raise ToolError("INJECTED_TOOL_FAILURE", "场景注入的一次性工具失败", True)
            data = await self.tools.invoke(
                call.name,
                args,
                state,
                call.id,
                lambda kind, **kw: self.record(state, kind, **kw),
            )
        except (ToolError, ModelError) as exc:
            entry["outcome"] = "failed"
            return await self.observe(
                state,
                call,
                "failed",
                fee=fee,
                duration=duration,
                replan=True,
                error={
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": getattr(exc, "retryable", True),
                },
            )
        except (KeyError, ValueError, TypeError) as exc:
            entry["outcome"] = "failed"
            return await self.observe(
                state,
                call,
                "failed",
                fee=fee,
                duration=duration,
                replan=True,
                error={
                    "code": "TOOL_RESULT_INVALID",
                    "message": f"工具结果不符合约定：{type(exc).__name__}",
                    "retryable": False,
                },
            )
        entry["outcome"] = "succeeded"
        replan = call.name == "search_materials" and not data["materials"]
        if call.name == "estimate_cost":
            replan = not data["within_budget"] or completed + timedelta(
                seconds=self.tools.data["tools"]["save_plan"]["duration_seconds"]
            ) > parse_time(state["input"]["deadline"])
        if call.name == "save_plan":
            state["saved"] = data
            state["status"] = "succeeded"
        return await self.observe(
            state, call, "succeeded", data=data, fee=fee, duration=duration, replan=replan
        )

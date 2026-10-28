import json
from decimal import Decimal

from .schemas import ToolError, fingerprint, money


class BusinessTools:
    def __init__(self, data_dir, model, storage):
        self.model = model
        self.storage = storage
        self.data = {
            name: json.loads((data_dir / f"{name}.json").read_text())
            for name in ("materials", "pricing", "tools")
        }
        prices = self.data["pricing"]
        values = [
            *prices["materials"].values(),
            *prices["production"].values(),
            prices["publishing"],
        ]
        values += [rule["fee"] for name, rule in self.data["tools"].items() if name != "version"]
        if any(not Decimal(v).is_finite() or Decimal(v) < 0 for v in values):
            raise ValueError("费用规则必须为非负有限金额")

    def dependencies(self, name, args, state):
        if name == "generate_script":
            if args.base_plan_ref and args.base_plan_ref not in state["plans"]:
                raise ToolError("PLAN_NOT_FOUND", "基础方案不存在")
            for item in args.items:
                for asset_id in item.asset_ids:
                    asset = state["retrieved_materials"].get(asset_id)
                    if not asset or not asset["available"]:
                        raise ToolError("ASSET_NOT_AVAILABLE", "只能引用本任务已检索的可用素材")
        if name in ("estimate_cost", "save_plan"):
            if args.plan_ref not in state["plans"]:
                raise ToolError("PLAN_NOT_FOUND", "方案必须实际生成成功")
        if name == "save_plan":
            plan = state["plans"][args.plan_ref]
            estimate = state["estimates"].get(args.estimate_id)
            if args.plan_ref != state["current_plan_ref"]:
                raise ToolError("STALE_PLAN", "只能保存当前候选版本")
            if (
                not estimate
                or estimate["plan_ref"] != args.plan_ref
                or estimate["content_hash"] != plan["content_hash"]
                or estimate["pricing_version"] != self.data["pricing"]["version"]
            ):
                raise ToolError("STALE_ESTIMATE", "估算不存在或与当前版本不匹配")

    def dependency_ids(self, name, args, state):
        if name == "generate_script":
            return sorted(
                {
                    state["retrieved_materials"][a]["source_call_id"]
                    for i in args.items
                    for a in i.asset_ids
                }
            )
        if name in ("estimate_cost", "save_plan"):
            ids = [state["plans"][args.plan_ref]["source_call_id"]]
            if name == "save_plan":
                ids.append(state["estimates"][args.estimate_id]["source_call_id"])
            return ids
        return []

    async def invoke(self, name, args, state, call_id, record):
        if name == "search_materials":
            return self.search(args, state, call_id)
        if name == "generate_script":
            return await self.generate(args, state, call_id, record)
        if name == "estimate_cost":
            return self.estimate(args, state, call_id)
        return await self.save(args, state)

    def search(self, args, state, call_id):
        matches = []
        if not state["scenario"]["empty_materials"]:
            query = args.query.casefold()
            for asset in self.data["materials"]:
                if not asset["available"] or args.material_type not in ("all", asset["type"]):
                    continue
                if any(tag.casefold() in query for tag in asset["tags"]) or query in asset["title"]:
                    found = {**asset, "source_call_id": call_id}
                    matches.append(found)
                    state["retrieved_materials"][asset["asset_id"]] = found
        state["searched"] = True
        return {"search_result_id": f"search-{state['tool_calls']}", "materials": matches}

    async def generate(self, args, state, call_id, record):
        request = {
            "theme": state["input"]["theme"],
            "audience": args.audience,
            "channel": args.channel,
            "items": [item.model_dump() for item in args.items],
            "materials": list(state["retrieved_materials"].values()),
        }
        draft = await self.model.generate_script(request, record)
        if len(draft.items) != len(args.items):
            raise ToolError("SCRIPT_ITEM_MISMATCH", "脚本条数与请求不同", True)
        for expected, actual in zip(args.items, draft.items, strict=True):
            if (
                actual.asset_ids != expected.asset_ids
                or actual.production_tier != expected.production_tier
            ):
                raise ToolError("SCRIPT_REFERENCE_MISMATCH", "生成模型擅自修改素材或规格", True)
        version = len(state["plans"]) + 1
        plan = {
            "theme": state["input"]["theme"],
            "audience": args.audience,
            "channel": args.channel,
            "items": [item.model_dump() for item in draft.items],
        }
        plan.update(
            content_hash=fingerprint(plan),
            plan_id="campaign",
            version=version,
            plan_ref=f"campaign-v{version}",
            source_call_id=call_id,
        )
        await self.storage.artifact(state, f"plans/campaign-v{version}.json", plan)
        state["plans"][plan["plan_ref"]] = plan
        state["current_plan_ref"] = plan["plan_ref"]
        state["current_estimate_id"] = None
        return plan

    def estimate(self, args, state, call_id):
        plan = state["plans"][args.plan_ref]
        rules = self.data["pricing"]
        assets = {a for item in plan["items"] for a in item["asset_ids"]}
        breakdown = [
            {"kind": "material", "id": asset, "amount": rules["materials"][asset]}
            for asset in sorted(assets)
        ]
        breakdown += [
            {
                "kind": "production",
                "video": n,
                "amount": rules["production"][item["production_tier"]],
            }
            for n, item in enumerate(plan["items"], 1)
        ]
        breakdown.append({"kind": "publishing", "amount": rules["publishing"]})
        cost = sum((Decimal(row["amount"]) for row in breakdown), Decimal(0))
        total = cost + Decimal(state["tool_cost"])
        result = {
            "estimate_id": f"estimate-{state['tool_calls']}",
            "plan_ref": args.plan_ref,
            "content_hash": plan["content_hash"],
            "pricing_version": rules["version"],
            "breakdown": breakdown,
            "plan_cost": money(cost),
            "tool_cost_at_estimate": state["tool_cost"],
            "total_at_estimate": money(total),
            "within_budget": total <= Decimal(state["input"]["budget"]),
            "budget_gap": money(max(Decimal(0), total - Decimal(state["input"]["budget"]))),
            "source_call_id": call_id,
        }
        state["estimates"][result["estimate_id"]] = result
        state["current_estimate_id"] = result["estimate_id"]
        return result

    async def save(self, args, state):
        estimate = state["estimates"][args.estimate_id]
        saved_id = "saved-" + state["run_id"]
        relative = f"saved/{saved_id}.json"
        result = {
            "saved_plan_id": saved_id,
            "path": relative,
            "content_hash": state["plans"][args.plan_ref]["content_hash"],
            "plan_ref": args.plan_ref,
            "estimate_id": args.estimate_id,
            "tool_cost": state["tool_cost"],
            "plan_cost": estimate["plan_cost"],
            "total_cost": money(Decimal(state["tool_cost"]) + Decimal(estimate["plan_cost"])),
            "simulated_saved_at": state["simulated_now"],
        }
        await self.storage.artifact(
            state, relative, {**result, "plan": state["plans"][args.plan_ref], "estimate": estimate}
        )
        return result

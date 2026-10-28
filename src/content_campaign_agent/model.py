import asyncio
from decimal import Decimal
from uuid import uuid4

import openai
from openai import AsyncOpenAI

from .prompts import PLANNER_VERSION, SCRIPT_PROMPT, SCRIPT_VERSION
from .schemas import (
    ARGUMENTS,
    Decision,
    ModelError,
    ScriptDraft,
    ToolCall,
    dumps,
    fingerprint,
)
from .telemetry import Telemetry

DESCRIPTIONS = {
    "search_materials": "检索本地可用合成素材。返回素材ID及事实；空列表说明素材不足。",
    "generate_script": "用已检索素材生成新的方案及脚本。重写也收费，可改变条数与制作规格。",
    "estimate_cost": "估算指定已生成方案，返回方案费用与已含本次调用的工具累计费用。",
    "save_plan": "保存当前已估算且符合预算、截止时间的方案，返回实际保存结果。",
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": contract.model_json_schema(),
        },
    }
    for name, contract in ARGUMENTS.items()
]


class DeepSeekClient:
    mode = "real"

    def __init__(self, settings, telemetry=None):
        self.settings = settings
        self.telemetry = telemetry or Telemetry(settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_model_requests)
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value() or "not-configured",
            base_url=settings.deepseek_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=0,
        )

    async def close(self):
        try:
            await self.client.close()
        finally:
            await self.telemetry.flush()

    async def request(self, role, messages, record, **kwargs):
        async with self.semaphore:
            version = PLANNER_VERSION if role == "planner" else SCRIPT_VERSION
            with self.telemetry.observation(
                "decide-next-action" if role == "planner" else "write-video-scripts",
                "generation",
                model=self.settings.deepseek_model,
                input=messages,
                version=version,
                model_parameters={
                    "max_tokens": kwargs.get("max_tokens"),
                    "thinking": "disabled",
                },
                metadata={
                    "role": role,
                    "prompt_hash": fingerprint(messages[0]["content"]),
                    "tools": kwargs.get("tools"),
                    "response_format": kwargs.get("response_format"),
                },
            ) as observation:
                return await self._request(role, messages, record, observation, **kwargs)

    async def _request(self, role, messages, record, observation, **kwargs):
        version = PLANNER_VERSION if role == "planner" else SCRIPT_VERSION
        await record(
            "model_request",
            role=role,
            prompt_version=version,
            prompt_hash=fingerprint(messages[0]["content"]),
            model=self.settings.deepseek_model,
            sdk_version=openai.__version__,
        )
        try:
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                response = await self.client.chat.completions.create(
                    model=self.settings.deepseek_model,
                    messages=messages,
                    extra_body={"thinking": {"type": "disabled"}},
                    **kwargs,
                )
        except (TimeoutError, openai.APITimeoutError) as exc:
            code = "MODEL_TIMEOUT"
            await record("model_error", role=role, code=code)
            raise ModelError(code, "模型请求超时，未自动重试") from exc
        except openai.APIError as exc:
            code = (
                f"MODEL_HTTP_{exc.status_code}"
                if hasattr(exc, "status_code")
                else "MODEL_NETWORK_ERROR"
            )
            await record("model_error", role=role, code=code)
            # Do not persist provider error bodies or authentication headers.
            raise ModelError(code, "模型接口请求失败，详情仅记录状态代码") from exc
        await record(
            "model_response",
            role=role,
            model=response.model,
            request_id=getattr(response, "_request_id", None),
            usage=response.usage.model_dump() if response.usage else None,
            finish_reason=response.choices[0].finish_reason if response.choices else None,
        )
        usage = response.usage.model_dump() if response.usage else {}
        usage_details = {
            key: value
            for key, value in {
                "input": usage.get("prompt_tokens"),
                "output": usage.get("completion_tokens"),
                "total": usage.get("total_tokens"),
            }.items()
            if isinstance(value, int)
        }
        observation.update(
            model=response.model,
            output=[choice.message.model_dump() for choice in response.choices]
            if response.choices and hasattr(response.choices[0].message, "model_dump")
            else [],
            usage_details=usage_details,
            metadata={
                "provider_usage": usage,
                "request_id": getattr(response, "_request_id", None),
                "finish_reasons": [choice.finish_reason for choice in response.choices],
            },
        )
        if not response.choices:
            raise ModelError("MODEL_PROTOCOL_ERROR", "模型响应没有 choices")
        if response.choices[0].finish_reason == "length":
            raise ModelError("MODEL_OUTPUT_TRUNCATED", "模型输出被截断")
        return response.choices[0].message

    async def decide(self, context, messages, record):
        message = await self.request("planner", messages, record, tools=TOOLS, max_tokens=2048)
        calls = []
        for call in message.tool_calls or []:
            if call.type != "function":
                raise ModelError("MODEL_PROTOCOL_ERROR", "仅支持函数工具调用")
            calls.append(ToolCall(call.id, call.function.name, call.function.arguments))
        return Decision(calls, message.content)

    async def generate_script(self, request, record):
        message = await self.request(
            "script",
            [
                {"role": "system", "content": SCRIPT_PROMPT},
                {"role": "user", "content": dumps(request)},
            ],
            record,
            response_format={"type": "json_object"},
            max_tokens=4096,
        )
        try:
            return ScriptDraft.model_validate_json(message.content or "")
        except ValueError as exc:
            raise ModelError("INVALID_SCRIPT_JSON", "脚本响应为空或不符合约定结构") from exc


class OfflineMockClient:
    """Deterministic engineering fixture; never evidence of real model decisions."""

    mode = "mock"

    def __init__(self, policy="normal"):
        self.policy = policy

    async def close(self):
        pass

    def call(self, name, **args):
        args.setdefault(
            "remaining_steps",
            {
                "search_materials": ["generate_script", "estimate_cost", "save_plan"],
                "generate_script": ["estimate_cost", "save_plan"],
                "estimate_cost": ["save_plan"],
                "save_plan": [],
            }[name],
        )
        return Decision([ToolCall("mock-" + uuid4().hex, name, dumps(args))])

    async def decide(self, context, messages, record):
        await record("mock_decision", policy=self.policy, model="offline-deterministic-v1")
        if self.policy == "loop":
            return self.call("search_materials", query="春季", decision_note="离线循环上限测试")
        if context["remaining_seconds"] < context["minimum_remaining_seconds"]:
            return Decision(
                content=dumps({"reason_code": "DEADLINE_INFEASIBLE", "summary": "剩余时间不足"})
            )
        if Decimal(context["minimum_total_cost"]) > Decimal(context["input"]["budget"]):
            return Decision(
                content=dumps({"reason_code": "BUDGET_INFEASIBLE", "summary": "预算不足"})
            )
        assets = context["retrieved_materials"]
        if not assets:
            if context["searched"]:
                return Decision(
                    content=dumps(
                        {"reason_code": "MATERIALS_INSUFFICIENT", "summary": "无可用素材"}
                    )
                )
            return self.call("search_materials", query="春季新品", decision_note="检索可用素材")
        plan = context["current_plan"]
        estimate = context["current_estimate"]
        if plan and not estimate:
            return self.call(
                "estimate_cost", plan_ref=plan["plan_ref"], decision_note="取得真实估算"
            )
        if estimate and estimate["within_budget"]:
            return self.call(
                "save_plan",
                plan_ref=plan["plan_ref"],
                estimate_id=estimate["estimate_id"],
                decision_note="估算合规，保存方案",
            )
        premium = self.policy == "premium_first" and not context["plans_count"]
        return self.call(
            "generate_script",
            audience="年轻消费者",
            channel="短视频平台",
            items=[
                {
                    "asset_ids": ["premium-spring" if premium else "basic-spring"],
                    "production_tier": "premium" if premium else "economy",
                    "requirements": "展示春季新品的日常使用场景",
                }
            ],
            decision_note="生成初稿" if not plan else "观察到超预算，换基础素材和经济规格",
        )

    async def generate_script(self, request, record):
        await record("mock_script", model="offline-template-v1")
        return ScriptDraft.model_validate(
            {
                "items": [
                    {
                        "title": request["theme"][:60],
                        "asset_ids": item["asset_ids"],
                        "production_tier": item["production_tier"],
                        "hook": "用一个日常场景开启春日新体验。",
                        "body": "以自然光展示新品外观，切换使用场景与产品细节。",
                        "cta": "查看新品介绍，了解适合自己的使用方式。",
                        "shots": ["场景全景", "产品外观特写", "使用镜头"],
                    }
                    for item in request["items"]
                ]
            }
        )

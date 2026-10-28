import asyncio
import json
from contextlib import contextmanager

import httpx
import openai
import pytest
from langfuse import Langfuse
from openai.types.chat import ChatCompletion
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from content_campaign_agent.agent import Agent
from content_campaign_agent.config import Settings
from content_campaign_agent.model import DeepSeekClient, OfflineMockClient
from content_campaign_agent.schemas import Decision, Scenario, ToolCall, dumps
from content_campaign_agent.telemetry import Telemetry


@pytest.fixture(scope="module")
def sdk_client():
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-offline-test",
        secret_key="sk-lf-offline-test",
        base_url="http://localhost:9",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )
    yield client, exporter
    client.shutdown()


@pytest.fixture
def observed(tmp_path, sdk_client):
    client, exporter = sdk_client
    client.flush()
    exporter.clear()
    settings = Settings(
        _env_file=None,
        output_dir=tmp_path,
        deepseek_api_key="secret-deepseek-fixture",
        langfuse_secret_key="secret-langfuse-fixture",
        langfuse_public_key="public-langfuse-fixture",
        langfuse_enabled=False,
    )
    telemetry = Telemetry(settings)
    telemetry.client = client
    telemetry.status = "ready"
    return settings, telemetry, exporter


async def fake_completion(**kwargs):
    await asyncio.sleep(0)
    mock = OfflineMockClient()

    async def record(*args, **kwargs):
        pass

    if "tools" in kwargs:
        context = json.loads(kwargs["messages"][-1]["content"][9:-10])
        decision = await mock.decide(context, kwargs["messages"], record)
        message = {
            "role": "assistant",
            "content": decision.content,
            "tool_calls": [call.message() for call in decision.calls] or None,
        }
    else:
        request = json.loads(kwargs["messages"][-1]["content"])
        draft = await mock.generate_script(request, record)
        message = {"role": "assistant", "content": draft.model_dump_json()}
    return ChatCompletion.model_validate(
        {
            "id": "provider-fixture",
            "created": 1,
            "model": "deepseek-flash",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {
                "prompt_tokens": 35,
                "completion_tokens": 12,
                "total_tokens": 47,
                "prompt_cache_hit_tokens": 7,
            },
        }
    )


async def test_sdk_hierarchy_usage_concurrent_isolation_and_reuse(observed, task, monkeypatch):
    settings, telemetry, exporter = observed
    model = DeepSeekClient(settings, telemetry)
    monkeypatch.setattr(model.client.chat.completions, "create", fake_completion)
    agent = Agent(settings, model, Scenario(fixed_start="2030-01-01T10:00:00+08:00"))
    try:
        results = (await agent.run_batch([{**task, "task_id": f"trace-{i}"} for i in range(3)]))[
            "results"
        ]
        await telemetry.flush()
        count = len(exporter.get_finished_spans())
        repeated = await agent.run_task({**task, "task_id": "trace-0"})
        await telemetry.flush()
        assert repeated["reused"] and len(exporter.get_finished_spans()) == count
        assert all(result["status"] == "succeeded" for result in results)
        assert len({result["telemetry"]["trace_id"] for result in results}) == 3
        spans = exporter.get_finished_spans()
        for result in results:
            tree = [span for span in spans if f"{span.context.trace_id:032x}" == result["run_id"]]
            assert len(tree) == 10  # root, four planners, four tools, one script generation
            root = next(span for span in tree if span.name == "plan-content-campaign")
            script_tool = next(span for span in tree if span.name == "generate_script")
            for span in tree:
                if span.name == "write-video-scripts":
                    assert span.parent.span_id == script_tool.context.span_id
                elif span is not root:
                    assert span.parent.span_id == root.context.span_id
                if span.attributes["langfuse.observation.type"] == "generation":
                    usage = json.loads(span.attributes["langfuse.observation.usage_details"])
                    assert usage == {"input": 35, "output": 12, "total": 47}
                    assert span.attributes["langfuse.observation.model.name"] == "deepseek-flash"
                    messages = json.loads(span.attributes["langfuse.observation.output"])
                    assert messages and all(message["role"] == "assistant" for message in messages)
            root_output = json.loads(root.attributes["langfuse.observation.output"])
            assert root_output["simulated_costs"]["total_cost"] == "5.00"
            assert root_output["plan"]["items"]
    finally:
        await model.close()


async def test_sdk_failed_tool_preserves_fee_and_error(observed, task):
    settings, telemetry, exporter = observed
    agent = Agent(
        settings,
        OfflineMockClient(),
        Scenario(fixed_start="2030-01-01T10:00:00+08:00", failures={"generate_script": [1]}),
        telemetry=telemetry,
    )
    result = await agent.run_task(task)
    await telemetry.flush()
    assert result["status"] == "succeeded" and result["costs"]["tool_cost"] == "6.00"
    scripts = [span for span in exporter.get_finished_spans() if span.name == "generate_script"]
    failed = next(
        span for span in scripts if span.attributes.get("langfuse.observation.level") == "WARNING"
    )
    output = json.loads(failed.attributes["langfuse.observation.output"])
    assert output["fee"] == "2.00"
    assert output["error"]["code"] == "INJECTED_TOOL_FAILURE"
    assert not any(
        span.attributes["langfuse.observation.type"] == "generation"
        for span in exporter.get_finished_spans()
    )


async def test_sdk_masks_secrets_and_credentials_excluded_from_snapshot(observed, task):
    settings, telemetry, exporter = observed
    agent = Agent(
        settings,
        OfflineMockClient(),
        Scenario(fixed_start="2030-01-01T10:00:00+08:00"),
        telemetry=telemetry,
    )
    await agent.run_task({**task, "theme": "春季 " + settings.deepseek_api_key.get_secret_value()})
    with telemetry.observation(
        "redaction", input={"Authorization": "Bearer other-key", "api_key": "other-key"}
    ):
        pass
    await telemetry.flush()
    payload = dumps([dict(span.attributes) for span in exporter.get_finished_spans()])
    assert "secret-deepseek-fixture" not in payload and "other-key" not in payload
    assert "[REDACTED]" in payload
    assert "secret-langfuse-fixture" not in dumps(settings.public_snapshot())
    assert "public-langfuse-fixture" not in dumps(settings.public_snapshot())


@pytest.mark.parametrize("operation", ["start", "update", "end", "flush"])
async def test_telemetry_failures_do_not_change_business_result(make_agent, task, operation):
    class BrokenClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            if operation == "start":
                raise RuntimeError("private-provider-error")
            try:
                yield self
            finally:
                if operation == "end":
                    raise RuntimeError("private-provider-error")

        def update(self, **kwargs):
            if operation == "update":
                raise RuntimeError("private-provider-error")

        def flush(self):
            if operation == "flush":
                raise RuntimeError("private-provider-error")

    agent = make_agent()
    agent.telemetry.client = BrokenClient()
    result = await agent.run_task(task)
    await agent.telemetry.flush()
    assert result["status"] == "succeeded"
    assert result["costs"]["total_cost"] == "5.00"
    assert agent.telemetry.errors > 0


async def test_sdk_invalid_input_records_warning_without_tools(observed, task):
    settings, telemetry, exporter = observed
    agent = Agent(settings, OfflineMockClient(), telemetry=telemetry)
    result = await agent.run_task({**task, "budget": None})
    await telemetry.flush()
    spans = exporter.get_finished_spans()
    assert result["status"] == "needs_input" and len(spans) == 1
    assert spans[0].attributes["langfuse.observation.level"] == "WARNING"


def test_disabled_or_missing_credentials_does_not_initialize_sdk(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("must not initialize SDK")

    monkeypatch.setattr("langfuse.Langfuse", unexpected)
    disabled = Telemetry(Settings(_env_file=None, langfuse_enabled=False))
    missing = Telemetry(
        Settings(
            _env_file=None, langfuse_enabled=True, langfuse_public_key="", langfuse_secret_key=""
        )
    )
    assert disabled.status == "disabled" and missing.status == "not_configured"


async def test_provider_error_exports_code_without_response_body(observed, task, monkeypatch):
    settings, telemetry, exporter = observed
    model = DeepSeekClient(settings, telemetry)

    async def fail(**kwargs):
        raise openai.APIStatusError(
            "private-body-fixture",
            response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")),
            body={"error": "private-body-fixture"},
        )

    monkeypatch.setattr(model.client.chat.completions, "create", fail)
    agent = Agent(settings, model, Scenario(fixed_start="2030-01-01T10:00:00+08:00"))
    try:
        result = await agent.run_task(task)
    finally:
        await model.close()
    assert result["reason"] == "MODEL_HTTP_429"
    payload = dumps([dict(span.attributes) for span in exporter.get_finished_spans()])
    assert "private-body-fixture" not in payload
    assert "MODEL_HTTP_429" in payload


async def test_cancelled_task_has_final_output_and_closed_spans(observed, task):
    settings, telemetry, exporter = observed
    entered = asyncio.Event()

    class WaitingModel(OfflineMockClient):
        async def generate_script(self, request, record):
            entered.set()
            await asyncio.Event().wait()

    agent = Agent(
        settings,
        WaitingModel(),
        Scenario(fixed_start="2030-01-01T10:00:00+08:00"),
        telemetry=telemetry,
    )
    running = asyncio.create_task(agent.run_task(task))
    await asyncio.wait_for(entered.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await telemetry.flush()
    root = next(
        span for span in exporter.get_finished_spans() if span.name == "plan-content-campaign"
    )
    assert json.loads(root.attributes["langfuse.observation.output"])["reason"] == "CANCELLED"
    assert root.attributes["langfuse.observation.level"] == "ERROR"


async def test_skipped_calls_are_visible_and_not_charged(observed, task):
    settings, telemetry, exporter = observed

    class MultipleCalls(OfflineMockClient):
        async def decide(self, context, messages, record):
            if not context["searched"] and context["tool_cost"] == "0.00":
                return Decision(
                    [
                        ToolCall(
                            str(index),
                            "search_materials",
                            dumps({"query": "春季", "decision_note": "test"}),
                        )
                        for index in range(2)
                    ]
                )
            return await super().decide(context, messages, record)

    agent = Agent(
        settings,
        MultipleCalls(),
        Scenario(fixed_start="2030-01-01T10:00:00+08:00", failures={"search_materials": [1]}),
        telemetry=telemetry,
    )
    result = await agent.run_task(task)
    await telemetry.flush()
    outputs = [
        json.loads(span.attributes["langfuse.observation.output"])
        for span in exporter.get_finished_spans()
        if span.name == "search_materials"
    ]
    skipped = next(output for output in outputs if output["status"] == "skipped")
    assert skipped["fee"] == "0.00" and skipped["error"]["code"] == "REPLAN_REQUIRED"
    assert result["status"] == "succeeded" and result["costs"]["total_cost"] == "6.00"

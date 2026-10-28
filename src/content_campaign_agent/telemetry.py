"""Optional, failure-isolated Langfuse observations with explicit safe payloads."""

import asyncio
import json
import logging
from contextlib import contextmanager

from .schemas import fingerprint

logger = logging.getLogger(__name__)
REDACTED = "[REDACTED]"
SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "secret_key",
    "public_key",
    "password",
    "deepseek_api_key",
    "langfuse_secret_key",
    "langfuse_public_key",
}


class Observation:
    def __init__(self, telemetry, span=None):
        self.telemetry = telemetry
        self.span = span

    def update(self, **kwargs):
        if self.span is not None:
            try:
                self.span.update(**self.telemetry.mask(kwargs))
            except Exception:
                self.telemetry.failed("update")


class Telemetry:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.errors = 0
        self.secrets = tuple(
            value.get_secret_value()
            for value in (
                settings.deepseek_api_key,
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
            )
            if value.get_secret_value()
        )
        if not settings.langfuse_enabled:
            self.status = "disabled"
        elif not (
            settings.langfuse_public_key.get_secret_value()
            and settings.langfuse_secret_key.get_secret_value()
        ):
            self.status = "not_configured"
        else:
            try:
                from langfuse import Langfuse

                self.client = Langfuse(
                    public_key=settings.langfuse_public_key.get_secret_value(),
                    secret_key=settings.langfuse_secret_key.get_secret_value(),
                    base_url=settings.langfuse_base_url,
                    environment=settings.langfuse_environment,
                    timeout=5,
                    mask=self.mask,
                )
                self.status = "ready"
            except Exception:
                self.status = "initialization_failed"
                self.failed("initialize")

    def mask(self, data, **kwargs):
        if isinstance(data, dict):
            return {
                str(key): REDACTED if str(key).casefold() in SENSITIVE_KEYS else self.mask(value)
                for key, value in data.items()
            }
        if isinstance(data, (list, tuple)):
            return [self.mask(value) for value in data]
        if isinstance(data, str):
            for secret in self.secrets:
                data = data.replace(secret, REDACTED)
            return data
        return data

    def failed(self, operation):
        self.errors += 1
        # Never log exception bodies: SDK/exporter errors may contain remote payloads.
        logger.warning("Langfuse %s failed; local execution continues", operation)

    @contextmanager
    def observation(self, name, as_type="span", **kwargs):
        manager = None
        span = None
        if self.client is not None:
            try:
                manager = self.client.start_as_current_observation(
                    name=name, as_type=as_type, **self.mask(kwargs)
                )
                span = manager.__enter__()
            except Exception:
                manager = None
                self.failed("start")
        handle = Observation(self, span)
        try:
            yield handle
        except BaseException as exc:
            handle.update(
                level="ERROR",
                status_message=getattr(exc, "code", type(exc).__name__),
            )
            raise
        finally:
            if manager is not None:
                try:
                    # Exceptions are represented by safe codes above, not raw bodies.
                    manager.__exit__(None, None, None)
                except Exception:
                    self.failed("end")

    @contextmanager
    def task(self, state):
        with self.observation(
            "plan-content-campaign",
            "agent",
            trace_context={"trace_id": state["run_id"]},
            input=state["input"],
            metadata={
                "task_id": state["task_id"],
                "run_id": state["run_id"],
                "mode": state["mode"],
                "scenario": state["scenario"]["name"],
                "pricing_version": state["data_snapshot"]["pricing"]["version"],
            },
        ) as span:
            # Attribute propagation is context-local, including under asyncio.gather.
            manager = None
            if self.client is not None:
                try:
                    from langfuse import propagate_attributes

                    manager = propagate_attributes(
                        session_id=fingerprint(state["task_id"]),
                        trace_name="plan-content-campaign",
                        tags=[state["mode"], "content-campaign"],
                    )
                    manager.__enter__()
                except Exception:
                    manager = None
                    self.failed("propagate")
            try:
                yield span
            finally:
                if manager is not None:
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        self.failed("propagate_end")

    def reference(self, run_id):
        result = {"status": self.status}
        if self.client is not None:
            result.update(trace_id=run_id, delivery="unverified")
            if self.settings.langfuse_project_id:
                result["trace_url"] = (
                    f"{self.settings.langfuse_base_url.rstrip('/')}/project/"
                    f"{self.settings.langfuse_project_id}/traces/{run_id}"
                )
        return result

    async def flush(self):
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.flush)
            except Exception:
                self.failed("flush")


def tool_input(arguments):
    try:
        return json.loads(arguments)
    except (TypeError, ValueError):
        return {"invalid_arguments": True}


def task_output(result, state):
    plan = state["plans"].get(state["current_plan_ref"])
    return {
        "status": result["status"],
        "reason": result["reason"],
        "summary": result["summary"],
        "saved": result["saved"],
        "plan": plan,
        "simulated_costs": result["costs"],
        "simulated_elapsed_seconds": result["elapsed_seconds"],
        "decision_rounds": result["decision_rounds"],
        "tool_calls": result["tool_calls"],
        "input_errors": result["input_errors"],
    }

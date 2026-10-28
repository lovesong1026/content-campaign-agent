import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

TOOL_NAMES = ["search_materials", "generate_script", "estimate_cost", "save_plan"]
ToolName = Literal["search_materials", "generate_script", "estimate_cost", "save_plan"]
Tier = Literal["economy", "standard", "premium"]


def dumps(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, default=str, **kwargs)


def fingerprint(value):
    return hashlib.sha256(dumps(value, sort_keys=True).encode()).hexdigest()


def money(value):
    return format(Decimal(str(value)).quantize(Decimal("0.01")), ".2f")


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def validate_input(raw):
    """Return normalized input and all errors without making a model request."""
    normalized = {key: raw.get(key) for key in ("theme", "budget", "deadline")}
    errors = {}
    theme = raw.get("theme")
    if not isinstance(theme, str) or not theme.strip():
        errors["theme"] = "需要非空活动主题"
    else:
        normalized["theme"] = theme.strip()
    try:
        value = raw.get("budget")
        if value is None or isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError
        normalized["budget"] = money(amount)
    except (ValueError, InvalidOperation):
        errors["budget"] = "需要非负预算，最多两位小数"
    try:
        if not raw.get("deadline"):
            raise ValueError
        normalized["deadline"] = (
            parse_time(raw["deadline"]).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
        )
    except (ValueError, TypeError, OverflowError):
        errors["deadline"] = "需要有效 ISO 8601 截止时间，默认北京时间"
    return normalized, errors


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommonArgs(Contract):
    decision_note: str = Field(min_length=1, max_length=500)
    remaining_steps: list[ToolName] | None = Field(default=None, max_length=12)


class SearchArgs(CommonArgs):
    query: str = Field(min_length=1, max_length=1000)
    material_type: Literal["video", "all"] = "all"


class ItemSpec(Contract):
    asset_ids: list[str] = Field(min_length=1, max_length=5)
    production_tier: Tier
    requirements: str = Field(default="", max_length=2000)


class GenerateArgs(CommonArgs):
    audience: str = Field(min_length=1, max_length=300)
    channel: str = Field(min_length=1, max_length=100)
    items: list[ItemSpec] = Field(min_length=1, max_length=3)
    base_plan_ref: str | None = None


class EstimateArgs(CommonArgs):
    plan_ref: str = Field(min_length=1, max_length=100)


class SaveArgs(EstimateArgs):
    estimate_id: str = Field(min_length=1, max_length=100)


class VideoItem(Contract):
    title: str = Field(min_length=1)
    asset_ids: list[str] = Field(min_length=1)
    production_tier: Tier
    hook: str = Field(min_length=1)
    body: str = Field(min_length=1)
    cta: str = Field(min_length=1)
    shots: list[str] = Field(min_length=1)


class ScriptDraft(Contract):
    items: list[VideoItem] = Field(min_length=1, max_length=3)


class Scenario(Contract):
    name: str = "default"
    fixed_start: str | None = None
    empty_materials: bool = False
    failures: dict[ToolName, list[int]] = Field(default_factory=dict)


ARGUMENTS = dict(zip(TOOL_NAMES, [SearchArgs, GenerateArgs, EstimateArgs, SaveArgs], strict=True))


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def message(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Decision:
    calls: list[ToolCall] = field(default_factory=list)
    content: str | None = None


class ModelError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ToolError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

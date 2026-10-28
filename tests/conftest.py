import pytest

from content_campaign_agent.agent import Agent
from content_campaign_agent.config import Settings
from content_campaign_agent.model import OfflineMockClient
from content_campaign_agent.schemas import Scenario


@pytest.fixture(autouse=True)
def disable_live_telemetry(monkeypatch):
    # Unit tests must never upload fixtures using a developer's shell credentials.
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")


@pytest.fixture
def make_agent(tmp_path):
    def make(model=None, scenario=None, **settings):
        config = Settings(_env_file=None, output_dir=tmp_path, **settings)
        return Agent(
            config,
            model or OfflineMockClient(),
            scenario or Scenario(fixed_start="2030-01-01T10:00:00+08:00"),
        )

    return make


@pytest.fixture
def task():
    return {
        "task_id": "test",
        "theme": "春季新品推广",
        "budget": "20",
        "deadline": "2030-01-01T10:30:00+08:00",
    }

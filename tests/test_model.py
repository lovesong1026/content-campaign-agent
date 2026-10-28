import asyncio
from types import SimpleNamespace

from content_campaign_agent.config import Settings
from content_campaign_agent.model import DeepSeekClient


async def test_request_semaphore_and_no_hidden_retry(monkeypatch):
    model = DeepSeekClient(
        Settings(_env_file=None, deepseek_api_key="fixture", max_concurrent_model_requests=2)
    )
    running = 0
    peak = 0
    received = []
    release = asyncio.Event()
    entered_two = asyncio.Event()

    async def create(**kwargs):
        nonlocal running, peak
        received.append(kwargs)
        running += 1
        peak = max(peak, running)
        if running == 2:
            entered_two.set()
        await release.wait()
        running -= 1
        return SimpleNamespace(
            model="fixture",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="{}", tool_calls=None)
                )
            ],
        )

    monkeypatch.setattr(model.client.chat.completions, "create", create)
    events = []

    async def record(kind, **data):
        events.append((kind, data))

    tasks = [
        asyncio.create_task(model.decide({}, [{"role": "system", "content": "fixture"}], record))
        for _ in range(5)
    ]
    await asyncio.wait_for(entered_two.wait(), timeout=2)
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2 and len(received) == 5
    assert model.client.max_retries == 0
    assert all(request["extra_body"]["thinking"]["type"] == "disabled" for request in received)
    assert "fixture" not in str([data.get("api_key") for _, data in events])
    await model.close()

import asyncio
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .schemas import dumps, fingerprint
from .trace import event


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Storage:
    def __init__(self, root):
        self.root = Path(root)

    def directory(self, task_id):
        return self.root / "tasks" / fingerprint(task_id)

    @contextmanager
    def writer(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".writer.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("OUTPUT_DIRECTORY_BUSY") from exc
            try:
                yield self
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    async def load(self, task_id):
        path = self.directory(task_id) / "task.json"
        return await asyncio.to_thread(
            lambda: json.loads(path.read_text()) if path.exists() else None
        )

    async def save(self, state):
        directory = self.directory(state["task_id"])
        # The snapshot is authoritative; the JSONL file is a reproducible projection.
        snapshot = dumps(state, indent=2) + "\n"
        trace = "".join(dumps(item) + "\n" for item in state["events"])
        await asyncio.to_thread(atomic_text, directory / "task.json", snapshot)
        await asyncio.to_thread(atomic_text, directory / "trace.jsonl", trace)

    async def artifact(self, state, relative, data):
        await asyncio.to_thread(
            atomic_text, self.directory(state["task_id"]) / relative, dumps(data, indent=2) + "\n"
        )
        return relative

    async def recover_interrupted(self):
        for path in self.root.glob("tasks/*/task.json"):
            state = await asyncio.to_thread(lambda p=path: json.loads(p.read_text()))
            if state["status"] == "running":
                state.update(status="incomplete", phase="finished", reason="EXECUTION_INTERRUPTED")
                for step in state["steps"]:
                    if step["status"] == "running":
                        step.update(status="failed", outcome_unknown=True)
                for entry in state["ledger"]:
                    if entry["outcome"] == "running":
                        entry["outcome"] = "unknown"
                for step in state["pending_plan"]:
                    step["status"] = "skipped"
                event(state, "task_finished", reason="EXECUTION_INTERRUPTED")
                await self.save(state)

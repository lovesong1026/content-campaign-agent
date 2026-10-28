"""The published synthetic dataset doubles as parameterized regression coverage."""

import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = runpy.run_path(str(ROOT / "scripts/run_synthetic.py"))
CASES = json.loads((ROOT / "examples/synthetic/cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_synthetic_case(case, tmp_path):
    _, _, checked = await RUNNER["run_case"](case, tmp_path)
    assert checked["passed"], checked


async def test_evaluator_detects_wrong_expectation(tmp_path):
    case = copy.deepcopy(next(case for case in CASES if case["id"] == "budget-exact"))
    case["expectedOutput"]["total_cost"] = "4.99"
    _, _, checked = await RUNNER["run_case"](case, tmp_path)
    assert not checked["passed"]
    assert not checked["checks"]["total_cost"]


async def test_runner_refuses_to_overwrite_evidence(tmp_path):
    marker = tmp_path / "original.txt"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        await RUNNER["run"](tmp_path)
    assert marker.read_text() == "keep"

"""Free-form requests (tests/freeform/*.json) must keep passing: see scripts/eval_freeform.py."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from eval_freeform import run_set  # noqa: E402


@pytest.mark.parametrize("name", ["dev", "heldout", "blind"])
def test_freeform_set(name):
    res = asyncio.run(run_set(ROOT / "tests" / "freeform" / f"{name}.json"))
    failed = {c["id"]: c["fails"] for c in res["cases"] if not c["pass"]}
    assert not failed, failed

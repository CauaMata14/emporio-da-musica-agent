import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def frozen_date(monkeypatch, tmp_path):
    """Freeze 'today' and isolate runtime files so tests are deterministic."""
    from emporio_agent import config

    monkeypatch.setenv("AGENT_REFERENCE_DATE", "2026-03-20")  # a Friday
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    yield

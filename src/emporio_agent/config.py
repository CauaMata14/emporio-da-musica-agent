"""Central configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

DATA_RAW = ROOT_DIR / "data" / "raw"
DATA_PROCESSED = ROOT_DIR / "data" / "processed"
RUNTIME_DIR = ROOT_DIR / "data" / "runtime"

# Campo Grande/MS is UTC-4 all year (no DST since 2019).
STORE_TZ = ZoneInfo("America/Campo_Grande")


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.getenv("CLAUDE_EFFORT", "low"))
    enable_fallback: bool = field(
        default_factory=lambda: os.getenv("ENABLE_FALLBACK", "1").lower() not in {"0", "false", "no"}
    )
    max_tokens: int = 4096
    max_tool_rounds: int = 8
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("AGENT_DB_PATH", RUNTIME_DIR / "conversations.db"))
    )


def now() -> datetime:
    """Current time in the store's timezone.

    AGENT_REFERENCE_DATE freezes the date (time fixed at 10:00) so that demos and tests are
    reproducible — the dataset's orders are from 2025/early 2026.
    """
    ref = os.getenv("AGENT_REFERENCE_DATE")
    if ref:
        d = date.fromisoformat(ref)
        return datetime(d.year, d.month, d.day, 10, 0, tzinfo=STORE_TZ)
    return datetime.now(STORE_TZ)


def today() -> date:
    return now().date()

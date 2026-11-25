from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.config import get_settings


def test_password_with_special_characters_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd #1 'x'")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    get_settings.cache_clear()
    try:
        parsed = conninfo_to_dict(get_settings().database_url)
    finally:
        get_settings.cache_clear()
    assert parsed["password"] == "p@ss:w/rd #1 'x'" and parsed["host"] == "db"

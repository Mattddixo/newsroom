from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.settings import Settings
from newsroom.web.app import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, rate_limit="1000/minute")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))

"""Shared pytest fixtures: fresh temp DB + TestClient per test."""

import tempfile

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import create_app


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_db(tmp.name)
    app = create_app()
    with TestClient(app) as c:
        yield c

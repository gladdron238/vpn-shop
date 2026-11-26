import os
import tempfile

# Настройки для тестов задаём до импорта приложения: своя пустая база, демо, без бота.
_tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(_tmp, "test.db"), DEMO="1", PANEL="mock",
                  BOT_TOKEN="", CRYPTOPAY_TOKEN="", ADMIN_ID="0", SITE_URL="http://test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth(client):
    token = client.post("/api/auth/demo").json()["token"]
    return {"Authorization": "Bearer " + token}

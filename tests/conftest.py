import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import create_app
from app.config import Config
from app.db import db
from app.curriculum import seed_curriculum

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def app(tmp_path):
    cfg = Config(
        {
            "TESTING": "true",
            "SECRET_KEY": "test-secret-key",
            "LLM_PROVIDER": "fake",
            "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
            "RESOURCE_INDEX_PATH": os.path.join(REPO_ROOT, "app", "data", "resources.sqlite"),
        }
    )
    application = create_app(cfg)
    with application.app_context():
        db.create_all()
        seed_curriculum(os.path.join(REPO_ROOT, "curriculum", "topics.yaml"))
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()

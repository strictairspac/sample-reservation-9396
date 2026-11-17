import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# These tests truncate business tables.  They never run against the default
# demo database: an explicit disposable TEST_DATABASE_URL is required.
if not os.getenv('TEST_DATABASE_URL'):
    pytest.skip('set TEST_DATABASE_URL to a disposable PostgreSQL database', allow_module_level=True)
os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']

from src.config.database import Base, engine  # noqa: E402
from src.models import entities  # noqa: E402,F401
from src.services import reservations  # noqa: E402
from scripts import seed_demo  # noqa: E402


@pytest.fixture(scope='session', autouse=True)
def schema():
    # The tests own a disposable database: drop first so schema changes apply.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reservations.bootstrap()
    yield


@pytest.fixture(autouse=True)
def clean():
    with engine.begin() as conn:
        conn.execute(text(
            'TRUNCATE receipts, trial_balances, trials, reservations, reservation_runs, '
            'audit_logs, transfers, samples RESTART IDENTITY CASCADE'
        ))
        conn.execute(text("UPDATE reservation_settings SET mode='off' WHERE id=1"))
    seed_demo.seed()
    yield

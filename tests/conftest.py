"""Shared fixtures: env store over SQLite (default) or PostgreSQL (when Docker is available)."""

import pytest
from sqlalchemy import text

from settings import Settings
from storage import Store


def sql(conn, query: str, *params):
    """Test helper: run raw SQL with '?' placeholders against a SQLAlchemy connection."""
    if len(params) == 1 and isinstance(params[0], (tuple, list)):
        params = tuple(params[0])
    parts = query.split('?')
    if len(parts) != len(params) + 1:
        raise ValueError('SQL placeholder count does not match parameters')
    statement, binds = parts[0], {}
    for index, value in enumerate(params):
        key = f'p{index}'
        statement += f':{key}' + parts[index + 1]
        binds[key] = value
    return conn.execute(text(statement), binds)


def _docker_available() -> bool:
    import shutil
    import subprocess
    if not shutil.which('docker'):
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@pytest.fixture
def env(tmp_path):
    settings = Settings(_env_file=None, database_url=None,
                        database_path=tmp_path / 'test.db', skills_path=tmp_path / 'skills',
                        public_origin='http://testserver')
    store = Store(settings.database_path)
    store.init_db()
    from services.accounts import create_user
    user = create_user(store, 'alice', 'long-test-password')
    other = create_user(store, 'bob', 'another-password')
    yield settings, store, user, other
    store.dispose()


def pytest_addoption(parser):
    parser.addoption('--run-postgres', action='store_true', default=False,
                     help='Run PostgreSQL tests against an isolated schema or testcontainer')


def pytest_collection_modifyitems(config, items):
    if config.getoption('--run-postgres'):
        return
    for item in items:
        if 'postgres' in item.keywords:
            item.add_marker(pytest.mark.skip(reason='Use --run-postgres to enable PostgreSQL tests'))


@pytest.fixture(scope='session')
def postgres_url():
    import os

    explicit = os.environ.get('DATA_AGENT_TEST_POSTGRES_URL')
    if explicit:
        yield explicit
        return
    if not _docker_available():
        pytest.skip('Docker unavailable; set DATA_AGENT_TEST_POSTGRES_URL for an isolated test server')
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer('postgres:16', driver='psycopg') as container:
        yield container.get_connection_url()


@pytest.fixture
def pg_env(postgres_url, tmp_path):
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema

    from services.accounts import create_user

    url = make_url(postgres_url).set(drivername='postgresql+psycopg')
    schema = 'test_agent_' + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    isolated = url.update_query_dict({'options': f'-csearch_path={schema}'})
    settings = Settings(_env_file=None, database_url=isolated.render_as_string(hide_password=False),
                        skills_path=tmp_path / 'skills', public_origin='http://testserver')
    store = Store(settings.resolved_database_url)
    try:
        store.init_db()
        user = create_user(store, 'alice', 'long-test-password')
        other = create_user(store, 'bob', 'another-password')
        yield settings, store, user, other
    finally:
        store.dispose()
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()

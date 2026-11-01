"""
EAP-38: the eap_app role's password comes from APP_DATABASE_URL, not from migration 0005.

The resolution is a pure function in the migration module, loaded here from its file (alembic
version files are not a package). The integration half is every CI run: a fresh Postgres,
`alembic upgrade head` with APP_DATABASE_URL set, and then a suite that connects as eap_app
with the password that URL carries.
"""

import importlib.util
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0005_application_database_role.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_unset_means_the_dev_default() -> None:
    m = _load()
    assert m.app_role_password(None) == "eap_app"
    assert m.DEFAULT_PASSWORD == "eap_app"


def test_the_password_inside_app_database_url_wins() -> None:
    m = _load()
    assert m.app_role_password("postgresql+asyncpg://eap_app:s3cret@db:5432/eap") == "s3cret"


def test_url_encoded_passwords_are_decoded() -> None:
    # A password with '@' or '/' has to be percent-encoded inside a URL; the role must get
    # the real characters, not the encoding.
    m = _load()
    assert m.app_role_password("postgresql+asyncpg://eap_app:p%40ss%2Fw0rd@db:5432/eap") == "p@ss/w0rd"


def test_a_url_without_a_password_falls_back_to_the_default() -> None:
    m = _load()
    assert m.app_role_password("postgresql+asyncpg://eap_app@db:5432/eap") == "eap_app"

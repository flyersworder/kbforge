import pytest
from sql_testdb import URL_ENV, flat_cfg, make_db
from sqlalchemy.exc import OperationalError, ProgrammingError

import kbforge_sql.connector as mod
from kbforge_sql.connector import CONNECTOR
from kbforge_sql.errors import SqlSourceError


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(mod, "_sleep", calls.append)
    return calls


def _operational(message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(message))


def test_a_transient_error_is_retried_then_succeeds(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    real = mod._query_once
    failures = iter([_operational("server closed the connection")])

    def flaky(url, query):
        for exc in failures:
            raise exc
        return real(url, query)

    monkeypatch.setattr(mod, "_query_once", flaky)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert len(result.records) == 3
    assert sleeps == [1]


def test_transient_errors_exhaust_the_retries(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)

    def down(url, query):
        raise _operational("connection refused")

    monkeypatch.setattr(mod, "_query_once", down)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(retries=2), None)
    assert str(exc.value) == (
        "sql source 'products': OperationalError after 3 attempt(s): connection refused"
    )
    assert sleeps == [1, 2]


def test_a_programming_error_is_not_retried(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)

    def bad_sql(url, query):
        raise ProgrammingError(
            "SELECT", {}, Exception('relation "prodcut" does not exist')
        )

    monkeypatch.setattr(mod, "_query_once", bad_sql)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        "sql source 'products': ProgrammingError: relation \"prodcut\" does not exist"
    )
    assert sleeps == []


def test_the_password_never_appears_in_an_error(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_SQL_PASSWORD", "s3cret-pw")

    def leaky(url, query):
        raise _operational("login failed for password s3cret-pw")

    monkeypatch.setattr(mod, "_query_once", leaky)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(
            flat_cfg(password_env="KB_SQL_PASSWORD", retries=0), None
        )
    assert "s3cret-pw" not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_password_inside_the_url_is_redacted_too(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    monkeypatch.setenv(URL_ENV, "postgresql://kb:inline-pw@db.example/x")

    def leaky(url, query):
        raise _operational("auth failed: inline-pw")

    monkeypatch.setattr(mod, "_query_once", leaky)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(retries=0), None)
    assert "inline-pw" not in str(exc.value)


def test_an_unparseable_url_is_reported_without_its_value(tmp_path, monkeypatch):
    monkeypatch.setenv(URL_ENV, "not a url s3cret")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        f"sql source 'products': the value of {URL_ENV} is not a SQLAlchemy URL"
    )


def test_a_missing_driver_names_the_remedy(tmp_path, monkeypatch):
    monkeypatch.setenv(URL_ENV, "nosuchdialect://kb@db.example/x")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value).startswith(
        "sql source 'products': no SQLAlchemy dialect or driver for this URL "
        "is installed ("
    )

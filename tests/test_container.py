"""Deployment credentials must survive encoding without leaking to logs."""

import pytest
from sqlalchemy.engine import make_url

from backend.container import configure_environment, main


def database_env():
    return {
        "FINANCE_DB_HOST": "postgres",
        "FINANCE_DB_PORT": "5432",
        "FINANCE_DB_USER": "finance",
        "FINANCE_DB_NAME": "finance",
        "FINANCE_DB_PASSWORD": "test-only:@/#$?% +汉字",
    }


def test_password_round_trip():
    env = database_env()
    configure_environment(env)
    url = make_url(env["FINANCE_DATABASE_URL"])
    assert url.password == env["FINANCE_DB_PASSWORD"]
    assert (url.drivername, url.host, url.port, url.database) == (
        "postgresql+psycopg", "postgres", 5432, "finance",
    )


def test_explicit_url_wins_over_container_fields():
    env = database_env()
    env["FINANCE_DATABASE_URL"] = "sqlite:///:memory:"
    configure_environment(env)
    assert env["FINANCE_DATABASE_URL"] == "sqlite:///:memory:"


def test_local_defaults_do_not_require_container_settings():
    env = {}
    configure_environment(env)
    assert env == {}


@pytest.mark.parametrize("port", ["", "postgres", "0", "65536"])
def test_bad_port_does_not_echo_secret(port):
    env = database_env()
    env["FINANCE_DB_PORT"] = port
    with pytest.raises(ValueError, match="FINANCE_DB_PORT") as raised:
        configure_environment(env)
    assert env["FINANCE_DB_PASSWORD"] not in str(raised.value)


def test_missing_password_names_setting_only():
    env = database_env()
    env.pop("FINANCE_DB_PASSWORD")
    with pytest.raises(ValueError, match="FINANCE_DB_PASSWORD"):
        configure_environment(env)


def test_entrypoint_executes_argument_vector_without_shell(monkeypatch):
    import backend.container as container
    env = database_env()
    monkeypatch.setattr(container.os, "environ", env)
    executed = []
    monkeypatch.setattr(container.os, "execvpe", lambda *args: executed.append(args))
    command = ["python", "-m", "backend.worker", "--poll-seconds", "0.5"]
    assert main(command) == 0
    assert executed[0][:2] == ("python", command)
    assert make_url(executed[0][2]["FINANCE_DATABASE_URL"]).password == env["FINANCE_DB_PASSWORD"]


def test_entrypoint_rejects_invalid_environment_before_exec(monkeypatch, capsys):
    import backend.container as container
    env = database_env()
    env["FINANCE_DB_PORT"] = "invalid"
    monkeypatch.setattr(container.os, "environ", env)
    def unexpected_exec(*args):
        pytest.fail("An invalid environment must not launch the command")
    monkeypatch.setattr(container.os, "execvpe", unexpected_exec)
    assert main(["alembic", "upgrade", "head"]) == 2
    assert env["FINANCE_DB_PASSWORD"] not in capsys.readouterr().err

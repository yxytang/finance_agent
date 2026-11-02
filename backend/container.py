"""Container entrypoint shared by migrations, API and background workers.

Compose passes database fields separately so passwords need no manual URL
encoding. A complete explicit FINANCE_DATABASE_URL takes precedence. Nothing
here logs credentials or invokes a shell.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
import os
import sys

from sqlalchemy.engine import URL


def configure_environment(environ: MutableMapping[str, str]) -> None:
    if environ.get("FINANCE_DATABASE_URL", "").strip():
        return
    if not environ.get("FINANCE_DB_HOST", "").strip():
        return  # Ordinary local commands can still use the SQLite default.

    required = ("FINANCE_DB_USER", "FINANCE_DB_NAME", "FINANCE_DB_PASSWORD")
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("Missing container database settings: " + ", ".join(missing))
    try:
        port = int(environ.get("FINANCE_DB_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("FINANCE_DB_PORT must be an integer from 1 to 65535") from None

    environ["FINANCE_DATABASE_URL"] = URL.create(
        "postgresql+psycopg",
        username=environ["FINANCE_DB_USER"],
        password=environ["FINANCE_DB_PASSWORD"],
        host=environ["FINANCE_DB_HOST"].strip(),
        port=port,
        database=environ["FINANCE_DB_NAME"],
    ).render_as_string(hide_password=False)


def main(argv: Sequence[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        print("Container entrypoint requires a command", file=sys.stderr)
        return 2
    try:
        configure_environment(os.environ)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.execvpe(command[0], command, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

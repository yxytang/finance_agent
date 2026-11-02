"""Migration environment shared by local SQLite and PostgreSQL deployments."""
from alembic import context
from backend.db import Database, default_url
from backend.models import Base

config = context.config
target_metadata = Base.metadata
url = config.attributes.get("database_url") or default_url()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    connection = config.attributes["connection"]
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()
else:
    db = Database(url)
    try:
        with db.engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              compare_type=True, render_as_batch=connection.dialect.name == "sqlite")
            with context.begin_transaction():
                context.run_migrations()
    finally:
        db.dispose()

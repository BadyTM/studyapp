"""Alembic environment.

There are no SQLAlchemy models in this project, so `--autogenerate` does nothing
useful -- migrations are written by hand. SQLAlchemy is present only because
Alembic runs on it.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# What alembic.ini carries when nobody has said otherwise. It is a relative path,
# so taking it at face value would migrate a file in whatever folder alembic was
# started from rather than the one the app opens.
PLACEHOLDER = "sqlite:///studyapp.db"


def database_url() -> str:
    """Which database to migrate.

    `studyapp.db.connection.alembic_config` sets this explicitly, so the app and
    the tests always name their own file. The bare `alembic upgrade head` does
    not -- and without this it would quietly create a second, empty database in
    the project folder, migrate that, and report success. `*.db` is gitignored,
    so nothing would ever show it had happened.
    """
    configured = config.get_main_option("sqlalchemy.url")
    if configured and configured != PLACEHOLDER:
        return configured

    from studyapp import config as app_config

    return f"sqlite:///{app_config.db_path().as_posix()}"


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = dict(config.get_section(config.config_ini_section, {}))
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Batch mode rewrites the table for ALTERs SQLite cannot do in place.
        # Turning it on now saves rewriting later migrations when it is needed.
        context.configure(connection=connection, target_metadata=None, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

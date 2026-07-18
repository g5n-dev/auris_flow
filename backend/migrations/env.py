from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def ensure_mysql_alembic_version_length(connection: Connection) -> None:
    if connection.dialect.name not in {"mysql", "mariadb"}:
        return

    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(128) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
        ")"
    )
    connection.exec_driver_sql(
        "ALTER TABLE alembic_version MODIFY version_num VARCHAR(128) NOT NULL"
    )


def run_migrations_offline() -> None:
    settings = get_settings()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = get_settings()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.database_url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        ensure_mysql_alembic_version_length(connection)
        if connection.in_transaction():
            connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

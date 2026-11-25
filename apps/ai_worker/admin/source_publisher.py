"""Dedicated credentials and effective grants for trusted Source aggregate publication."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from ai_worker.admin.source_writer import validate_source_writer_session
from infra.python.source_role_policy import SOURCE_PUBLISHER_LOCK_COLUMNS, SOURCE_TABLES

_OTHER_PASSWORDS = (
    "DB_PASSWORD",
    "DB_APP_PASSWORD",
    "DB_MIGRATION_PASSWORD",
    "DB_ADMIN_PASSWORD",
    "SOURCE_WRITER_PASSWORD",
    "SOURCE_MANAGEMENT_PASSWORD",
    "SOURCE_CLEANUP_EXECUTOR_PASSWORD",
    "CATALOG_WRITER_PASSWORD",
    "CATALOG_APPROVAL_PASSWORD",
    "KNOWLEDGE_INDEX_BUILDER_PASSWORD",
    "CANDIDATE_INDEX_BUILDER_PASSWORD",
    "SOURCE_CAPTURE_ISSUER_PASSWORD",
)


@dataclass(frozen=True)
class SourcePublisherConfig:
    url: URL = field(repr=False)
    actor: str

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "SourcePublisherConfig":
        if any(env.get(key) for key in _OTHER_PASSWORDS):
            raise ValueError("Source Publisher requires an isolated credential environment")
        values = {
            key: env.get(f"SOURCE_PUBLISHER_{key}", "") for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD", "ACTOR")
        }
        if any(not value.strip() for value in values.values()):
            raise ValueError("Source Publisher configuration is incomplete")
        if len(values["ACTOR"]) > 100:
            raise ValueError("Source Publisher actor is too long")
        port = int(values["PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("Source Publisher port is invalid")
        return cls(
            URL.create(
                "postgresql+asyncpg",
                username=values["USER"],
                password=values["PASSWORD"],
                host=values["HOST"],
                port=port,
                database=values["NAME"],
            ),
            values["ACTOR"],
        )


async def validate_source_publisher_session(session: AsyncSession) -> None:
    """Require the publisher's actual allowlist, including effective column grants."""
    await validate_source_writer_session(session)
    safe_login = await session.scalar(
        text("SELECT current_user=session_user AND NOT has_schema_privilege(current_user,current_schema(),'CREATE')")
    )
    if safe_login is not True:
        raise ValueError("Source Publisher requires a direct non-owner login without schema creation privileges")
    sequence_access = await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind='S' "
            "AND CASE WHEN c.relkind='S' THEN (has_sequence_privilege(current_user,c.oid,'SELECT') "
            "OR has_sequence_privilege(current_user,c.oid,'UPDATE') "
            "OR has_sequence_privilege(current_user,c.oid,'USAGE')) ELSE false END)"
        )
    )
    if sequence_access is not False:
        raise ValueError("Source Publisher must not receive sequence privileges")
    rows = (
        await session.execute(
            text(
                "SELECT c.relname,p.privilege FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')) p(privilege) "
                "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f') "
                "AND has_table_privilege(current_user,c.oid,p.privilege)"
            )
        )
    ).all()
    expected = {(table, privilege) for table in SOURCE_TABLES for privilege in ("SELECT", "INSERT")}
    if set(rows) != expected:
        raise ValueError("Source Publisher table privileges do not match the allowlist")
    columns = (
        await session.execute(
            text(
                "SELECT c.relname,a.attname,p.privilege FROM pg_attribute a "
                "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) p(privilege) "
                "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped "
                "AND has_column_privilege(current_user,c.oid,a.attnum,p.privilege)"
            )
        )
    ).all()
    for table, column, privilege in columns:
        if (table, privilege) in expected or (
            privilege == "UPDATE" and SOURCE_PUBLISHER_LOCK_COLUMNS.get(table) == column
        ):
            continue
        raise ValueError("Source Publisher column privileges do not match the allowlist")
    if not {(table, column, "UPDATE") for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items()} <= set(columns):
        raise ValueError("Source Publisher row-lock privileges are missing")

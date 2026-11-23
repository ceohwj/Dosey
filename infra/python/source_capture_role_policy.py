"""Narrow synthetic capture privileges; no role creation or production grant wiring."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from infra.python.source_role_policy import quoted_identifier

_CAPTURE = "rag_source_capture_observation"
_METADATA = (
    "rag_source",
    "rag_source_endpoint",
    "rag_source_operation",
    "rag_source_snapshot",
    "rag_source_ingestion_run",
    "rag_source_ingestion_artifact",
)


async def _require_no_metadata_writes(connection: AsyncConnection, schema: str, reader: str, issuer: str) -> None:
    for name in _METADATA:
        table_name = f"{schema}.{name}"
        for role in (reader, issuer):
            writable = await connection.scalar(
                text(
                    "SELECT has_table_privilege(:role, :table_name, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                    "OR has_any_column_privilege(:role, :table_name, 'INSERT,UPDATE,REFERENCES')"
                ),
                {"role": role, "table_name": table_name},
            )
            if writable:
                raise ValueError("capture roles must not have Source metadata write grants")


async def apply_source_capture_role_policy(
    connection: AsyncConnection, *, schema: str, reader: str, issuer: str
) -> None:
    if reader == issuer:
        raise ValueError("capture reader and issuer must differ")
    schema_sql, reader_sql, issuer_sql = map(quoted_identifier, (schema, reader, issuer))
    roles = (
        (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolbypassrls "
                    "FROM pg_roles WHERE rolname IN (:reader, :issuer)"
                ),
                {"reader": reader, "issuer": issuer},
            )
        )
        .mappings()
        .all()
    )
    if len(roles) != 2 or any(
        row["rolsuper"] or row["rolinherit"] or row["rolcreaterole"] or row["rolbypassrls"] for row in roles
    ):
        raise ValueError("capture roles must be unprivileged and non-inheriting")
    memberships = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
            "WHERE r.rolname IN (:reader, :issuer)"
        ),
        {"reader": reader, "issuer": issuer},
    )
    if memberships:
        raise ValueError("capture roles must not inherit another role")
    elevated = await connection.scalar(
        text(
            "SELECT has_schema_privilege(:reader, :schema, 'CREATE') "
            "OR has_schema_privilege(:issuer, :schema, 'CREATE') "
            "OR EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
            "WHERE n.nspname=:schema AND r.rolname IN (:reader, :issuer)) "
            "OR EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba "
            "WHERE d.datname=current_database() AND r.rolname IN (:reader, :issuer)) "
            "OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname=:schema "
            "AND c.relname=:table AND r.rolname IN (:reader, :issuer))"
        ),
        {"reader": reader, "issuer": issuer, "schema": schema, "table": _CAPTURE},
    )
    if elevated:
        raise ValueError("capture roles cannot own database/schema or CREATE in schema")
    present = set(
        (
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema": schema}
            )
        ).all()
    )
    if not {_CAPTURE, *_METADATA} <= present:
        raise ValueError("source capture migration and metadata tables required")
    await _require_no_metadata_writes(connection, schema, reader, issuer)
    table = f"{schema_sql}.{quoted_identifier(_CAPTURE)}"
    await connection.execute(text(f"REVOKE ALL ON TABLE {table} FROM PUBLIC, {reader_sql}, {issuer_sql}"))
    column_revokes = await connection.scalars(
        text(
            "SELECT DISTINCT format('REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %s', "
            "a.attname, n.nspname, c.relname, CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(r.rolname) END) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(a.attacl) acl "
            "LEFT JOIN pg_roles r ON r.oid=acl.grantee "
            "WHERE n.nspname=:schema AND c.relname=:table AND a.attnum>0 AND NOT a.attisdropped "
            "AND (acl.grantee=0 OR r.rolname IN (:reader, :issuer))"
        ),
        {"schema": schema, "table": _CAPTURE, "reader": reader, "issuer": issuer},
    )
    for statement in column_revokes:
        await connection.execute(text(statement))
    await connection.execute(text(f"REVOKE CREATE ON SCHEMA {schema_sql} FROM {reader_sql}, {issuer_sql}"))
    await connection.execute(text(f"GRANT USAGE ON SCHEMA {schema_sql} TO {reader_sql}, {issuer_sql}"))
    await connection.execute(text(f"GRANT SELECT ON TABLE {table} TO {reader_sql}, {issuer_sql}"))
    await connection.execute(text(f"GRANT INSERT ON TABLE {table} TO {issuer_sql}"))
    for name in _METADATA:
        await connection.execute(text(f"GRANT SELECT ON TABLE {schema_sql}.{quoted_identifier(name)} TO {issuer_sql}"))

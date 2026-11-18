"""Source Writer 전용 권한 적용. 관리자 연결의 외부 transaction에서 실행합니다.

Runtime 프로세스에 관리자/Writer credential을 전달하지 않습니다.
이 모듈은 역할을 생성하거나 비밀번호를 취급하지 않습니다.
"""

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SOURCE_TABLES = (
    "rag_source",
    "rag_source_endpoint",
    "rag_source_operation",
    "rag_source_snapshot",
    "rag_source_snapshot_member",
    "rag_source_ingestion_run",
    "rag_source_ingestion_artifact",
    "rag_source_snapshot_verification",
)
# #807: Source Use Approval is Source governance history, but remains separate from
# the tables exposed to Knowledge Index Builder through SOURCE_TABLES.
SOURCE_USE_APPROVAL_TABLES = ("rag_source_use_approval",)
# 승인 이력·원본은 append-only입니다. 관리용 수정/삭제는 별도 권한 경로로 연결합니다.
WRITER_PUBLICATION_TABLES = (
    "rag_source_ingestion_run",
    "rag_source_ingestion_artifact",
    "rag_source_snapshot",
    "rag_source_snapshot_member",
)
SOURCE_PUBLISHER_LOCK_COLUMNS = {
    "rag_source": "knowledge_index_lock_marker",
    "rag_source_endpoint": "knowledge_index_lock_marker",
    "rag_source_operation": "knowledge_index_lock_marker",
    "rag_source_snapshot_member": "knowledge_index_lock_marker",
    "rag_source_ingestion_run": "knowledge_index_lock_marker",
    "rag_source_ingestion_artifact": "knowledge_index_lock_marker",
    "rag_source_snapshot": "management_lock_marker",
}
WRITER_UPDATE_TABLES = {"rag_source_operation"}
WRITER_LOCK_TABLES = (
    "rag_source",
    "rag_source_endpoint",
    "rag_source_ingestion_run",
    "rag_source_ingestion_artifact",
)


def quoted_identifier(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value) is None:
        raise ValueError("Invalid database role or schema identifier")
    return f'"{value}"'


async def apply_source_role_policy(
    connection: AsyncConnection, *, schema: str, owner: str, runtime: str, writer: str, publisher: str | None = None
) -> None:
    """기존 역할의 안전성을 확인하고 Source 권한을 명시적으로 적용합니다."""
    names = [quoted_identifier(value) for value in (schema, owner, runtime, writer)]
    configured = [owner, runtime, writer, *([publisher] if publisher is not None else [])]
    if len(set(configured)) != len(configured):
        raise ValueError("Migration, Runtime, Writer and Publisher roles must differ")
    schema_sql, owner_sql, runtime_sql, writer_sql = names
    publisher_sql = quoted_identifier(publisher) if publisher is not None else None
    roles = [runtime, writer, *([publisher] if publisher is not None else [])]
    await _validate_role_boundary(connection, schema=schema, runtime=runtime, writer=writer, publisher=publisher)
    global_defaults = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclnamespace=0 AND (d.defaclobjtype='r' OR (d.defaclobjtype='S' AND :has_publisher)) "
            "AND d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=:owner) "
            "AND (a.grantee=0 OR a.grantee IN (SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)))"
        ),
        {"owner": owner, "roles": roles, "has_publisher": publisher is not None},
    )
    if global_defaults:
        raise ValueError("Remove global table default grants before Source role cutover")
    tables = await connection.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema": schema}
    )
    present = set(tables.scalars())
    if not (set(SOURCE_TABLES) | set(SOURCE_USE_APPROVAL_TABLES)).issubset(present):
        raise ValueError("Apply Source migrations before provisioning Writer privileges")
    await _validate_lock_markers(connection, schema=schema, publisher=publisher is not None)
    await connection.execute(text(f"REVOKE CREATE ON SCHEMA {schema_sql} FROM PUBLIC"))
    await connection.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema_sql} FROM {writer_sql}"))
    if publisher_sql is not None:
        await _clear_publisher_grants(
            connection, schema_sql=schema_sql, owner_sql=owner_sql, publisher_sql=publisher_sql
        )
    await _revoke_column_grants(connection, schema=schema, runtime=runtime, writer=writer, publisher=publisher)
    for role_sql in (runtime_sql, writer_sql, *([publisher_sql] if publisher_sql else [])):
        await connection.execute(text(f"REVOKE CREATE ON SCHEMA {schema_sql} FROM {role_sql}"))
        await connection.execute(text(f"GRANT USAGE ON SCHEMA {schema_sql} TO {role_sql}"))
        await connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} "
                f"REVOKE ALL ON TABLES FROM {role_sql}"
            )
        )
    # PUBLIC 경유 권한도 차단하고 신규 테이블에 암묵적인 쓰기 권한을 남기지 않습니다.
    await connection.execute(
        text(f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} REVOKE ALL ON TABLES FROM PUBLIC")
    )
    await _grant_source_permissions(
        connection, schema_sql=schema_sql, runtime_sql=runtime_sql, writer_sql=writer_sql, publisher_sql=publisher_sql
    )


async def _clear_publisher_grants(
    connection: AsyncConnection, *, schema_sql: str, owner_sql: str, publisher_sql: str
) -> None:
    # PUBLIC privileges also flow to Publisher; stale grants outside Source
    # cannot remain an implicit Catalog/Knowledge write path.
    for object_type in ("TABLES", "SEQUENCES"):
        await connection.execute(
            text(f"REVOKE ALL ON ALL {object_type} IN SCHEMA {schema_sql} FROM PUBLIC, {publisher_sql}")
        )
        for recipient in ("PUBLIC", publisher_sql):
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql} IN SCHEMA {schema_sql} "
                    f"REVOKE ALL ON {object_type} FROM {recipient}"
                )
            )


async def _grant_source_permissions(
    connection: AsyncConnection, *, schema_sql: str, runtime_sql: str, writer_sql: str, publisher_sql: str | None
) -> None:
    for table in SOURCE_TABLES + SOURCE_USE_APPROVAL_TABLES:
        target = f"{schema_sql}.{quoted_identifier(table)}"
        await connection.execute(text(f"REVOKE ALL ON TABLE {target} FROM PUBLIC, {runtime_sql}, {writer_sql}"))
        await connection.execute(text(f"GRANT SELECT ON TABLE {target} TO {runtime_sql}, {writer_sql}"))
        if table not in WRITER_PUBLICATION_TABLES:
            await connection.execute(text(f"GRANT INSERT ON TABLE {target} TO {writer_sql}"))
        if publisher_sql is not None and table in SOURCE_TABLES:
            await connection.execute(text(f"GRANT SELECT, INSERT ON TABLE {target} TO {publisher_sql}"))
            marker = SOURCE_PUBLISHER_LOCK_COLUMNS.get(table)
            if marker is not None:
                await connection.execute(
                    text(f"GRANT UPDATE ({quoted_identifier(marker)}) ON TABLE {target} TO {publisher_sql}")
                )
        if table == "rag_source_snapshot":
            await connection.execute(
                text(
                    f"GRANT UPDATE (verification_status, verified_at, effective_at, verification_seal_id) ON TABLE {target} TO {writer_sql}"
                )
            )
        if table in WRITER_LOCK_TABLES:
            await connection.execute(
                text(f"GRANT UPDATE (knowledge_index_lock_marker) ON TABLE {target} TO {writer_sql}")
            )
        if table in WRITER_UPDATE_TABLES:
            await connection.execute(text(f"GRANT UPDATE ON TABLE {target} TO {writer_sql}"))
    approval_target = f"{schema_sql}.{quoted_identifier(SOURCE_USE_APPROVAL_TABLES[0])}"
    await connection.execute(
        text(f"GRANT UPDATE (revoked_at, revoked_by, revoked_reason) ON TABLE {approval_target} TO {writer_sql}")
    )


async def _validate_role_boundary(
    connection: AsyncConnection, *, schema: str, runtime: str, writer: str, publisher: str | None = None
) -> None:
    role_names = [runtime, writer, *([publisher] if publisher is not None else [])]
    roles = await connection.execute(
        text(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = ANY(:roles)"
        ),
        {"roles": role_names},
    )
    rows = roles.mappings().all()
    if len(rows) != len(role_names) or any(
        any(row[key] for key in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolbypassrls", "rolreplication"))
        for row in rows
    ):
        raise ValueError("Runtime, Writer and Publisher must exist without administrative privileges")
    memberships = await connection.scalar(
        text("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname = ANY(:roles)"),
        {"roles": role_names},
    )
    if memberships:
        raise ValueError("Runtime, Writer and Publisher must not inherit or SET ROLE to another role")
    owned = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND r.rolname = ANY(:roles)"
        ),
        {"schema": schema, "roles": role_names},
    )
    if owned:
        raise ValueError("Runtime, Writer and Publisher must not own schema objects")
    schema_owned = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=:schema AND r.rolname = ANY(:roles)"
        ),
        {"schema": schema, "roles": role_names},
    )
    database_owned = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database() AND r.rolname = ANY(:roles)"
        ),
        {"roles": role_names},
    )
    if schema_owned or database_owned:
        raise ValueError("Runtime, Writer and Publisher must not own database or schema")
    legacy_function = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=:schema AND p.proname='transition_rag_source_snapshot'"
        ),
        {"schema": schema},
    )
    if legacy_function:
        raise ValueError("Remove the legacy Source transition function before role cutover")


async def _revoke_column_grants(
    connection: AsyncConnection, *, schema: str, runtime: str, writer: str, publisher: str | None = None
) -> None:
    # Table REVOKE does not remove independently granted column privileges.
    # Writer loses column access across the schema; Source also closes PUBLIC/Runtime access.
    statements = await connection.scalars(
        text(
            "SELECT DISTINCT format('REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %s', "
            "a.attname, n.nspname, c.relname, "
            "CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(r.rolname) END) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(a.attacl) acl "
            "LEFT JOIN pg_roles r ON r.oid=acl.grantee "
            "WHERE n.nspname=:schema AND a.attnum>0 AND NOT a.attisdropped "
            "AND (r.rolname=:writer OR r.rolname=:publisher OR (acl.grantee=0 AND :has_publisher) OR "
            "(c.relname=ANY(:tables) AND (acl.grantee=0 OR r.rolname=:runtime)))"
        ),
        {
            "schema": schema,
            "runtime": runtime,
            "writer": writer,
            "publisher": publisher,
            "has_publisher": publisher is not None,
            "tables": list(SOURCE_TABLES + SOURCE_USE_APPROVAL_TABLES),
        },
    )
    for statement in statements:
        await connection.execute(text(statement))


async def _validate_lock_markers(connection: AsyncConnection, *, schema: str, publisher: bool = False) -> None:
    """Fail before ACL changes unless the existing lock-only columns are constrained to zero."""
    markers = (
        SOURCE_PUBLISHER_LOCK_COLUMNS if publisher else dict.fromkeys(WRITER_LOCK_TABLES, "knowledge_index_lock_marker")
    )
    for table, marker in markers.items():
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT a.attnotnull, pg_get_expr(k.conbin,k.conrelid) AS expression "
                        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "LEFT JOIN pg_constraint k ON k.conrelid=c.oid AND k.contype='c' AND k.convalidated "
                        "WHERE n.nspname=:schema AND c.relname=:table "
                        "AND a.attname=:marker AND NOT a.attisdropped "
                        "AND a.atttypid='integer'::regtype"
                    ),
                    {"schema": schema, "table": table, "marker": marker},
                )
            )
            .mappings()
            .all()
        )
        if not any(
            row["attnotnull"] and re.sub(r"[\s()]", "", row["expression"] or "") == f"{marker}=0" for row in rows
        ):
            raise ValueError(
                f"Source lock marker {table}.{marker} requires NOT NULL and a validated CHECK equal to zero"
            )


async def revoke_source_writer_publication(
    connection: AsyncConnection, *, schema: str, owner: str, runtime: str, writer: str
) -> None:
    """T1 denial barrier; caller commits independently before final provisioning.

    Never grants privileges, creates roles, or changes passwords. Failure must
    roll back this transaction; a later T2 failure cannot undo a committed T1.
    """
    schema_sql, owner_sql, writer_sql = (quoted_identifier(value) for value in (schema, owner, writer))
    quoted_identifier(runtime)
    if len({owner, runtime, writer}) != 3:
        raise ValueError("Migration, Runtime and Writer roles must differ")
    await _validate_role_boundary(connection, schema=schema, runtime=runtime, writer=writer)
    present = set(
        await connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema": schema})
    )
    if not set(WRITER_PUBLICATION_TABLES).issubset(present):
        raise ValueError("Apply Source migrations before Writer publication denial")
    for table in WRITER_PUBLICATION_TABLES:
        target = f"{schema_sql}.{quoted_identifier(table)}"
        await connection.execute(text(f"REVOKE INSERT ON TABLE {target} FROM PUBLIC, {writer_sql}"))
    statements = await connection.scalars(
        text(
            "SELECT DISTINCT format('REVOKE INSERT (%I) ON TABLE %I.%I FROM %s', "
            "a.attname, n.nspname, c.relname, "
            "CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(r.rolname) END) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(a.attacl) acl LEFT JOIN pg_roles r ON r.oid=acl.grantee "
            "WHERE n.nspname=:schema AND c.relname=ANY(:tables) AND a.attnum>0 AND NOT a.attisdropped "
            "AND acl.privilege_type='INSERT' AND (acl.grantee=0 OR r.rolname=:writer)"
        ),
        {"schema": schema, "tables": list(WRITER_PUBLICATION_TABLES), "writer": writer},
    )
    for statement in statements:
        await connection.execute(text(statement))
    for recipient in ("PUBLIC", writer_sql):
        for namespace in ("", f" IN SCHEMA {schema_sql}"):
            await connection.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_sql}{namespace} REVOKE INSERT ON TABLES FROM {recipient}"
                )
            )
    await _verify_writer_publication_denial(connection, schema=schema, owner=owner, writer=writer)


async def verify_source_role_policy(
    connection: AsyncConnection,
    *,
    schema: str,
    owner: str,
    runtime: str,
    writer: str,
    publisher: str | None = None,
    deny_only: bool = False,
) -> None:
    """Read effective ACLs after commit using a fresh admin session; never repair ACLs."""
    names = [owner, runtime, writer, *([publisher] if publisher is not None else [])]
    for name in [schema, *names]:
        quoted_identifier(name)
    if len(names) != len(set(names)):
        raise ValueError("Migration, Runtime, Writer and Publisher roles must differ")
    await _validate_role_boundary(
        connection, schema=schema, runtime=runtime, writer=writer, publisher=None if deny_only else publisher
    )
    await _verify_writer_publication_denial(connection, schema=schema, owner=owner, writer=writer)
    if not deny_only and publisher is not None:
        await _validate_lock_markers(connection, schema=schema, publisher=True)
        await _verify_publisher_effective_permissions(connection, schema=schema, owner=owner, publisher=publisher)


async def _verify_writer_publication_denial(
    connection: AsyncConnection, *, schema: str, owner: str, writer: str
) -> None:
    for table in WRITER_PUBLICATION_TABLES:
        target = f"{quoted_identifier(schema)}.{quoted_identifier(table)}"
        if await connection.scalar(
            text(
                "SELECT has_table_privilege(:role,:target,'INSERT') OR has_any_column_privilege(:role,:target,'INSERT')"
            ),
            {"role": writer, "target": target},
        ):
            raise ValueError(f"Writer publication INSERT remains effective on {table}")
    defaults = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=:owner) AND d.defaclobjtype='r' "
            "AND (d.defaclnamespace=0 OR d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname=:schema)) "
            "AND a.privilege_type='INSERT' AND (a.grantee=0 OR a.grantee=(SELECT oid FROM pg_roles WHERE rolname=:writer))"
        ),
        {"owner": owner, "schema": schema, "writer": writer},
    )
    if defaults:
        raise ValueError("Writer publication INSERT default grants remain effective")


async def _verify_publisher_effective_permissions(
    connection: AsyncConnection, *, schema: str, owner: str, publisher: str
) -> None:
    parameters = {
        "publisher": publisher,
        "schema": schema,
        "tables": list(SOURCE_TABLES),
        "markers": [f"{table}.{column}" for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items()],
    }
    present = set(
        await connection.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname=:schema AND tablename=ANY(:tables)"), parameters
        )
    )
    if present != set(SOURCE_TABLES):
        raise ValueError("Publisher Source tables are missing during ACL read-back")
    table_mismatch = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN']) p(privilege) "
            "WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
            "AND (has_table_privilege(:publisher,c.oid,p.privilege) <> "
            "(n.nspname=:schema AND c.relname=ANY(:tables) AND p.privilege IN ('SELECT','INSERT')) "
            "OR has_table_privilege(:publisher,c.oid,p.privilege || ' WITH GRANT OPTION'))"
        ),
        parameters,
    )
    if table_mismatch:
        raise ValueError("Publisher effective table privileges differ from the Source-only policy")
    column_mismatch = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) p(privilege) "
            "WHERE c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped "
            "AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
            "AND (has_column_privilege(:publisher,c.oid,a.attnum,p.privilege) <> "
            "(n.nspname=:schema AND c.relname=ANY(:tables) AND (p.privilege IN ('SELECT','INSERT') OR "
            "(p.privilege='UPDATE' AND (c.relname || '.' || a.attname)=ANY(:markers)))) "
            "OR has_column_privilege(:publisher,c.oid,a.attnum,p.privilege || ' WITH GRANT OPTION'))"
        ),
        parameters,
    )
    if column_mismatch:
        raise ValueError("Publisher effective column privileges differ from the constrained marker policy")
    await _verify_publisher_object_boundary(connection, schema=schema, owner=owner, publisher=publisher)


async def _verify_publisher_object_boundary(
    connection: AsyncConnection, *, schema: str, owner: str, publisher: str
) -> None:
    parameters = {"schema": schema, "owner": owner, "publisher": publisher}
    sequences = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind='S' AND n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
            "AND CASE WHEN c.relkind='S' THEN has_sequence_privilege(:publisher,c.oid,'USAGE,SELECT,UPDATE') ELSE false END"
        ),
        parameters,
    )
    unsafe_schemas = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
            "AND (has_schema_privilege(:publisher,n.oid,'CREATE') OR n.nspowner=(SELECT oid FROM pg_roles WHERE rolname=:publisher))"
        ),
        parameters,
    )
    owned = await connection.scalar(
        text("SELECT count(*) FROM pg_class c WHERE c.relowner=(SELECT oid FROM pg_roles WHERE rolname=:publisher)"),
        parameters,
    )
    database_create = await connection.scalar(
        text("SELECT has_database_privilege(:publisher,current_database(),'CREATE')"), parameters
    )
    if sequences or unsafe_schemas or owned or database_create:
        raise ValueError("Publisher must not have sequence access, CREATE privileges or object ownership")
    if not await connection.scalar(text("SELECT has_schema_privilege(:publisher,:schema,'USAGE')"), parameters):
        raise ValueError("Publisher requires Source schema USAGE")
    defaults = await connection.scalar(
        text(
            "SELECT count(*) FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
            "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=:owner) AND d.defaclobjtype IN ('r','S') "
            "AND (d.defaclnamespace=0 OR d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname=:schema)) "
            "AND (a.grantee=0 OR a.grantee=(SELECT oid FROM pg_roles WHERE rolname=:publisher))"
        ),
        parameters,
    )
    if defaults:
        raise ValueError("Publisher table or sequence default grants remain effective")

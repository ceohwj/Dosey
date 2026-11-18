"""Read-only, aggregate-only inventory of one explicitly local Candidate database.

Never treats an unavailable database as zero legacy keys. No migrations, cleanup,
response decryption, patient rows, key digests or connection strings are emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ("medication-candidate.confirm", "medication-candidate.reject")
BINDING_COLUMNS = {
    "runtime_release_bundle_manifest_hash",
    "runtime_environment_id",
    "runtime_environment_revision",
}


def local_database_url(values: Mapping[str, str | None]) -> URL | None:
    """Reject missing/nonlocal configuration before opening any connection."""
    if values.get("ENV", "local") not in {"local", "test"}:
        return None
    if values.get("DB_HOST") not in {"localhost", "127.0.0.1", "::1", "postgres"}:
        return None
    if not values.get("DB_USER") or not values.get("DB_NAME") or "DB_PASSWORD" not in values:
        return None
    host = values["DB_HOST"]
    port = int(values.get("DB_EXPOSE_PORT") or values.get("DB_PORT") or "5432")
    if not 1 <= port <= 65535:
        return None
    return URL.create(
        "postgresql+asyncpg",
        username=values["DB_USER"],
        password=values.get("DB_PASSWORD") or "",
        host="127.0.0.1" if host == "postgres" else host,
        port=port,
        database=values["DB_NAME"],
    )


async def audit(values: Mapping[str, str | None]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scope": "configured-local-database-only",
        "status": "UNAVAILABLE",
        "unexpired_candidate_keys": None,
        "latest_unexpired_expiry": None,
        "search_rows": None,
        "runtime_bound_search_rows": None,
        "legacy_retry_support_required": None,
    }
    try:
        url = local_database_url(values)
    except (TypeError, ValueError):
        result["reason"] = "INVALID_LOCAL_CONFIGURATION"
        return result
    if url is None:
        result["reason"] = "MISSING_OR_NONLOCAL_CONFIGURATION"
        return result

    engine = create_async_engine(
        url,
        echo=False,
        hide_parameters=True,
        connect_args={"timeout": 5},
    )
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            if await connection.scalar(text("SHOW transaction_read_only")) != "on":
                result["reason"] = "READ_ONLY_TRANSACTION_NOT_CONFIRMED"
                return result
            result["read_only"] = True
            for table in ("idempotency_record", "medication_candidate_search", "alembic_version"):
                exists = await connection.scalar(text("SELECT to_regclass(:table)"), {"table": f"public.{table}"})
                if exists is None:
                    result["reason"] = "REQUIRED_TABLE_UNAVAILABLE"
                    return result
            versions = await connection.scalars(text("SELECT version_num FROM public.alembic_version ORDER BY 1"))
            result["database_revisions"] = list(versions)
            counts = (
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) AS unexpired, max(expires_at) AS latest_expiry "
                            "FROM public.idempotency_record WHERE record_type = 'SYNC_MUTATION' "
                            "AND operation_id IN (:confirm, :reject) AND expires_at > CURRENT_TIMESTAMP"
                        ),
                        {"confirm": OPERATIONS[0], "reject": OPERATIONS[1]},
                    )
                )
                .mappings()
                .one()
            )
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'medication_candidate_search'"
                    )
                )
            )
            result["search_rows"] = await connection.scalar(
                text("SELECT count(*) FROM public.medication_candidate_search")
            )
            result["binding_columns_present"] = BINDING_COLUMNS <= columns
            result["runtime_bound_search_rows"] = (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM public.medication_candidate_search "
                        "WHERE runtime_release_bundle_id IS NOT NULL "
                        "AND runtime_release_bundle_manifest_hash IS NOT NULL "
                        "AND runtime_environment_id IS NOT NULL AND runtime_environment_revision IS NOT NULL"
                    )
                )
                if result["binding_columns_present"]
                else 0
            )
            # Publish counts only after the entire audit succeeds.
            result.update(
                status="OBSERVED",
                unexpired_candidate_keys=counts["unexpired"],
                latest_unexpired_expiry=counts["latest_expiry"].isoformat() if counts["latest_expiry"] else None,
                legacy_retry_support_required=bool(counts["unexpired"]),
            )
    except Exception:
        # Driver errors can contain credentials, hostnames or record fragments.
        result["reason"] = "DATABASE_AUDIT_FAILED"
    finally:
        await engine.dispose()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    source = args.env_file
    if source is None:
        source = next((p for p in (ROOT / ".env", ROOT / "envs/.local.env") if p.is_file()), None)
    values: dict[str, str | None] = {}
    if source is not None:
        if not source.is_file():
            print(json.dumps({"status": "UNAVAILABLE", "reason": "CONFIG_FILE_UNAVAILABLE"}))
            return 2
        values.update(dotenv_values(source, interpolate=False))
    values.update(os.environ)
    result = asyncio.run(audit(values))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "OBSERVED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

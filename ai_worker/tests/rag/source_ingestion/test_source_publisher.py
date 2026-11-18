"""Dedicated aggregate publisher configuration and effective grant boundary."""

from unittest.mock import AsyncMock, Mock

import pytest

from ai_worker.admin.source_publisher import SourcePublisherConfig, validate_source_publisher_session


def _environment():
    return {
        f"SOURCE_PUBLISHER_{key}": value
        for key, value in {
            "HOST": "localhost",
            "PORT": "5432",
            "NAME": "synthetic",
            "USER": "synthetic_publisher",
            "PASSWORD": "synthetic-publisher-only",
            "ACTOR": "synthetic-publisher",
        }.items()
    }


@pytest.mark.parametrize("key", ["HOST", "PORT", "NAME", "USER", "PASSWORD", "ACTOR"])
def test_requires_each_publisher_setting_without_writer_fallback(key):
    env = _environment()
    value = env.pop(f"SOURCE_PUBLISHER_{key}")
    env[f"SOURCE_WRITER_{key}"] = value
    with pytest.raises(ValueError):
        SourcePublisherConfig.from_environment(env)


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_rejects_other_role_secrets_without_disclosing_them(key):
    with pytest.raises(ValueError) as failure:
        SourcePublisherConfig.from_environment({**_environment(), key: "synthetic-co-injected"})
    assert "synthetic-co-injected" not in str(failure.value)


@pytest.mark.parametrize("changes", [{"ACTOR": " "}, {"ACTOR": "a" * 101}, {"PORT": "0"}, {"PORT": "65536"}])
def test_actor_and_port_are_bounded(changes):
    with pytest.raises(ValueError):
        SourcePublisherConfig.from_environment(
            {**_environment(), **{f"SOURCE_PUBLISHER_{k}": v for k, v in changes.items()}}
        )


def test_hides_url_and_has_no_writer_alias():
    config = SourcePublisherConfig.from_environment(_environment())
    assert config.url.username == "synthetic_publisher"
    assert config.actor == "synthetic-publisher"
    assert "synthetic-publisher-only" not in repr(config)
    assert not hasattr(config, "writer")


@pytest.mark.asyncio
async def test_rejects_unsafe_role_before_grant_queries():
    session = Mock(scalar=AsyncMock(return_value=True), execute=AsyncMock())
    with pytest.raises(ValueError):
        await validate_source_publisher_session(session)
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "violation", [None, "table", "maintain", "column", "missing_marker", "sequence", "switched_login"]
)
async def test_checks_effective_grants_not_role_name(violation):
    from infra.python.source_role_policy import SOURCE_PUBLISHER_LOCK_COLUMNS, SOURCE_TABLES

    tables = [(table, privilege) for table in SOURCE_TABLES for privilege in ("SELECT", "INSERT")]
    columns = [(table, column, "UPDATE") for table, column in SOURCE_PUBLISHER_LOCK_COLUMNS.items()]
    if violation == "table":
        tables.append(("rag_source_use_approval", "INSERT"))
    if violation == "maintain":
        tables.append(("rag_source_snapshot", "MAINTAIN"))
    if violation == "column":
        columns.append(("rag_source_snapshot", "verification_status", "UPDATE"))
    if violation == "missing_marker":
        columns.pop()
    session = Mock(
        scalar=AsyncMock(side_effect=[False, violation != "switched_login", violation == "sequence"]),
        execute=AsyncMock(side_effect=[Mock(all=Mock(return_value=tables)), Mock(all=Mock(return_value=columns))]),
    )
    if violation:
        with pytest.raises(ValueError):
            await validate_source_publisher_session(session)
    else:
        await validate_source_publisher_session(session)


@pytest.mark.parametrize("command", ["product", "label", "rematerialization"])
def test_ingestion_configs_expose_only_publisher_credentials(tmp_path, command):
    from ai_worker.admin.mfds_label_writer import MfdsLabelWriterConfig
    from ai_worker.admin.mfds_product_ingestion import MfdsProductIngestionConfig
    from ai_worker.admin.mfds_product_rematerialization import MfdsProductRematerializationConfig

    env = {
        **_environment(),
        "SOURCE_ARTIFACT_STORAGE_BACKEND": "LOCAL_PRIVATE",
        "SOURCE_ARTIFACT_READER_ROOT": str(tmp_path / "reader"),
        "SOURCE_ARTIFACT_FINALIZER_COMMAND": str(tmp_path / "finalizer"),
        "SOURCE_CLEANUP_JOURNAL_ROOT": str(tmp_path / "cleanup"),
        "SOURCE_ACQUISITION_SPOOL_ROOT": str(tmp_path / "spool"),
        "RAG_MFDS_API_KEY": "synthetic-provider",
        "MFDS_LABEL_SOURCE_CODE": "SYNTHETIC",
        "MFDS_LABEL_ENDPOINT_CODE": "LABEL",
        "MFDS_LABEL_OPERATION_CODE": "GET_LABEL",
        "MFDS_LABEL_ENDPOINT_RECEIPT_HASH": "a" * 64,
    }
    config_type = {
        "product": MfdsProductIngestionConfig,
        "label": MfdsLabelWriterConfig,
        "rematerialization": MfdsProductRematerializationConfig,
    }[command]
    if command == "rematerialization":
        env.pop("RAG_MFDS_API_KEY")
    config = config_type.from_environment(env)
    assert isinstance(config.publisher, SourcePublisherConfig)
    assert not hasattr(config, "writer")
    with pytest.raises(ValueError):
        config_type.from_environment({**env, "SOURCE_WRITER_PASSWORD": "synthetic-old-writer"})

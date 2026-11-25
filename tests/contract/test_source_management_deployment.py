"""The management credential and routes stay out of ordinary deployment surfaces."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_management_profiles_keep_credentials_out_of_runtime_services():
    services = yaml.safe_load((ROOT / "infra/docker/docker-compose.prod.yml").read_text())["services"]
    for name, service in services.items():
        environment = service.get("environment", {})
        if name not in {"source-management", "source-management-bootstrap", "provision-db-roles", "postgres"}:
            assert "SOURCE_MANAGEMENT_USER" not in environment
            assert "SOURCE_MANAGEMENT_PASSWORD" not in str(environment)
    manager = services["source-management"]
    assert manager["profiles"] == ["source-management"]
    assert manager["ports"] == ["127.0.0.1:8010:8010"]
    assert manager["environment"]["DB_PASSWORD"] == "${SOURCE_MANAGEMENT_PASSWORD:-}"
    assert "DB_ADMIN_PASSWORD" not in str(manager["environment"])
    assert "DB_APP_PASSWORD" not in str(manager["environment"])
    assert "DB_MIGRATION_PASSWORD" not in str(manager["environment"])
    assert "source_management" not in (ROOT / "apps/backend/app/main.py").read_text()
    for service in services.values():
        assert "source-management" not in service.get("depends_on", {})


async def test_management_rejects_publisher_secret_before_opening_database(monkeypatch):
    import pytest

    for key, value in {
        "DB_HOST": "127.0.0.1",
        "DB_USER": "synthetic-manager",
        "DB_PASSWORD": "synthetic-manager-secret",
        "DB_NAME": "synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    from app.admin import source_management_api as module

    monkeypatch.setenv("SOURCE_MANAGEMENT_USER", "synthetic-manager")
    monkeypatch.setenv("SOURCE_PUBLISHER_PASSWORD", "synthetic-publisher-secret")
    monkeypatch.setattr(module.config, "DB_USER", "synthetic-manager")
    with pytest.raises(RuntimeError, match="only its own database credential") as error:
        async with module.lifespan(module.management_app):
            raise AssertionError("management must reject publisher credential before startup")
    assert "synthetic-publisher-secret" not in str(error.value)


async def test_management_rejects_publisher_role_identity(monkeypatch):
    import pytest

    for key, value in {
        "DB_HOST": "127.0.0.1",
        "DB_USER": "synthetic-manager",
        "DB_PASSWORD": "synthetic-manager-secret",
        "DB_NAME": "synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    from app.admin import source_management_api as module

    monkeypatch.setattr(module.config, "DB_USER", "synthetic-manager")
    monkeypatch.setenv("SOURCE_MANAGEMENT_USER", "synthetic-manager")
    monkeypatch.setenv("SOURCE_PUBLISHER_USER", "synthetic-manager")
    with pytest.raises(RuntimeError, match="distinct database roles"):
        async with module.lifespan(module.management_app):
            raise AssertionError("management must reject publisher identity before startup")
    services = yaml.safe_load((ROOT / "infra/docker/docker-compose.prod.yml").read_text())["services"]
    for name in ("source-management", "source-management-bootstrap"):
        assert services[name]["environment"]["SOURCE_PUBLISHER_USER"] == "${SOURCE_PUBLISHER_USER}"
        assert "SOURCE_PUBLISHER_PASSWORD" not in str(services[name])
    bootstrap = (ROOT / "infra/docker/postgres/configure-management-role.sql").read_text()
    assert "\\getenv publisher_user SOURCE_PUBLISHER_USER" in bootstrap
    assert "count(DISTINCT name)=6" in bootstrap
    assert "(:'publisher_user')" in bootstrap

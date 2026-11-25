from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI

from app import main
from app.models.guides import Guide
from app.models.users import User
from app.services.guide_sync_runtime_execution import GuideSyncRuntimeAuthorityProviderFactory
from app.services.guide_sync_runtime_lifecycle import GuideSyncRuntimeAuthority


class _UnusedAuthority:
    def for_session(self, session):
        return self

    async def resolve(self, *, user: User, guide: Guide) -> GuideSyncRuntimeAuthority:
        raise AssertionError("bootstrap must not issue or resolve request authority")


@pytest.mark.parametrize(
    "authority",
    [None, object(), SimpleNamespace(resolve=None), SimpleNamespace(resolve=lambda **_kwargs: object())],
)
def test_invalid_authority_is_rejected_without_registering_partial_runtime(authority: object) -> None:
    app = FastAPI()

    with pytest.raises(RuntimeError, match="authority provider"):
        main.configure_guide_runtime(
            app,
            provider_dependencies=object(),
            authority_provider=cast(GuideSyncRuntimeAuthorityProviderFactory, authority),
        )

    assert not hasattr(app.state, "guide_runtime_provider_dependencies")
    assert not hasattr(app.state, "guide_sync_runtime_authority_provider")


def test_missing_dependencies_are_rejected_before_runtime_state_is_registered() -> None:
    app = FastAPI()

    with pytest.raises(RuntimeError, match="dependencies"):
        main.configure_guide_runtime(
            app,
            provider_dependencies=None,
            authority_provider=_UnusedAuthority(),
        )

    assert not hasattr(app.state, "guide_runtime_provider_dependencies")
    assert not hasattr(app.state, "guide_sync_runtime_authority_provider")


@pytest.mark.parametrize("callable_object", [False, True])
def test_async_bootstrap_factory_is_rejected_before_it_can_create_a_coroutine(
    monkeypatch: pytest.MonkeyPatch,
    callable_object: bool,
) -> None:
    async def factory() -> main.GuideRuntimeBootstrap:
        raise AssertionError("an async bootstrap factory must never be invoked")

    class AsyncFactory:
        async def __call__(self) -> main.GuideRuntimeBootstrap:
            return await factory()

    monkeypatch.setattr(
        main,
        "import_module",
        lambda _name: SimpleNamespace(build=AsyncFactory() if callable_object else factory),
    )

    with pytest.raises(RuntimeError, match="synchronous"):
        main._load_guide_runtime_bootstrap_factory("synthetic_bootstrap:build")


def test_invalid_executor_factory_is_rejected_without_publishing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    main.configure_guide_runtime(
        app,
        provider_dependencies=object(),
        authority_provider=_UnusedAuthority(),
    )
    app.state.openai_client = object()
    monkeypatch.setattr(main, "build_production_guide_runtime_executor_factory", lambda *_args, **_kwargs: object())

    with pytest.raises(RuntimeError, match="executor factory"):
        main.initialize_guide_runtime_executor_factory(app)

    assert not hasattr(app.state, "guide_runtime_executor_factory")


def test_async_executor_creation_is_rejected_before_the_first_guide_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AsyncFactory:
        async def create(self) -> object:
            raise AssertionError("executor creation must be synchronous")

    app = FastAPI()
    main.configure_guide_runtime(
        app,
        provider_dependencies=object(),
        authority_provider=_UnusedAuthority(),
    )
    app.state.openai_client = object()
    monkeypatch.setattr(
        main, "build_production_guide_runtime_executor_factory", lambda *_args, **_kwargs: AsyncFactory()
    )

    with pytest.raises(RuntimeError, match="executor factory"):
        main.initialize_guide_runtime_executor_factory(app)

    assert not hasattr(app.state, "guide_runtime_executor_factory")


async def test_runtime_initialization_failure_still_closes_client_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    main.configure_guide_runtime(
        app,
        provider_dependencies=object(),
        authority_provider=_UnusedAuthority(),
    )
    closed: list[str] = []

    class Client:
        async def close(self) -> None:
            closed.append("client")

    async def close_database() -> None:
        closed.append("database")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic runtime initialization failure")

    monkeypatch.setattr(main.config, "GUIDE_RUNTIME_ENABLED", False)
    monkeypatch.setattr(main.config, "CHAT_CLOSED_DEMO_RAG_ENABLED", False)
    monkeypatch.setattr(main.config, "GUIDE_CLOSED_DEMO_RAG_ENABLED", False)
    monkeypatch.setattr(main, "get_email_sender", lambda: object())
    monkeypatch.setattr(main, "AsyncOpenAI", lambda **_kwargs: Client())
    monkeypatch.setattr(main, "build_production_guide_runtime_executor_factory", unavailable)
    monkeypatch.setattr(main, "close_database", close_database)

    with pytest.raises(RuntimeError, match="synthetic runtime initialization failure"):
        async with main.lifespan(app):
            pytest.fail("startup must not publish an unavailable runtime")

    assert closed == ["client", "database"]


@pytest.mark.parametrize("failed_close", ["guide", "chat", "client"])
async def test_close_failure_does_not_skip_remaining_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
    failed_close: str,
) -> None:
    app = FastAPI()
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.name == failed_close:
                raise RuntimeError(f"{self.name} close failed")

        async def close(self) -> None:
            await self.aclose()

    async def close_database() -> None:
        closed.append("database")

    monkeypatch.setattr(main.config, "GUIDE_RUNTIME_ENABLED", False)
    monkeypatch.setattr(main.config, "CHAT_CLOSED_DEMO_RAG_ENABLED", True)
    monkeypatch.setattr(main.config, "GUIDE_CLOSED_DEMO_RAG_ENABLED", True)
    monkeypatch.setattr(main, "get_email_sender", lambda: object())
    monkeypatch.setattr(main, "AsyncOpenAI", lambda **_kwargs: Resource("client"))
    monkeypatch.setattr(main, "build_configured_closed_demo_retrieval_service", lambda _client: Resource("chat"))
    monkeypatch.setattr(main, "build_configured_guide_closed_demo_retrieval_service", lambda _client: Resource("guide"))
    monkeypatch.setattr(main, "close_database", close_database)

    with pytest.raises(RuntimeError, match=f"{failed_close} close failed"):
        async with main.lifespan(app):
            pass

    assert closed == ["guide", "chat", "client", "database"]

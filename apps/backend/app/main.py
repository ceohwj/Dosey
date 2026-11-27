import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from inspect import iscoroutinefunction
from typing import Protocol, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from openai import AsyncOpenAI

from ai_worker.tasks.rag.guide_runtime_provider import build_production_guide_runtime_executor_factory
from app.apis.v1 import v1_routers
from app.core import config
from app.core.db.databases import close_database
from app.core.errors import ApiError, ErrorDetail, register_exception_handlers
from app.core.no_store_middleware import NoStoreMiddleware
from app.core.validation_trace_middleware import RequestTraceMiddleware, ValidationTraceMiddleware
from app.dependencies.services import (
    build_configured_chat_citation_retriever,
    build_configured_chat_citation_validator,
    build_configured_closed_demo_retrieval_service,
    build_configured_guide_closed_demo_retrieval_service,
    get_email_sender,
)
from app.services.guide_sync_runtime_execution import GuideSyncRuntimeAuthorityProviderFactory


@dataclass(frozen=True, slots=True)
class GuideRuntimeBootstrap:
    provider_dependencies: object
    authority_provider: GuideSyncRuntimeAuthorityProviderFactory


class GuideRuntimeBootstrapFactory(Protocol):
    def __call__(self) -> GuideRuntimeBootstrap: ...


def _is_async_callable(value: object) -> bool:
    return iscoroutinefunction(value) or (callable(value) and iscoroutinefunction(type(value).__call__))


def _load_guide_runtime_bootstrap_factory(path: str) -> GuideRuntimeBootstrapFactory:
    try:
        module_name, symbol_name = path.split(":", maxsplit=1)
        factory = getattr(import_module(module_name), symbol_name)
    except (AttributeError, ImportError, ValueError) as error:
        raise RuntimeError("Guide runtime bootstrap factory cannot be loaded") from error
    if not callable(factory):
        raise RuntimeError("Guide runtime bootstrap factory is not callable")
    if _is_async_callable(factory):
        raise RuntimeError("Guide runtime bootstrap factory must be synchronous")
    return cast(GuideRuntimeBootstrapFactory, factory)


def configure_guide_runtime(
    app: FastAPI,
    *,
    provider_dependencies: object,
    authority_provider: GuideSyncRuntimeAuthorityProviderFactory,
) -> None:
    """Atomically register the external composition required by Guide Sync runtime."""

    state_keys = (
        "guide_runtime_provider_dependencies",
        "guide_sync_runtime_authority_provider",
        "guide_runtime_executor_factory",
    )
    if any(hasattr(app.state, key) for key in state_keys):
        raise RuntimeError("Guide runtime bootstrap is already configured")
    if provider_dependencies is None:
        raise RuntimeError("Guide runtime provider dependencies are missing")
    resolver = getattr(authority_provider, "for_session", None)
    if not callable(resolver) or _is_async_callable(resolver):
        raise RuntimeError("Guide runtime authority provider is not configured")
    app.state.guide_runtime_provider_dependencies = provider_dependencies
    app.state.guide_sync_runtime_authority_provider = authority_provider


def configure_guide_runtime_from_settings(app: FastAPI) -> None:
    """Run the configured production composition root before lifespan initialization."""

    if not config.GUIDE_RUNTIME_ENABLED:
        return
    factory = _load_guide_runtime_bootstrap_factory(config.GUIDE_RUNTIME_BOOTSTRAP_FACTORY)
    bootstrap = factory()
    if type(bootstrap) is not GuideRuntimeBootstrap:
        raise RuntimeError("Guide runtime bootstrap factory returned an invalid contract")
    configure_guide_runtime(
        app,
        provider_dependencies=bootstrap.provider_dependencies,
        authority_provider=bootstrap.authority_provider,
    )


def initialize_guide_runtime_executor_factory(app: FastAPI) -> None:
    """Install the optional local/internal Guide runtime factory exactly once.

    The opaque Worker-owned dependency bundle is supplied by local/internal
    composition before lifespan starts. Public Backend services never inspect it.
    """

    has_dependencies = hasattr(app.state, "guide_runtime_provider_dependencies")
    has_authority = hasattr(app.state, "guide_sync_runtime_authority_provider")
    if not has_dependencies and not has_authority:
        return
    if not has_dependencies or not has_authority:
        raise RuntimeError("Guide runtime bootstrap is incomplete")
    if hasattr(app.state, "guide_runtime_executor_factory"):
        raise RuntimeError("Guide runtime executor factory is already initialized")
    factory = build_production_guide_runtime_executor_factory(
        app.state.guide_runtime_provider_dependencies,
        openai_client=app.state.openai_client,
        openai_model=config.OPENAI_MODEL,
        openai_timeout_seconds=config.OPENAI_TIMEOUT_SECONDS,
    )
    create = getattr(factory, "create", None)
    if not callable(create) or _is_async_callable(create):
        raise RuntimeError("Guide runtime executor factory is invalid")
    app.state.guide_runtime_executor_factory = factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # FastAPI가 실제 이메일 발송 경계를 소비하므로, non-local noop이나 잘못된 SMTP 설정은
    # 첫 이메일 요청까지 미루지 않고 앱 시작 시점에 드러냅니다.
    get_email_sender()

    configure_guide_runtime_from_settings(app)

    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(close_database)
        # guide_ai·chat_ai 연동에서 공용으로 사용할 AsyncOpenAI 클라이언트를 조립합니다.
        # 재시도는 asyncio.timeout으로 감싼 우리 쪽 타임아웃/에러 매핑이 전담하도록 SDK 자동 재시도를 끕니다.
        app.state.openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY, max_retries=0)
        cleanup.push_async_callback(app.state.openai_client.close)
        initialize_guide_runtime_executor_factory(app)
        if config.CHAT_CLOSED_DEMO_RAG_ENABLED:
            closed_demo_retrieval_service = build_configured_closed_demo_retrieval_service(app.state.openai_client)
            cleanup.push_async_callback(closed_demo_retrieval_service.aclose)
            app.state.closed_demo_retrieval_service = closed_demo_retrieval_service
        if config.CHAT_CITATION_RAG_ENABLED:
            chat_citation_retriever = build_configured_chat_citation_retriever(app.state.openai_client)
            chat_citation_validator = build_configured_chat_citation_validator()
            if config.CHAT_CITATION_RAG_WARMUP_NLI:
                await asyncio.to_thread(chat_citation_validator.warmup)
            app.state.chat_citation_retriever = chat_citation_retriever
            app.state.chat_citation_validator = chat_citation_validator
        if config.GUIDE_CLOSED_DEMO_RAG_ENABLED:
            guide_closed_demo_retrieval_service = build_configured_guide_closed_demo_retrieval_service(
                app.state.openai_client
            )
            cleanup.push_async_callback(guide_closed_demo_retrieval_service.aclose)
            app.state.guide_closed_demo_retrieval_service = guide_closed_demo_retrieval_service
        yield


fastapi_app = FastAPI(
    default_response_class=ORJSONResponse,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


register_exception_handlers(fastapi_app)

fastapi_app.include_router(v1_routers)


@fastapi_app.get(
    "/api/v1/_internal/upload-too-large",
    include_in_schema=False,
)
async def proxy_upload_too_large() -> None:
    # Nginx가 차단한 파일 본문을 읽거나 저장하지 않고 오류만 반환합니다.
    raise ApiError(
        status_code=400,
        code="UPLOAD_FILE_TOO_LARGE",
        message="파일 크기는 30MB 이하만 업로드할 수 있습니다.",
        details=[
            ErrorDetail(
                field="file",
                reason="TOO_LARGE",
            )
        ],
    )


# validation 거부 응답도 CORS와 no-store 경계를 통과하도록 FastAPI 바로 바깥에 둡니다.
validated_app = ValidationTraceMiddleware(
    fastapi_app,
    environment=config.ENV,
    validation_enabled=config.RELEASE_VALIDATION_ALLOWED,
)

# FastAPI의 바깥쪽 예외 처리 계층에서 반환되는 500 응답에도 CORS 헤더를 붙입니다.
cors_app = CORSMiddleware(
    app=NoStoreMiddleware(validated_app),
    allow_origins=config.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Retry-After: job_routers.build_job_status_response()가 RETRY_WAIT에서 설정하는 값을
    # cross-origin Frontend가 fetch()로 읽으려면 CORS expose 대상에 있어야 합니다 — 없으면
    # 브라우저가 응답 자체는 받아도 스크립트에서 헤더값을 읽지 못합니다.
    expose_headers=["X-Trace-Id", "Retry-After"],
)

# trace 경계는 CORS preflight를 포함한 모든 HTTP 응답을 감싸도록 가장 바깥에 둡니다.
app = RequestTraceMiddleware(cors_app)

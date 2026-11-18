"""Synthetic Candidate retry checks against the real shared idempotency service."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core import config
from app.core.errors import ApiError
from app.core.utils.idempotency import compute_key_hmac, compute_request_hash
from app.dtos.medication_candidates import ConfirmMedicationCandidateRequest, RejectMedicationCandidateRequest
from app.services.idempotency import (
    RUNTIME_RELEASE_BUNDLE_PLACEHOLDER,
    IdempotencyKeyConflictError,
    SyncMutationIdempotencyService,
)
from app.services.medication_candidates import MedicationCandidateService


class FakeSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


class FakeCipher:
    def encrypt(self, plaintext):
        return plaintext[::-1], "test-v1"

    def decrypt(self, ciphertext, *, key_version):
        assert key_version == "test-v1"
        return ciphertext[::-1]


class FakeIdempotencyRepository:
    def __init__(self):
        self.session = FakeSession()
        self.record = None
        self.writes = 0
        self.deleted = 0

    async def find_sync_idempotency_record(self, **kwargs):
        record = self.record
        if record is None:
            return None
        if (record.user_id, record.operation_id, record.parent_resource_id) != (
            kwargs["user_id"],
            kwargs["operation_id"],
            kwargs["parent_resource_id"],
        ):
            return None
        if not any(
            digest.key_hmac_version == record.key_hmac_version and digest.key_hmac == record.key_hmac
            for digest in kwargs["key_hmac_candidates"]
        ):
            return None
        return record

    async def delete_expired_idempotency_record(self, *, record_id):
        assert self.record.id == record_id
        self.record = None
        self.deleted += 1

    async def create_sync_idempotency_record(self, **kwargs):
        assert self.record is None
        self.writes += 1
        self.record = SimpleNamespace(
            **kwargs,
            id=uuid4(),
            expires_at=datetime.now(config.TIMEZONE) + timedelta(days=config.IDEMPOTENCY_RECORD_TTL_DAYS),
        )
        return self.record


class FakeCandidateRepository:
    def __init__(self, selection, owner_id):
        self.selection = selection
        self.owner_id = owner_id

    async def get_result_selection_owned(self, *, candidate_search_result_id, user_id):
        if user_id != self.owner_id or candidate_search_result_id != self.selection.result.id:
            return None
        return self.selection


class FakeIdentificationService:
    def __init__(self, parent_id):
        self.parent_id = parent_id
        self.validations = 0
        self.mutations = 0
        self.stale = False

    async def validate_candidate_runtime(self, **kwargs):
        self.validations += 1
        if self.stale:
            raise ApiError(status_code=409, code="IDENTIFICATION_CONTEXT_STALE", message="stale")

    async def confirm_identification(self, **kwargs):
        self.mutations += 1
        return SimpleNamespace(
            id=uuid4(),
            prescription_version_medication_id=self.parent_id,
            status="MATCHED",
            source="USER_SELECTED",
            product_id=uuid4(),
            confirmed_at=datetime.now(config.TIMEZONE),
        )

    async def reject_identification(self, **kwargs):
        self.mutations += 1
        return SimpleNamespace(
            id=uuid4(),
            prescription_version_medication_id=self.parent_id,
            status="UNRESOLVED",
            rejected_at=datetime.now(config.TIMEZONE),
        )


def fixture(*, bound=True):
    user = SimpleNamespace(id=uuid4())
    parent_id = uuid4()
    search = SimpleNamespace(
        id=uuid4(),
        prescription_version_medication_id=parent_id,
        runtime_release_bundle_id=uuid4() if bound else None,
        runtime_release_bundle_manifest_hash="a" * 64 if bound else None,
        runtime_environment_id=uuid4() if bound else None,
        runtime_environment_revision=3 if bound else None,
    )
    selection = SimpleNamespace(search=search, result=SimpleNamespace(id=uuid4()))
    idem_repo = FakeIdempotencyRepository()
    identification = FakeIdentificationService(parent_id)
    service = MedicationCandidateService(
        FakeCandidateRepository(selection, user.id),
        identification,
        SyncMutationIdempotencyService(idem_repo, FakeCipher()),
    )
    return user, selection, idem_repo, identification, service


def request_for(action, selection):
    if action == "confirm":
        return ConfirmMedicationCandidateRequest(
            prescription_version_medication_id=selection.search.prescription_version_medication_id,
            candidate_search_result_id=selection.result.id,
        )
    return RejectMedicationCandidateRequest(
        search_id=selection.search.id, candidate_search_result_id=selection.result.id
    )


async def call(service, action, user, request, key="candidate-runtime-key-0001"):
    method = service.confirm_candidate if action == "confirm" else service.reject_candidate
    return await method(user=user, request=request, idempotency_key=key)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_new_hash_uses_immutable_binding_and_replays_after_state_change(action):
    user, selection, repo, identification, service = fixture()
    request = request_for(action, selection)
    first = await call(service, action, user, request)
    expected = service._candidate_fingerprint(action=action, selection=selection)
    assert repo.record.request_hash == compute_request_hash(expected)
    assert expected["schema_version"] == 2
    assert expected["runtime_binding"]["runtime_environment_revision"] == 3
    assert repo.writes == identification.validations == identification.mutations == 1
    identification.stale = True
    assert await call(service, action, user, request) == first
    assert repo.writes == identification.validations == identification.mutations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_existing_placeholder_hash_replays_without_runtime_validation(action):
    user, selection, repo, identification, service = fixture(bound=False)
    request = request_for(action, selection)
    key = "candidate-runtime-key-0001"
    legacy = {
        "action": action,
        "candidate_search_result_id": str(selection.result.id),
        "runtime_release_bundle": RUNTIME_RELEASE_BUNDLE_PLACEHOLDER,
    }
    if action == "reject":
        legacy["search_id"] = str(selection.search.id)
    body = {
        "prescription_version_medication_id": str(selection.search.prescription_version_medication_id),
        "status": "MATCHED" if action == "confirm" else "UNRESOLVED",
    }
    if action == "confirm":
        body.update(
            identification_id=str(uuid4()),
            source="USER_SELECTED",
            product_id=str(uuid4()),
            confirmed_at=datetime.now(config.TIMEZONE).isoformat(),
        )
    else:
        body.update(
            identification_event_id=str(uuid4()),
            search_status="INVALIDATED_USER_REJECTED",
            rejected_at=datetime.now(config.TIMEZONE).isoformat(),
        )
    repo.record = SimpleNamespace(
        id=uuid4(),
        user_id=user.id,
        operation_id=f"medication-candidate.{action}",
        parent_resource_id=selection.search.prescription_version_medication_id,
        key_hmac_version=config.IDEMPOTENCY_HMAC_KEY_VERSION,
        key_hmac=compute_key_hmac(key, hmac_key=config.IDEMPOTENCY_HMAC_KEY),
        request_hash=compute_request_hash(legacy),
        response_status=200,
        response_body_snapshot=FakeCipher().encrypt(json.dumps(body).encode())[0],
        encryption_key_version="test-v1",
        expires_at=datetime.now(config.TIMEZONE) + timedelta(days=1),
    )
    await call(service, action, user, request, key)
    assert repo.record.request_hash == compute_request_hash(legacy)
    assert repo.writes == identification.validations == identification.mutations == 0


@pytest.mark.asyncio
async def test_missing_binding_new_key_fails_without_write():
    user, selection, repo, identification, service = fixture(bound=False)
    identification.stale = True
    with pytest.raises(ApiError) as error:
        await call(service, "confirm", user, request_for("confirm", selection))
    assert error.value.code == "IDENTIFICATION_CONTEXT_STALE"
    assert repo.writes == identification.mutations == 0


@pytest.mark.asyncio
async def test_changed_request_and_cross_user_do_not_replay():
    user, selection, repo, identification, service = fixture()
    request = request_for("confirm", selection)
    await call(service, "confirm", user, request)
    selection.result.id = uuid4()
    with pytest.raises(IdempotencyKeyConflictError):
        await call(service, "confirm", user, request_for("confirm", selection))
    with pytest.raises(ApiError) as error:
        await call(service, "confirm", SimpleNamespace(id=uuid4()), request)
    assert error.value.status_code == 404
    assert repo.writes == identification.mutations == 1


@pytest.mark.asyncio
async def test_wrong_parent_or_search_returns_not_found_before_replay():
    user, selection, repo, identification, service = fixture()
    await call(service, "confirm", user, request_for("confirm", selection))
    wrong_parent = ConfirmMedicationCandidateRequest(
        prescription_version_medication_id=uuid4(), candidate_search_result_id=selection.result.id
    )
    wrong_search = RejectMedicationCandidateRequest(search_id=uuid4(), candidate_search_result_id=selection.result.id)
    for action, request in (("confirm", wrong_parent), ("reject", wrong_search)):
        with pytest.raises(ApiError) as error:
            await call(service, action, user, request)
        assert error.value.status_code == 404
    assert repo.writes == identification.mutations == 1


@pytest.mark.asyncio
async def test_expired_record_is_not_replayed_when_new_validation_fails():
    user, selection, repo, identification, service = fixture(bound=False)
    request = request_for("confirm", selection)
    await call(service, "confirm", user, request)
    repo.record.expires_at = datetime.now(config.TIMEZONE) - timedelta(seconds=1)
    identification.stale = True
    with pytest.raises(ApiError):
        await call(service, "confirm", user, request)
    assert repo.deleted == 1
    assert repo.record is None
    assert repo.writes == 1


@pytest.mark.asyncio
async def test_domain_error_requery_accepts_legacy_winner_but_not_expired_winner():
    user, selection, repo, _, _ = fixture()
    key = "candidate-runtime-domain-race-0001"
    legacy = {
        "action": "confirm",
        "candidate_search_result_id": str(selection.result.id),
        "runtime_release_bundle": RUNTIME_RELEASE_BUNDLE_PLACEHOLDER,
    }
    fingerprint = {"schema_version": 2, "action": "confirm"}
    body = {"ok": True}

    async def mutate():
        repo.record = SimpleNamespace(
            id=uuid4(),
            user_id=user.id,
            operation_id="medication-candidate.confirm",
            parent_resource_id=selection.search.prescription_version_medication_id,
            key_hmac_version=config.IDEMPOTENCY_HMAC_KEY_VERSION,
            key_hmac=compute_key_hmac(key, hmac_key=config.IDEMPOTENCY_HMAC_KEY),
            request_hash=compute_request_hash(legacy),
            response_status=200,
            response_body_snapshot=FakeCipher().encrypt(json.dumps(body).encode())[0],
            encryption_key_version="test-v1",
            expires_at=datetime.now(config.TIMEZONE) + timedelta(days=1),
        )
        raise ApiError(status_code=409, code="CANDIDATE_SEARCH_STALE", message="stale")

    service = SyncMutationIdempotencyService(repo, FakeCipher())
    result = await service.execute(
        user_id=user.id,
        operation_id="medication-candidate.confirm",
        parent_resource_id=selection.search.prescription_version_medication_id,
        idempotency_key=key,
        fingerprint=fingerprint,
        legacy_fingerprint=legacy,
        success_status=200,
        mutate=mutate,
    )
    assert result.is_replay and result.response_body == body
    repo.record.expires_at = datetime.now(config.TIMEZONE) - timedelta(seconds=1)
    repo.record = None

    # A domain exception with no usable winner remains the original domain exception.
    async def stale_mutate():
        try:
            await mutate()
        finally:
            repo.record.expires_at = datetime.now(config.TIMEZONE) - timedelta(seconds=1)

    with pytest.raises(ApiError) as error:
        await service.execute(
            user_id=user.id,
            operation_id="medication-candidate.confirm",
            parent_resource_id=selection.search.prescription_version_medication_id,
            idempotency_key=key,
            fingerprint=fingerprint,
            legacy_fingerprint=legacy,
            success_status=200,
            mutate=stale_mutate,
        )
    assert error.value.code == "CANDIDATE_SEARCH_STALE"


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_unique_conflict_requery_accepts_only_unexpired_v2_winner(expired):
    user, selection, repo, _, _ = fixture()
    key = "candidate-runtime-unique-race-0001"
    fingerprint = {"schema_version": 2, "action": "reject"}
    original_create = repo.create_sync_idempotency_record

    class UniqueViolationError(Exception):
        sqlstate = "23505"
        constraint_name = "uq_idempotency_sync_scope"

    async def competing_create(**kwargs):
        await original_create(**kwargs)
        if expired:
            repo.record.expires_at = datetime.now(config.TIMEZONE) - timedelta(seconds=1)
        raise IntegrityError("INSERT", {}, UniqueViolationError()) from UniqueViolationError()

    repo.create_sync_idempotency_record = competing_create

    async def execute():
        return await SyncMutationIdempotencyService(repo, FakeCipher()).execute(
            user_id=user.id,
            operation_id="medication-candidate.reject",
            parent_resource_id=selection.search.prescription_version_medication_id,
            idempotency_key=key,
            fingerprint=fingerprint,
            success_status=200,
            mutate=_response,
        )

    if expired:
        with pytest.raises(IntegrityError):
            await execute()
    else:
        result = await execute()
        assert result.is_replay and result.response_body == {"ok": True}
    assert repo.writes == 1


async def _response():
    return {"ok": True}


@pytest.mark.asyncio
async def test_default_sync_mutation_does_not_accept_legacy_hash():
    user, selection, repo, _, _ = fixture()
    key = "default-sync-mutation-0001"
    repo.record = SimpleNamespace(
        id=uuid4(),
        user_id=user.id,
        operation_id="medication-checkin.create",
        parent_resource_id=selection.search.prescription_version_medication_id,
        key_hmac_version=config.IDEMPOTENCY_HMAC_KEY_VERSION,
        key_hmac=compute_key_hmac(key, hmac_key=config.IDEMPOTENCY_HMAC_KEY),
        request_hash=compute_request_hash({"old": True}),
        response_status=200,
        response_body_snapshot=FakeCipher().encrypt(b'{"ok":true}')[0],
        encryption_key_version="test-v1",
        expires_at=datetime.now(config.TIMEZONE) + timedelta(days=1),
    )
    with pytest.raises(IdempotencyKeyConflictError):
        await SyncMutationIdempotencyService(repo, FakeCipher()).execute(
            user_id=user.id,
            operation_id="medication-checkin.create",
            parent_resource_id=selection.search.prescription_version_medication_id,
            idempotency_key=key,
            fingerprint={"new": True},
            success_status=200,
            mutate=_response,
        )


@pytest.mark.asyncio
async def test_candidate_replay_uses_retained_hmac_key(monkeypatch):
    user, selection, repo, _, _ = fixture()
    service = SyncMutationIdempotencyService(repo, FakeCipher())
    args = {
        "user_id": user.id,
        "operation_id": "medication-candidate.confirm",
        "parent_resource_id": selection.search.prescription_version_medication_id,
        "idempotency_key": "candidate-retained-key-0001",
        "fingerprint": {"schema_version": 2, "action": "confirm"},
        "success_status": 200,
        "mutate": _response,
    }
    old_key = "synthetic-old-idempotency-hmac-key-0001"
    new_key = "synthetic-new-idempotency-hmac-key-0002"
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_KEY", old_key)
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_KEY_VERSION", "old")
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_RETIRED_KEYS", {})
    assert not (await service.execute(**args)).is_replay
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_KEY", new_key)
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_KEY_VERSION", "new")
    monkeypatch.setattr(config, "IDEMPOTENCY_HMAC_RETIRED_KEYS", {"old": old_key})
    assert (await service.execute(**args)).is_replay
    assert repo.writes == 1

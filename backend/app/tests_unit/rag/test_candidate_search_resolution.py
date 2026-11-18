from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.models.rag_candidate import MedicationCandidateSearchStatus
from app.models.rag_candidate_index import RagCandidateIndexBuildMode
from app.models.rag_catalog import RagMedicationIngredient, RagMedicationProduct
from app.services.candidate_search_resolution import (
    _ConservativeAttributeMatcher,
    _LexicalRelevanceEvaluator,
    _product_source_for_candidate,
    resolve_synthetic_candidate_search,
)
from app.services.rag.candidate_policy import CandidateStage, ResolverPolicy
from app.services.rag.candidate_resolver import (
    AttributeCompatibility,
    CandidateEvidence,
    CandidateHit,
    CandidateIndexMode,
    CandidateProvenanceReceipt,
    CandidateSignal,
    CandidateSourceRef,
    HydratedCandidateEvidence,
    OfficialEntityType,
    OfficialIdentity,
    ProductSnapshot,
    ProductStatus,
    ResolverInput,
)


def _policy(**changes: object) -> ResolverPolicy:
    values: dict[str, object] = {
        "policy_version": "synthetic-only-v1",
        "maximum_input_length": 100,
        "retrieval_limit": 10,
        "enable_dense": False,
        "release_eligible": False,
        "stage_weights": (
            (CandidateStage.PRODUCT_NAME_EXACT, 4.0),
            (CandidateStage.APPROVED_ALIAS_EXACT, 3.0),
            (CandidateStage.TRIGRAM_EDIT_DISTANCE, 2.0),
            (CandidateStage.DENSE_VECTOR, 1.0),
        ),
        "rrf_k": 10.0,
        "minimum_relevance": 0.7,
        "minimum_margin": 0.02,
        "auto_select_stages": frozenset({CandidateStage.PRODUCT_NAME_EXACT}),
    }
    values.update(changes)
    return ResolverPolicy(**values)  # type: ignore[arg-type]


def _snapshot(*, count: int = 1, mode: RagCandidateIndexBuildMode = RagCandidateIndexBuildMode.LEXICAL_ONLY):
    source_id = uuid4()
    members = tuple(
        SimpleNamespace(
            identity_entity_type=OfficialEntityType.PRODUCT,
            identity_code_system="MFDS_ITEM_SEQ",
            identity_canonical_code=f"SYNTH-{number}",
            product_source_snapshot_id=source_id,
        )
        for number in range(count)
    )
    return SimpleNamespace(
        version=SimpleNamespace(
            id=uuid4(),
            index_code="SYNTHETIC_INDEX",
            index_version="index-v1",
            catalog_version="catalog-v1",
            catalog_manifest_hash="a" * 64,
            normalization_version="norm-v1",
            candidate_limit=10,
            build_mode=mode,
        ),
        members=members,
        source_refs=(SimpleNamespace(snapshot_id=str(source_id), source_version="source-v1"),),
    )


def _hit(snapshot: object, number: int, *, strength: str | None = "10mg", score: float = 0.88) -> CandidateHit:
    version = snapshot.version  # type: ignore[attr-defined]
    source_id = snapshot.source_refs[0].snapshot_id  # type: ignore[attr-defined]
    identity = OfficialIdentity(OfficialEntityType.PRODUCT, "MFDS_ITEM_SEQ", f"SYNTH-{number}")
    product = ProductSnapshot(
        identity=identity,
        product_name="합성제품",
        strength_text=strength,
        dosage_form="정제",
        manufacturer_name="합성제약",
        status=ProductStatus.ACTIVE,
    )
    return CandidateHit(
        identity=identity,
        product=product,
        stage=CandidateStage.PRODUCT_NAME_EXACT,
        rank=number + 1,
        stage_score=score,
        index_version=version.index_version,
        member_key="b" * 64,
        catalog_version=version.catalog_version,
        source_snapshot_id=source_id,
        normalization_version=version.normalization_version,
        embedding_model_version=None,
    )


def _evidence(snapshot: object, hits: tuple[CandidateHit, ...]) -> HydratedCandidateEvidence:
    version = snapshot.version  # type: ignore[attr-defined]
    source = snapshot.source_refs[0]  # type: ignore[attr-defined]
    return HydratedCandidateEvidence(
        provenance=CandidateProvenanceReceipt(
            index_version=version.index_version,
            catalog_version=version.catalog_version,
            catalog_manifest_hash=version.catalog_manifest_hash,
            source_refs=(CandidateSourceRef(source.snapshot_id, source.source_version),),
            normalization_version=version.normalization_version,
            embedding_model_version=None,
            index_mode=CandidateIndexMode.LEXICAL_ONLY,
        ),
        product_hits=hits,
        ingredient_hits=(),
    )


class _Session:
    def __init__(self, *, products: tuple[object, ...] = (), ingredients: tuple[object, ...] = ()) -> None:
        self.products = products
        self.ingredients = ingredients
        self.queries: list[object] = []
        self.product_query_count = 0

    async def scalar(self, statement: object):
        self.queries.append(statement)
        return SimpleNamespace(id=self.ingredients[0].entity_identity_id) if self.ingredients else None

    async def scalars(self, statement: object):
        self.queries.append(statement)
        entity = statement.column_descriptions[0]["entity"]  # type: ignore[attr-defined]
        if entity is RagMedicationProduct:
            selected = self.products[self.product_query_count : self.product_query_count + 1]
            self.product_query_count += 1
            return SimpleNamespace(all=lambda: selected)
        if entity is RagMedicationIngredient:
            return SimpleNamespace(all=lambda: self.ingredients)
        raise AssertionError("unexpected query")


def _product_row(snapshot: object, number: int, *, strength: str | None = "10mg"):
    return SimpleNamespace(
        id=uuid4(),
        source_snapshot_id=snapshot.members[number].product_source_snapshot_id,  # type: ignore[attr-defined]
        code_system="MFDS_ITEM_SEQ",
        canonical_code=f"SYNTH-{number}",
        product_name="합성제품",
        strength_text=strength,
        dosage_form="정제",
        manufacturer_name="합성제약",
        product_status="ACTIVE",
    )


def _fake_hydrator(monkeypatch: pytest.MonkeyPatch, evidence: HydratedCandidateEvidence) -> None:
    class Hydrator:
        def __init__(self, session: object, *, index_code: str) -> None:
            assert index_code == "SYNTHETIC_INDEX"

        async def hydrate_evidence(self, request: object) -> HydratedCandidateEvidence:
            return evidence

    monkeypatch.setattr("app.services.candidate_search_resolution.CandidateResolverHydrationAdapter", Hydrator)


def test_strength_requires_actual_exact_canonical_match() -> None:
    matcher = _ConservativeAttributeMatcher()
    expected = ResolverInput("합성제품", "10 MG", "index-v1", "synthetic-only-v1")
    identity = OfficialIdentity(OfficialEntityType.PRODUCT, "MFDS_ITEM_SEQ", "SYNTH-0")

    def assess(strength: str | None) -> AttributeCompatibility:
        product = ProductSnapshot(identity, "합성제품", strength, "정제", "합성제약", ProductStatus.ACTIVE)
        result = matcher.assess(expected, product)
        assert result.dosage_form is AttributeCompatibility.NOT_APPLICABLE
        assert result.manufacturer is AttributeCompatibility.NOT_APPLICABLE
        return result.strength

    assert assess("10mg") is AttributeCompatibility.MATCH
    assert assess(None) is AttributeCompatibility.UNKNOWN
    assert assess("1 0 mg") is AttributeCompatibility.CONFLICT
    assert assess("20mg") is AttributeCompatibility.CONFLICT
    assert assess("0.01g") is AttributeCompatibility.CONFLICT
    assert (
        matcher.assess(
            ResolverInput("합성제품", None, "index-v1", "synthetic-only-v1"),
            ProductSnapshot(identity, "합성제품", None, None, None, ProductStatus.ACTIVE),
        ).strength
        is AttributeCompatibility.NOT_APPLICABLE
    )


def test_relevance_uses_actual_lexical_signal_score() -> None:
    snapshot = _snapshot()
    hit = _hit(snapshot, 0)
    candidate = CandidateEvidence(hit.identity, hit.product, (CandidateSignal(hit.stage, 1, 0.83),), 0.36)
    assert (
        _LexicalRelevanceEvaluator().evaluate(
            SimpleNamespace(),
            candidate,  # type: ignore[arg-type]
        )
        == 0.83
    )


def test_alias_entry_source_does_not_replace_product_source_identity() -> None:
    snapshot = _snapshot()
    alias_source = uuid4()
    snapshot.source_refs = (
        *snapshot.source_refs,
        SimpleNamespace(snapshot_id=str(alias_source), source_version="alias-v1"),
    )
    hit = _hit(snapshot, 0)
    candidate = CandidateEvidence(
        hit.identity,
        hit.product,
        (CandidateSignal(CandidateStage.APPROVED_ALIAS_EXACT, 1, 1.0),),
        0.27,
    )
    assert _product_source_for_candidate(snapshot, candidate) == snapshot.members[0].product_source_snapshot_id


@pytest.mark.asyncio
async def test_exact_product_resolves_ready_and_projects_actual_uuid_and_fusion_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    hit = _hit(snapshot, 0)
    row = _product_row(snapshot, 0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, (hit,)))
    result = await resolve_synthetic_candidate_search(
        _Session(products=(row,)),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text="10MG",
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.READY
    assert result.status_reason is None
    assert len(result.results) == 1
    assert result.results[0].product_id == row.id
    assert result.results[0].is_displayed and result.results[0].selection_eligible
    assert result.results[0].result_score == pytest.approx(4 / 11)


@pytest.mark.asyncio
async def test_approved_alias_resolves_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    hit = replace(_hit(snapshot, 0), stage=CandidateStage.APPROVED_ALIAS_EXACT, stage_score=1.0)
    row = _product_row(snapshot, 0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, (hit,)))
    result = await resolve_synthetic_candidate_search(
        _Session(products=(row,)),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text="10mg",
        policy=_policy(
            auto_select_stages=frozenset({CandidateStage.PRODUCT_NAME_EXACT, CandidateStage.APPROVED_ALIAS_EXACT})
        ),
    )
    assert result.status is MedicationCandidateSearchStatus.READY
    assert result.results[0].product_id == row.id
    assert result.results[0].selection_eligible


@pytest.mark.asyncio
async def test_positive_trigram_only_never_resolves_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    hit = replace(_hit(snapshot, 0), stage=CandidateStage.TRIGRAM_EDIT_DISTANCE, stage_score=1.0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, (hit,)))
    session = _Session()
    result = await resolve_synthetic_candidate_search(
        session,  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text="10mg",
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.NO_CANDIDATE
    assert result.results == []
    assert session.queries == []


@pytest.mark.asyncio
async def test_positive_trigram_competitor_remains_in_margin_and_ambiguous_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(count=2)
    exact = _hit(snapshot, 0, score=1.0)
    fuzzy = replace(_hit(snapshot, 1), stage=CandidateStage.TRIGRAM_EDIT_DISTANCE, rank=1, stage_score=1.0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, (exact, fuzzy)))
    result = await resolve_synthetic_candidate_search(
        _Session(products=(_product_row(snapshot, 0), _product_row(snapshot, 1))),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text="10mg",
        policy=_policy(minimum_margin=0.2),
    )
    assert result.status is MedicationCandidateSearchStatus.AMBIGUOUS
    assert len(result.results) == 2
    assert all(not row.selection_eligible and not row.is_displayed for row in result.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [CandidateStage.TRIGRAM_EDIT_DISTANCE, CandidateStage.DENSE_VECTOR])
async def test_unapproved_auto_select_stage_fails_before_hydration(
    monkeypatch: pytest.MonkeyPatch, stage: CandidateStage
) -> None:
    class UnexpectedHydrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("policy must be rejected before hydration")

    monkeypatch.setattr(
        "app.services.candidate_search_resolution.CandidateResolverHydrationAdapter", UnexpectedHydrator
    )
    session = _Session()
    with pytest.raises(ApiError) as error:
        await resolve_synthetic_candidate_search(
            session,  # type: ignore[arg-type]
            snapshot=_snapshot(),
            medication_name="합성제품",
            strength_text="10mg",
            policy=_policy(auto_select_stages=frozenset({CandidateStage.PRODUCT_NAME_EXACT, stage})),
        )
    assert error.value.status_code == 503
    assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"
    assert session.queries == []


@pytest.mark.asyncio
async def test_missing_strength_and_multiple_candidates_never_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(count=2)
    hits = (_hit(snapshot, 0), _hit(snapshot, 1))
    _fake_hydrator(monkeypatch, _evidence(snapshot, hits))
    first = await resolve_synthetic_candidate_search(
        _Session(products=(_product_row(snapshot, 0), _product_row(snapshot, 1))),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text=None,
        policy=_policy(),
    )
    assert first.status is MedicationCandidateSearchStatus.AMBIGUOUS
    assert all(not row.is_displayed and not row.selection_eligible for row in first.results)


@pytest.mark.asyncio
async def test_ingredient_only_requires_actual_catalog_row_in_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(count=0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, ()))
    ingredient = SimpleNamespace(
        entity_identity_id=uuid4(),
        source_snapshot_id=snapshot.source_refs[0].snapshot_id,
        ingredient_code_system="MFDS_INGREDIENT_CODE",
        ingredient_code="SYNTH-I-1",
    )
    session = _Session(ingredients=(ingredient,))
    result = await resolve_synthetic_candidate_search(
        session,  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성성분",
        strength_text=None,
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.INGREDIENT_ONLY
    assert result.status_reason == "PRODUCT_NAME_REQUIRED"
    assert result.results == []
    assert len(session.queries) == 2
    assert "canonical_code" in str(session.queries[1])
    assert session.queries[0].column_descriptions[0]["entity"] is RagMedicationIngredient
    query = str(session.queries[0])
    assert "source_snapshot_id" in query
    assert "normalized_ingredient_name" in query
    assert "ingredient_code_system" in query


@pytest.mark.asyncio
async def test_conflicting_product_is_no_candidate_with_no_result_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    _fake_hydrator(monkeypatch, _evidence(snapshot, (_hit(snapshot, 0),)))
    session = _Session()
    result = await resolve_synthetic_candidate_search(
        session,  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="합성제품",
        strength_text="20mg",
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.NO_CANDIDATE
    assert result.results == []
    assert session.queries == []


@pytest.mark.asyncio
async def test_no_catalog_hits_is_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(count=0)
    _fake_hydrator(monkeypatch, _evidence(snapshot, ()))
    result = await resolve_synthetic_candidate_search(
        _Session(),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="없는합성제품",
        strength_text=None,
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.NO_CANDIDATE
    assert result.results == []


@pytest.mark.asyncio
async def test_invalid_input_short_circuits_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()

    class UnexpectedHydrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("input must be validated first")

    monkeypatch.setattr(
        "app.services.candidate_search_resolution.CandidateResolverHydrationAdapter", UnexpectedHydrator
    )
    result = await resolve_synthetic_candidate_search(
        _Session(),  # type: ignore[arg-type]
        snapshot=snapshot,
        medication_name="  합성제품",
        strength_text=None,
        policy=_policy(),
    )
    assert result.status is MedicationCandidateSearchStatus.INVALID_INPUT
    assert result.status_reason == "INVALID_INPUT"
    assert result.results == []


@pytest.mark.asyncio
async def test_hybrid_and_dense_policy_fail_closed() -> None:
    for snapshot, policy in (
        (_snapshot(mode=RagCandidateIndexBuildMode.HYBRID), _policy()),
        (_snapshot(), _policy(enable_dense=True)),
    ):
        with pytest.raises(ApiError) as error:
            await resolve_synthetic_candidate_search(
                _Session(),  # type: ignore[arg-type]
                snapshot=snapshot,
                medication_name="합성제품",
                strength_text=None,
                policy=policy,
            )
        assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_product_field_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    _fake_hydrator(monkeypatch, _evidence(snapshot, (_hit(snapshot, 0),)))
    with pytest.raises(ApiError) as error:
        await resolve_synthetic_candidate_search(
            _Session(products=(_product_row(snapshot, 0, strength="20mg"),)),  # type: ignore[arg-type]
            snapshot=snapshot,
            medication_name="합성제품",
            strength_text="10mg",
            policy=_policy(),
        )
    assert error.value.code == "CANDIDATE_RESOLVER_UNAVAILABLE"

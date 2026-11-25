"""Internal synthetic Candidate Search resolution against a verified lexical index.

The caller owns Runtime/Source guards and the transaction. This module does not
issue a release decision or install a production policy.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models.rag_candidate import MedicationCandidateSearchStatus
from app.models.rag_candidate_index import RagCandidateIndexBuildMode
from app.models.rag_catalog import RagEntityIdentity, RagMedicationIngredient, RagMedicationProduct
from app.repositories.medication_candidate_repository import MedicationCandidateResultCreate
from app.repositories.rag_candidate_index_repository import VerifiedReadyCandidateIndexSnapshot
from app.services.rag.candidate_policy import CandidateStage, ResolverPolicy, policy_is_valid
from app.services.rag.candidate_resolver import (
    AttributeCompatibility,
    CandidateAttributeAssessment,
    CandidateEvidence,
    CandidateIndexMode,
    CandidateRelevanceEvaluatorError,
    CandidateSearchRequest,
    HydratedCandidateEvidence,
    IngredientHit,
    MedicationResolver,
    OfficialEntityType,
    OfficialIdentity,
    ProductSnapshot,
    ResolverFailure,
    ResolverInput,
    ResolverOutcome,
    ResolverResult,
    prepare_candidate_search,
)
from app.services.rag_candidate_resolver_hydration import (
    CandidateIndexHydrationError,
    CandidateResolverHydrationAdapter,
)


@dataclass(frozen=True, slots=True)
class CandidateSearchResolution:
    status: MedicationCandidateSearchStatus
    status_reason: str | None
    results: list[MedicationCandidateResultCreate]


@dataclass(frozen=True, slots=True)
class _PrehydratedPort:
    evidence: HydratedCandidateEvidence

    def hydrate(self, request: CandidateSearchRequest) -> HydratedCandidateEvidence:
        return self.evidence


class _ConservativeAttributeMatcher:
    def assess(self, resolver_input: ResolverInput, product: ProductSnapshot) -> CandidateAttributeAssessment:
        expected = resolver_input.strength_text
        actual = product.strength_text
        if expected is None:
            strength = AttributeCompatibility.NOT_APPLICABLE
        elif actual is None or not actual.strip():
            strength = AttributeCompatibility.UNKNOWN
        elif _canonical_strength(expected) == _canonical_strength(actual):
            strength = AttributeCompatibility.MATCH
        else:
            strength = AttributeCompatibility.CONFLICT
        return CandidateAttributeAssessment(
            strength=strength,
            dosage_form=AttributeCompatibility.NOT_APPLICABLE,
            manufacturer=AttributeCompatibility.NOT_APPLICABLE,
        )


def _canonical_strength(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", value).casefold().split())
    # Only number/unit separation in a single scalar strength may be ignored.
    # Never collapse whitespace inside a number, decimal, or combination expression.
    scalar = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?) ?([a-z]+)", normalized)
    return "".join(scalar.groups()) if scalar else normalized


class _LexicalRelevanceEvaluator:
    def evaluate(self, search_request: CandidateSearchRequest, candidate: CandidateEvidence) -> float:
        scores = [float(signal.stage_score) for signal in candidate.signals]
        if not scores or any(not math.isfinite(score) or not 0 <= score <= 1 for score in scores):
            raise CandidateRelevanceEvaluatorError()
        return max(scores)


def _unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        code="CANDIDATE_RESOLVER_UNAVAILABLE",
        message="약품 후보 검색을 사용할 수 없습니다.",
    )


async def _actual_ingredient_hits(
    session: AsyncSession,
    *,
    snapshot: VerifiedReadyCandidateIndexSnapshot,
    medication_name: str,
    limit: int,
) -> tuple[IngredientHit, ...]:
    source_ids = tuple(sorted((UUID(ref.snapshot_id) for ref in snapshot.source_refs), key=str))
    if not source_ids:
        raise _unavailable()
    statement = (
        select(RagMedicationIngredient)
        .where(
            RagMedicationIngredient.source_snapshot_id.in_(source_ids),
            RagMedicationIngredient.normalized_ingredient_name == medication_name,
            RagMedicationIngredient.ingredient_code_system == "MFDS_INGREDIENT_CODE",
        )
        .order_by(
            RagMedicationIngredient.source_snapshot_id,
            RagMedicationIngredient.ingredient_code,
            RagMedicationIngredient.id,
        )
        .limit(limit)
        .with_for_update(read=True, of=RagMedicationIngredient)
    )
    rows = tuple((await session.scalars(statement)).all())
    for row in rows:
        identity = await session.scalar(
            select(RagEntityIdentity)
            .where(
                RagEntityIdentity.id == row.entity_identity_id,
                RagEntityIdentity.entity_type == "INGREDIENT",
                RagEntityIdentity.code_system == row.ingredient_code_system,
                RagEntityIdentity.canonical_code == row.ingredient_code,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if identity is None:
            raise _unavailable()
    return tuple(
        IngredientHit(
            identity=OfficialIdentity(
                entity_type=OfficialEntityType.INGREDIENT,
                code_system=row.ingredient_code_system,
                canonical_code=row.ingredient_code,
            ),
            rank=rank,
            stage_score=1.0,  # An actual exact catalog name match, not a product relevance estimate.
            index_version=snapshot.version.index_version,
            catalog_version=snapshot.version.catalog_version,
            source_snapshot_id=str(row.source_snapshot_id),
            normalization_version=snapshot.version.normalization_version,
        )
        for rank, row in enumerate(rows, start=1)
    )


def _product_source_for_candidate(
    snapshot: VerifiedReadyCandidateIndexSnapshot,
    candidate: CandidateEvidence,
) -> UUID:
    product_sources = {
        member.product_source_snapshot_id
        for member in snapshot.members
        if member.identity_entity_type.value == OfficialEntityType.PRODUCT.value
        and member.identity_code_system == candidate.identity.code_system
        and member.identity_canonical_code == candidate.identity.canonical_code
    }
    allowed_sources = {UUID(ref.snapshot_id) for ref in snapshot.source_refs}
    if len(product_sources) != 1 or not product_sources <= allowed_sources:
        raise _unavailable()
    return next(iter(product_sources))


async def _project_results(
    session: AsyncSession,
    *,
    snapshot: VerifiedReadyCandidateIndexSnapshot,
    resolved: ResolverResult,
) -> list[MedicationCandidateResultCreate]:
    selected_identity = resolved.candidate.identity if resolved.candidate is not None else None
    results: list[MedicationCandidateResultCreate] = []
    for rank, evaluated in enumerate(resolved.internal_candidates, start=1):
        candidate = evaluated.evidence
        source_id = _product_source_for_candidate(snapshot, candidate)
        statement = (
            select(RagMedicationProduct)
            .join(RagEntityIdentity, RagEntityIdentity.id == RagMedicationProduct.entity_identity_id)
            .where(
                RagEntityIdentity.entity_type == "PRODUCT",
                RagEntityIdentity.code_system == candidate.identity.code_system,
                RagEntityIdentity.canonical_code == candidate.identity.canonical_code,
                RagMedicationProduct.source_snapshot_id == source_id,
                RagMedicationProduct.code_system == candidate.identity.code_system,
                RagMedicationProduct.canonical_code == candidate.identity.canonical_code,
            )
            .with_for_update(read=True, of=(RagMedicationProduct, RagEntityIdentity))
            .execution_options(populate_existing=True)
        )
        products = tuple((await session.scalars(statement)).all())
        if len(products) != 1:
            raise _unavailable()
        product = products[0]
        hydrated = candidate.product
        if (
            product.source_snapshot_id != source_id
            or product.code_system != candidate.identity.code_system
            or product.canonical_code != candidate.identity.canonical_code
            or product.product_name != hydrated.product_name
            or product.strength_text != hydrated.strength_text
            or product.dosage_form != hydrated.dosage_form
            or product.manufacturer_name != hydrated.manufacturer_name
            or product.product_status != hydrated.status.value
            or not math.isfinite(candidate.fusion_score)
        ):
            raise _unavailable()
        displayed = selected_identity == candidate.identity and evaluated.eligible
        results.append(
            MedicationCandidateResultCreate(
                product_id=product.id,
                code_system=candidate.identity.code_system,
                canonical_code=candidate.identity.canonical_code,
                product_name=product.product_name,
                strength_text=product.strength_text,
                dosage_form=product.dosage_form,
                manufacturer_name=product.manufacturer_name,
                product_status=product.product_status,
                result_rank=rank,
                result_score=candidate.fusion_score,
                result_method="LEXICAL_RRF_SYNTHETIC_V1",
                is_displayed=displayed,
                selection_eligible=displayed,
            )
        )
    return results


async def resolve_synthetic_candidate_search(
    session: AsyncSession,
    *,
    snapshot: VerifiedReadyCandidateIndexSnapshot,
    medication_name: str,
    strength_text: str | None,
    policy: ResolverPolicy,
) -> CandidateSearchResolution:
    """Resolve only an explicitly supplied synthetic policy and verified lexical index."""
    selectable_stages = frozenset({CandidateStage.PRODUCT_NAME_EXACT, CandidateStage.APPROVED_ALIAS_EXACT})
    if (
        not policy_is_valid(policy)
        or policy.enable_dense
        or policy.release_eligible
        or not policy.auto_select_stages <= selectable_stages
        or snapshot.version.build_mode is not RagCandidateIndexBuildMode.LEXICAL_ONLY
        or policy.retrieval_limit > snapshot.version.candidate_limit
    ):
        raise _unavailable()

    resolver_input = ResolverInput(
        medication_name=medication_name,
        strength_text=strength_text,
        index_version=snapshot.version.index_version,
        policy_version=policy.policy_version,
    )
    prepared = prepare_candidate_search(resolver_input, policy)
    if isinstance(prepared, ResolverFailure):
        raise _unavailable()
    if isinstance(prepared, ResolverResult):
        return CandidateSearchResolution(MedicationCandidateSearchStatus.INVALID_INPUT, "INVALID_INPUT", [])

    try:
        hydrated = await CandidateResolverHydrationAdapter(
            session, index_code=snapshot.version.index_code
        ).hydrate_evidence(prepared)
        if hydrated.provenance.index_mode is not CandidateIndexMode.LEXICAL_ONLY or (
            hydrated.provenance.index_version != snapshot.version.index_version
            or hydrated.provenance.catalog_version != snapshot.version.catalog_version
            or hydrated.provenance.catalog_manifest_hash != snapshot.version.catalog_manifest_hash
            or hydrated.provenance.normalization_version != snapshot.version.normalization_version
            or {(ref.snapshot_id, ref.source_version) for ref in hydrated.provenance.source_refs}
            != {(ref.snapshot_id, ref.source_version) for ref in snapshot.source_refs}
        ):
            raise _unavailable()
        # A zero-similarity trigram row is a storage top-K filler, not positive
        # lexical evidence. This synthetic policy keeps every positive hit,
        # including ineligible ones; it does not relabel them as ingredient-only.
        hydrated = replace(
            hydrated,
            product_hits=tuple(
                hit
                for hit in hydrated.product_hits
                if not (hit.stage is CandidateStage.TRIGRAM_EDIT_DISTANCE and hit.stage_score == 0.0)
            ),
        )
        if not hydrated.product_hits:
            ingredients = await _actual_ingredient_hits(
                session,
                snapshot=snapshot,
                medication_name=medication_name,
                limit=policy.retrieval_limit,
            )
            hydrated = HydratedCandidateEvidence(
                provenance=hydrated.provenance,
                product_hits=(),
                ingredient_hits=ingredients,
            )
    except (CandidateIndexHydrationError, ValueError) as exc:
        raise _unavailable() from exc

    resolved = MedicationResolver(
        index_port=_PrehydratedPort(hydrated),
        attribute_matcher=_ConservativeAttributeMatcher(),
        relevance_evaluator=_LexicalRelevanceEvaluator(),
    ).resolve(resolver_input, policy)
    if isinstance(resolved, ResolverFailure):
        raise _unavailable()

    status_map = {
        ResolverOutcome.SINGLE_CANDIDATE: MedicationCandidateSearchStatus.READY,
        ResolverOutcome.AMBIGUOUS: MedicationCandidateSearchStatus.AMBIGUOUS,
        ResolverOutcome.NO_CANDIDATE: MedicationCandidateSearchStatus.NO_CANDIDATE,
        ResolverOutcome.INGREDIENT_ONLY: MedicationCandidateSearchStatus.INGREDIENT_ONLY,
        ResolverOutcome.INVALID_INPUT: MedicationCandidateSearchStatus.INVALID_INPUT,
    }
    status = status_map[resolved.outcome]
    status_reason = (
        "PRODUCT_NAME_REQUIRED"
        if status is MedicationCandidateSearchStatus.INGREDIENT_ONLY
        else "INVALID_INPUT"
        if status is MedicationCandidateSearchStatus.INVALID_INPUT
        else None
    )
    # The Target contract stores zero Result rows for NO_CANDIDATE, including
    # cases where the resolver inspected products but rejected every one.
    results = (
        await _project_results(session, snapshot=snapshot, resolved=resolved)
        if status in {MedicationCandidateSearchStatus.READY, MedicationCandidateSearchStatus.AMBIGUOUS}
        else []
    )
    return CandidateSearchResolution(status=status, status_reason=status_reason, results=results)

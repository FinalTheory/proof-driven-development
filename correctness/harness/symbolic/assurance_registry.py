from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from ..model import Graph, UniqueKeyLoader
from .bridge import SymbolicBridge
from .translation_assurance import (
    AssuranceStatus,
    ClaimTranslationSubject,
    AssumptionTranslationSubject,
    ObligationTranslationSubject,
    ComparisonReview,
    RoundTripTranslation,
    TranslationReviewError,
    TranslationStatus,
    TranslationSubject,
    aggregate_translation_assurance,
    build_claim_translation_subject,
    build_assumption_translation_subject,
    build_translation_subject,
    parse_comparison_review,
    parse_roundtrip_translation,
    translation_subject_signature,
)


class SubjectTrustStatus(str, Enum):
    TRUSTED = "TRUSTED"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class TranslationAssuranceRecord:
    kind: str
    subject_id: str
    subject_signature: str
    roundtrip: RoundTripTranslation
    direct_reviews: dict[str, ComparisonReview]
    roundtrip_reviews: dict[str, ComparisonReview]


@dataclass(frozen=True)
class SubjectTrustResult:
    status: SubjectTrustStatus
    reason: str
    current_signature: str
    recorded_signature: str | None

    @property
    def trusted(self) -> bool:
        return self.status == SubjectTrustStatus.TRUSTED


@dataclass(frozen=True)
class TranslationAssuranceRegistry:
    version: int
    records: dict[str, TranslationAssuranceRecord]

    @classmethod
    def load(cls, path: Path) -> "TranslationAssuranceRegistry":
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
        except OSError as exc:
            raise TranslationReviewError(
                f"translation assurance registry unavailable at {path}: {exc}"
            ) from exc
        except yaml.YAMLError as exc:
            raise TranslationReviewError(
                f"invalid translation assurance registry YAML: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise TranslationReviewError(
                "translation assurance registry must be a mapping"
            )
        return cls.from_data(raw)

    @classmethod
    def from_data(cls, raw: dict[str, Any]) -> "TranslationAssuranceRegistry":
        if set(raw) != {"version", "records"}:
            raise TranslationReviewError(
                "translation assurance registry must contain exactly version and records"
            )
        if raw.get("version") != 1:
            raise TranslationReviewError(
                "translation assurance registry version must be 1"
            )
        records_raw = raw.get("records")
        if not isinstance(records_raw, dict):
            raise TranslationReviewError(
                "translation assurance registry records must be a mapping"
            )

        records: dict[str, TranslationAssuranceRecord] = {}
        for key, item in records_raw.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise TranslationReviewError(
                    "translation assurance records must be named mappings"
                )
            expected = {
                "kind",
                "subject_id",
                "subject_signature",
                "roundtrip",
                "direct_reviews",
                "roundtrip_reviews",
            }
            if set(item) != expected:
                raise TranslationReviewError(
                    f"assurance record {key} must contain exactly {sorted(expected)}"
                )
            kind = item["kind"]
            subject_id = item["subject_id"]
            signature = item["subject_signature"]
            if kind not in {"contract", "claim", "assumption", "obligation"}:
                raise TranslationReviewError(
                    f"assurance record {key}.kind must be contract, claim, assumption, or obligation"
                )
            if not isinstance(subject_id, str) or not subject_id:
                raise TranslationReviewError(
                    f"assurance record {key}.subject_id must be non-empty"
                )
            expected_key = f"{kind}:{subject_id}"
            if key != expected_key:
                raise TranslationReviewError(
                    f"assurance record key {key!r} must equal {expected_key!r}"
                )
            if (
                not isinstance(signature, str)
                or re.fullmatch(r"[0-9a-f]{64}", signature) is None
            ):
                raise TranslationReviewError(
                    f"assurance record {key}.subject_signature must be sha256 hex"
                )

            roundtrip = _roundtrip_from_mapping(item["roundtrip"], key)
            direct = _review_map(item["direct_reviews"], f"{key}.direct_reviews")
            roundtrip_reviews = _review_map(
                item["roundtrip_reviews"], f"{key}.roundtrip_reviews"
            )
            artifact_signatures = {
                "roundtrip": roundtrip.subject_signature,
                **{
                    f"direct_reviews.{role}": review.subject_signature
                    for role, review in direct.items()
                },
                **{
                    f"roundtrip_reviews.{role}": review.subject_signature
                    for role, review in roundtrip_reviews.items()
                },
            }
            mismatched_artifacts = sorted(
                label
                for label, artifact_signature in artifact_signatures.items()
                if artifact_signature != signature
            )
            if mismatched_artifacts:
                raise TranslationReviewError(
                    f"assurance record {key} has reviewer artifacts for a different "
                    f"subject signature: {mismatched_artifacts}"
                )

            required_roles = {"weakening", "strengthening"}
            if not required_roles.issubset(direct):
                raise TranslationReviewError(
                    f"{key}.direct_reviews must include {sorted(required_roles)}"
                )
            if not required_roles.issubset(roundtrip_reviews):
                raise TranslationReviewError(
                    f"{key}.roundtrip_reviews must include {sorted(required_roles)}"
                )

            records[key] = TranslationAssuranceRecord(
                kind=kind,
                subject_id=subject_id,
                subject_signature=signature,
                roundtrip=roundtrip,
                direct_reviews=direct,
                roundtrip_reviews=roundtrip_reviews,
            )
        return cls(version=1, records=records)

    def evaluate(
        self,
        graph: Graph,
        bridge: SymbolicBridge,
        *,
        kind: str,
        subject_id: str,
    ) -> SubjectTrustResult:
        if kind == "obligation":
            raise TranslationReviewError(
                "obligation trust evaluation requires an explicit obligation translation subject"
            )
        subject = _build_subject(graph, bridge, kind=kind, subject_id=subject_id)
        return self.evaluate_subject(kind=kind, subject_id=subject_id, subject=subject)

    def evaluate_subject(
        self,
        *,
        kind: str,
        subject_id: str,
        subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
    ) -> SubjectTrustResult:
        current_signature = translation_subject_signature(subject)
        key = f"{kind}:{subject_id}"
        record = self.records.get(key)
        if record is None:
            return SubjectTrustResult(
                status=SubjectTrustStatus.UNVERIFIED,
                reason="no translation-assurance record exists for this subject",
                current_signature=current_signature,
                recorded_signature=None,
            )
        if record.subject_signature != current_signature:
            return SubjectTrustResult(
                status=SubjectTrustStatus.STALE,
                reason="source or symbolic translation changed since semantic review",
                current_signature=current_signature,
                recorded_signature=record.subject_signature,
            )
        if (
            record.roundtrip.status != TranslationStatus.TRANSLATED
            or record.roundtrip.ambiguities
        ):
            return SubjectTrustResult(
                status=SubjectTrustStatus.INCOMPLETE,
                reason="blind symbolic-to-natural-language round trip is incomplete or ambiguous",
                current_signature=current_signature,
                recorded_signature=record.subject_signature,
            )

        summary = aggregate_translation_assurance(
            direct_reviews=record.direct_reviews.values(),
            roundtrip_reviews=record.roundtrip_reviews.values(),
        )
        status_map = {
            AssuranceStatus.TRUSTED: SubjectTrustStatus.TRUSTED,
            AssuranceStatus.REJECTED: SubjectTrustStatus.REJECTED,
            AssuranceStatus.INCOMPLETE: SubjectTrustStatus.INCOMPLETE,
        }
        return SubjectTrustResult(
            status=status_map[summary.status],
            reason=summary.reason,
            current_signature=current_signature,
            recorded_signature=record.subject_signature,
        )

    def require_trusted(
        self,
        graph: Graph,
        bridge: SymbolicBridge,
        *,
        kind: str,
        subject_id: str,
    ) -> None:
        result = self.evaluate(
            graph,
            bridge,
            kind=kind,
            subject_id=subject_id,
        )
        if not result.trusted:
            raise TranslationReviewError(
                f"{kind} {subject_id} symbolic translation is not trusted: "
                f"{result.status.value}: {result.reason}"
            )


def assurance_record_payload(
    subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
    *,
    roundtrip: RoundTripTranslation,
    direct_reviews: Mapping[str, ComparisonReview],
    roundtrip_reviews: Mapping[str, ComparisonReview],
) -> dict[str, Any]:
    if isinstance(subject, TranslationSubject):
        kind = "contract"
        subject_id = subject.contract_id
    elif isinstance(subject, ClaimTranslationSubject):
        kind = "claim"
        subject_id = subject.claim_id
    elif isinstance(subject, AssumptionTranslationSubject):
        kind = "assumption"
        subject_id = subject.assumption_id
    elif isinstance(subject, ObligationTranslationSubject):
        kind = "obligation"
        subject_id = subject.obligation_id
    else:  # pragma: no cover - defensive
        raise TypeError(f"unsupported translation subject {type(subject).__name__}")

    subject_signature = translation_subject_signature(subject)
    artifacts = [
        ("roundtrip", roundtrip.subject_signature),
        *[
            (f"direct_reviews.{role}", review.subject_signature)
            for role, review in direct_reviews.items()
        ],
        *[
            (f"roundtrip_reviews.{role}", review.subject_signature)
            for role, review in roundtrip_reviews.items()
        ],
    ]
    mismatched = [
        label for label, signature in artifacts if signature != subject_signature
    ]
    if mismatched:
        raise TranslationReviewError(
            "review artifacts do not match current subject signature: "
            + ", ".join(sorted(mismatched))
        )

    return {
        "kind": kind,
        "subject_id": subject_id,
        "subject_signature": subject_signature,
        "roundtrip": {
            "subject_signature": roundtrip.subject_signature,
            "status": roundtrip.status.value,
            "natural_language": roundtrip.natural_language,
            "ambiguities": list(roundtrip.ambiguities),
        },
        "direct_reviews": {
            role: _review_payload(review)
            for role, review in direct_reviews.items()
        },
        "roundtrip_reviews": {
            role: _review_payload(review)
            for role, review in roundtrip_reviews.items()
        },
    }


def _review_payload(review: ComparisonReview) -> dict[str, Any]:
    return {
        "subject_signature": review.subject_signature,
        "verdict": review.verdict.value,
        "reason": review.reason,
        "differences": list(review.differences),
    }


def _roundtrip_from_mapping(value: Any, where: str) -> RoundTripTranslation:
    if not isinstance(value, dict):
        raise TranslationReviewError(f"{where}.roundtrip must be a mapping")
    return parse_roundtrip_translation(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    )


def _review_map(value: Any, where: str) -> dict[str, ComparisonReview]:
    if not isinstance(value, dict):
        raise TranslationReviewError(f"{where} must be a mapping")
    result: dict[str, ComparisonReview] = {}
    for role, review in value.items():
        if not isinstance(role, str) or not role:
            raise TranslationReviewError(f"{where} reviewer roles must be strings")
        if not isinstance(review, dict):
            raise TranslationReviewError(f"{where}.{role} must be a mapping")
        result[role] = parse_comparison_review(
            yaml.safe_dump(review, sort_keys=False, allow_unicode=True)
        )
    return result


def _build_subject(
    graph: Graph,
    bridge: SymbolicBridge,
    *,
    kind: str,
    subject_id: str,
) -> TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject:
    if kind == "contract":
        return build_translation_subject(graph, bridge, subject_id)
    if kind == "claim":
        return build_claim_translation_subject(graph, bridge, subject_id)
    if kind == "assumption":
        return build_assumption_translation_subject(graph, bridge, subject_id)
    raise TranslationReviewError(f"unknown translation subject kind {kind!r}")

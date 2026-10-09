from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..model import Graph, _catalog_namespace
from .bridge import SymbolicBridge


class TranslationReviewError(ValueError):
    """Raised when a translation-assurance artifact violates the review contract."""


class ComparisonVerdict(str, Enum):
    EQUIVALENT = "EQUIVALENT"
    WEAKER = "WEAKER"
    STRONGER = "STRONGER"
    MISMATCH = "MISMATCH"
    INCOMPLETE = "INCOMPLETE"


class TranslationStatus(str, Enum):
    TRANSLATED = "TRANSLATED"
    INCOMPLETE = "INCOMPLETE"


class AssuranceStatus(str, Enum):
    TRUSTED = "TRUSTED"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class RoundTripTranslation:
    status: TranslationStatus
    natural_language: str
    ambiguities: tuple[str, ...]
    subject_signature: str = ""


@dataclass(frozen=True)
class ComparisonReview:
    verdict: ComparisonVerdict
    reason: str
    differences: tuple[str, ...]
    subject_signature: str = ""


@dataclass(frozen=True)
class TranslationAssuranceSummary:
    status: AssuranceStatus
    direct_reviews: tuple[ComparisonReview, ...]
    roundtrip_reviews: tuple[ComparisonReview, ...]
    reason: str


@dataclass(frozen=True)
class TranslationSubject:
    contract_id: str
    canonical_contract: dict[str, Any]
    contract_class: str
    observation_scope: str | None
    formula: dict[str, Any]
    functions: dict[str, dict[str, Any]]
    catalog_symbols: dict[str, Any]

    def symbolic_context(self) -> str:
        payload = {
            "contract_semantics": {
                "class": self.contract_class,
                "observation_scope": self.observation_scope,
                "interpretation": _contract_class_interpretation(
                    self.contract_class, self.observation_scope
                ),
            },
            "functions": self.functions,
            "formula": self.formula,
            "catalog_symbols": self.catalog_symbols,
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    def source_context(self) -> str:
        payload = {
            "canonical_contract": {
                "contract_id": self.contract_id,
                **self.canonical_contract,
            },
            "canonical_vocabulary": self.catalog_symbols,
            "contract_semantics": {
                "class": self.contract_class,
                "observation_scope": self.observation_scope,
                "interpretation": _contract_class_interpretation(
                    self.contract_class, self.observation_scope
                ),
            },
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


@dataclass(frozen=True)
class ClaimTranslationSubject:
    claim_id: str
    canonical_claim: dict[str, Any]
    contract_ids: tuple[str, ...]
    canonical_contracts: dict[str, dict[str, Any]]
    formula: dict[str, Any]
    functions: dict[str, dict[str, Any]]
    catalog_symbols: dict[str, Any]

    def symbolic_context(self) -> str:
        payload = {
            "claim_mapping_semantics": (
                "This formula is the explicit symbolic translation of the claim statement. "
                "The referenced semantic contracts provide canonical vocabulary/relations, "
                "but their entire formulas are not implicitly conjoined into the claim."
            ),
            "referenced_semantic_contracts": list(self.contract_ids),
            "functions": self.functions,
            "formula": self.formula,
            "catalog_symbols": self.catalog_symbols,
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    def source_context(self) -> str:
        selected = {
            "canonical_claim": {
                "claim_id": self.claim_id,
                "kind": self.canonical_claim.get("kind"),
                "statement": self.canonical_claim.get("statement"),
                "formal_intent": self.canonical_claim.get("formal_intent"),
                "semantic_contracts": self.canonical_claim.get("semantic_contracts", []),
            },
            "referenced_semantic_contracts_context_only": self.canonical_contracts,
            "canonical_vocabulary": self.catalog_symbols,
            "interpretation": (
                "Referenced semantic contracts and vocabulary define terms/relations used by "
                "the claim. They are context only; the claim's guarantee is exactly its own "
                "statement/formal intent and is not automatically strengthened to the full "
                "referenced contract definitions."
            ),
        }
        return yaml.safe_dump(selected, sort_keys=False, allow_unicode=True)



@dataclass(frozen=True)
class AssumptionTranslationSubject:
    assumption_id: str
    canonical_assumption: dict[str, Any]
    formula: dict[str, Any]
    functions: dict[str, dict[str, Any]]
    catalog_symbols: dict[str, Any]

    def symbolic_context(self) -> str:
        payload = {
            "assumption_mapping_semantics": (
                "This formula is the explicit symbolic translation of one canonical proof assumption. "
                "The assumption is accepted as a premise; translation assurance checks only semantic fidelity."
            ),
            "functions": self.functions,
            "formula": self.formula,
            "catalog_symbols": self.catalog_symbols,
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    def source_context(self) -> str:
        payload = {
            "canonical_assumption": {
                "assumption_id": self.assumption_id,
                "statement": self.canonical_assumption.get("statement"),
                "status": self.canonical_assumption.get("status"),
                "note": self.canonical_assumption.get("note"),
            },
            "canonical_vocabulary": self.catalog_symbols,
            "interpretation": (
                "The assumption statement is the authoritative proposition. Status/note explain its proof boundary "
                "but must not silently strengthen or weaken the symbolic formula."
            ),
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


@dataclass(frozen=True)
class ObligationTranslationSubject:
    obligation_id: str
    statement: str
    description: str
    source_refs: tuple[str, ...]
    source_sections: dict[str, str]
    formula: dict[str, Any]
    functions: dict[str, dict[str, Any]]
    catalog_symbols: dict[str, Any]

    def symbolic_context(self) -> str:
        payload = {
            "design_obligation_mapping_semantics": (
                "This formula is the explicit symbolic translation of an independently extracted required design property. "
                "The formula is an oracle above the correctness DAG, not a proof premise generated from DAG roots."
            ),
            "functions": self.functions,
            "formula": self.formula,
            "catalog_symbols": self.catalog_symbols,
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    def source_context(self) -> str:
        payload = {
            "design_obligation": {
                "obligation_id": self.obligation_id,
                "statement": self.statement,
                "description": self.description,
                "source_refs": list(self.source_refs),
            },
            "canonical_source_sections": self.source_sections,
            "canonical_vocabulary": self.catalog_symbols,
            "interpretation": (
                "The cited canonical design sections are semantic authority. The obligation statement is the extracted "
                "required property under review. A faithful translation must neither invent a requirement unsupported by "
                "those sections nor omit/strengthen the extracted property's applicability, quantifiers, identity, state, "
                "or transition boundaries."
            ),
        }
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def build_assumption_translation_subject(
    graph: Graph,
    bridge: SymbolicBridge,
    assumption_id: str,
) -> AssumptionTranslationSubject:
    bridge.validate_against_graph(graph)
    mapping = bridge.assumptions.get(assumption_id)
    if mapping is None:
        raise TranslationReviewError(f"no symbolic assumption mapping for {assumption_id}")
    assumption = graph.assumptions.get(assumption_id)
    if not isinstance(assumption, dict):
        raise TranslationReviewError(f"unknown canonical assumption {assumption_id}")
    functions, symbols = _translation_function_and_catalog_context(
        graph, bridge, mapping.formula, mapping.catalog_symbols
    )
    return AssumptionTranslationSubject(
        assumption_id=assumption_id,
        canonical_assumption=dict(assumption),
        formula=mapping.formula,
        functions=functions,
        catalog_symbols=symbols,
    )


def build_obligation_translation_subject(
    graph: Graph,
    bridge: SymbolicBridge,
    *,
    obligation_id: str,
    statement: str,
    description: str,
    source_refs: tuple[str, ...] | list[str],
    formula: dict[str, Any],
) -> ObligationTranslationSubject:
    bridge.validate_against_graph(graph)
    source_sections = _resolve_source_sections(graph, tuple(source_refs))
    used_functions = _formula_function_names(formula)
    unknown = sorted(used_functions - set(bridge.functions))
    if unknown:
        raise TranslationReviewError(
            f"design obligation {obligation_id} uses unknown symbolic functions {unknown}"
        )
    symbol_names: set[str] = set()
    for fn in used_functions:
        symbol_names.update(bridge.functions[fn].get("catalog_symbols", []))
    functions, symbols = _translation_function_and_catalog_context(
        graph, bridge, formula, frozenset(symbol_names)
    )
    return ObligationTranslationSubject(
        obligation_id=obligation_id,
        statement=statement,
        description=description,
        source_refs=tuple(source_refs),
        source_sections=source_sections,
        formula=formula,
        functions=functions,
        catalog_symbols=symbols,
    )


def build_claim_translation_subject(
    graph: Graph,
    bridge: SymbolicBridge,
    claim_id: str,
) -> ClaimTranslationSubject:
    bridge.validate_against_graph(graph)
    mapping = bridge.claims.get(claim_id)
    if mapping is None:
        raise TranslationReviewError(f"no symbolic claim mapping for {claim_id}")
    claim = graph.claims.get(claim_id)
    if not isinstance(claim, dict):
        raise TranslationReviewError(f"unknown canonical claim {claim_id}")

    semantic_contracts = _catalog_namespace(graph, "semantic_contracts")
    canonical_contracts: dict[str, dict[str, Any]] = {}
    for contract_id in mapping.contract_ids:
        contract = semantic_contracts.get(contract_id)
        if not isinstance(contract, dict):
            raise TranslationReviewError(
                f"claim {claim_id} references missing canonical semantic contract {contract_id}"
            )
        canonical_contracts[contract_id] = dict(contract)

    function_names = _formula_function_names(mapping.formula)
    functions = {
        name: {
            key: value
            for key, value in bridge.functions[name].items()
            if key != "semantic_anchors"
        }
        for name in sorted(function_names)
    }
    terms = _catalog_namespace(graph, "terms")
    state = _catalog_namespace(graph, "state")
    symbol_defs: dict[str, Any] = {}
    for symbol in sorted(mapping.catalog_symbols):
        if symbol in terms:
            symbol_defs[symbol] = {"namespace": "terms", "definition": terms[symbol]}
        elif symbol in state:
            symbol_defs[symbol] = {"namespace": "state", "definition": state[symbol]}
        else:
            raise TranslationReviewError(
                f"symbolic claim mapping references undefined catalog symbol {symbol}"
            )

    return ClaimTranslationSubject(
        claim_id=claim_id,
        canonical_claim=dict(claim),
        contract_ids=mapping.contract_ids,
        canonical_contracts=canonical_contracts,
        formula=mapping.formula,
        functions=functions,
        catalog_symbols=symbol_defs,
    )


def build_translation_subject(
    graph: Graph,
    bridge: SymbolicBridge,
    contract_id: str,
) -> TranslationSubject:
    bridge.validate_against_graph(graph)
    mapping = bridge.contracts.get(contract_id)
    if mapping is None:
        raise TranslationReviewError(f"no symbolic mapping for {contract_id}")

    semantic_contracts = _catalog_namespace(graph, "semantic_contracts")
    canonical = semantic_contracts.get(contract_id)
    if not isinstance(canonical, dict):
        raise TranslationReviewError(f"unknown canonical semantic contract {contract_id}")

    function_names = _formula_function_names(mapping.formula)
    functions = {
        name: {
            key: value
            for key, value in bridge.functions[name].items()
            if key != "semantic_anchors"
        }
        for name in sorted(function_names)
    }

    terms = _catalog_namespace(graph, "terms")
    state = _catalog_namespace(graph, "state")
    symbol_defs: dict[str, Any] = {}
    for symbol in sorted(mapping.catalog_symbols):
        if symbol in terms:
            symbol_defs[symbol] = {"namespace": "terms", "definition": terms[symbol]}
        elif symbol in state:
            symbol_defs[symbol] = {"namespace": "state", "definition": state[symbol]}
        else:  # guarded by bridge validation, retained as a hard invariant
            raise TranslationReviewError(
                f"symbolic mapping references undefined catalog symbol {symbol}"
            )

    return TranslationSubject(
        contract_id=contract_id,
        canonical_contract=dict(canonical),
        contract_class=mapping.contract_class,
        observation_scope=mapping.observation_scope,
        formula=mapping.formula,
        functions=functions,
        catalog_symbols=symbol_defs,
    )



def _translation_function_and_catalog_context(
    graph: Graph,
    bridge: SymbolicBridge,
    formula: dict[str, Any],
    catalog_symbol_names: frozenset[str] | set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    function_names = _formula_function_names(formula)
    functions = {
        name: {
            key: value
            for key, value in bridge.functions[name].items()
            if key != "semantic_anchors"
        }
        for name in sorted(function_names)
    }
    terms = _catalog_namespace(graph, "terms")
    state = _catalog_namespace(graph, "state")
    symbol_defs: dict[str, Any] = {}
    for symbol in sorted(catalog_symbol_names):
        if symbol in terms:
            symbol_defs[symbol] = {"namespace": "terms", "definition": terms[symbol]}
        elif symbol in state:
            symbol_defs[symbol] = {"namespace": "state", "definition": state[symbol]}
        else:
            raise TranslationReviewError(
                f"symbolic mapping references undefined catalog symbol {symbol}"
            )
    return functions, symbol_defs


def _resolve_source_sections(graph: Graph, source_refs: tuple[str, ...]) -> dict[str, str]:
    if graph.repository_root is None:
        raise TranslationReviewError("design-obligation translation assurance requires repository context")
    source = graph.doc.get("source", {})
    article_ref = source.get("article") if isinstance(source, dict) else None
    if not isinstance(article_ref, str) or not article_ref:
        raise TranslationReviewError("canonical source.article is unavailable")
    article_path = graph.repository_root / article_ref
    try:
        text = article_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TranslationReviewError(f"canonical source article unavailable: {exc}") from exc
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    by_heading: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        raw = match.group(1).strip()
        heading = re.sub(r"^[0-9]+\.\s+", "", raw).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        by_heading.setdefault(heading, []).append(text[match.start():end].strip())
    sources = _catalog_namespace(graph, "sources")
    result: dict[str, str] = {}
    for source_id in source_refs:
        entry = sources.get(source_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("heading"), str):
            raise TranslationReviewError(f"unknown canonical source ref {source_id}")
        sections = by_heading.get(entry["heading"], [])
        if len(sections) != 1:
            raise TranslationReviewError(
                f"canonical source ref {source_id} resolves to {len(sections)} sections"
            )
        result[source_id] = sections[0]
    return result


def translation_subject_signature(
    subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
) -> str:
    """Digest the exact semantic source context and symbolic translation under review."""

    payload = {
        "source": yaml.safe_load(subject.source_context()),
        "symbolic": yaml.safe_load(subject.symbolic_context()),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_roundtrip_translation_prompt(
    subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
) -> str:
    """Blind symbolic -> NL translation. Deliberately excludes the source contract text."""

    subject_signature = translation_subject_signature(subject)
    return f"""You are performing a blind semantic round-trip for one symbolic correctness proposition.

You MUST NOT infer or reconstruct any unavailable source prose. Interpret only the typed symbolic material below.

SUBJECT SIGNATURE
{subject_signature}

SYMBOLIC MATERIAL
{subject.symbolic_context()}

TASK
Translate the formula into precise natural-language semantics.
Preserve every quantifier, implication guard, equality/inequality, existence requirement, scope condition encoded by predicates, and conjunction/disjunction.
Preserve semantic coordinates carried by typed function arguments; do not collapse distinct entity, state, or observation dimensions into one timeless or unscoped value.
Do not strengthen the formula. Do not add likely product requirements. Do not omit awkward conditions.
Predicate/function meanings and declared dimensions above are authoritative for this task.

Return exactly one YAML object:
subject_signature: {subject_signature}
status: TRANSLATED | INCOMPLETE
natural_language: >-
  <complete standalone natural-language proposition>
ambiguities:
  - <only if the symbolic material itself is insufficient or ambiguous; use ambiguities: [] when none>

Echo subject_signature exactly as supplied above.
Always emit the ambiguities field; use ambiguities: [] when there are none.
Use INCOMPLETE rather than guessing if a predicate meaning or binding is insufficient.
"""


def build_roundtrip_comparison_prompt(
    subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
    roundtrip_natural_language: str,
    *,
    focus: str = "balanced",
) -> str:
    focus_instruction = _comparison_focus(focus)
    subject_signature = translation_subject_signature(subject)
    obligation_instruction = (
        "For a design obligation, also verify that the extracted required-property statement is actually supported "
        "by the cited canonical design sections. If the statement adds a requirement not committed to by those "
        "sections, classify it as STRONGER rather than treating the extracted statement as unquestioned authority."
        if isinstance(subject, ObligationTranslationSubject) else ""
    )
    return f"""You are independently auditing semantic equivalence between two natural-language propositions.

SUBJECT SIGNATURE
{subject_signature}

ORIGINAL CANONICAL PROPOSITION
{subject.source_context()}

BLIND ROUND-TRIP RENDERING
{roundtrip_natural_language}

Your only question is whether the round-trip rendering preserves the original contract's semantics.
Check quantifiers, applicability/precondition boundaries, identity coordinates, provenance direction, cardinality, observation scope, exclusions, and whether either side makes a stronger guarantee.
Also check semantic-dimensionality preservation: distinct entity/state/observation coordinates must not be collapsed, and helper predicate/function meanings must not silently supply a cross-claim temporal, identity, or provenance bridge absent from the canonical source.
{focus_instruction}
{obligation_instruction}

Do not redesign the system and do not propose missing product requirements.
Verdict semantics are strict:
- WEAKER: the compared rendering/formula omits at least one source requirement.
- STRONGER: it adds at least one requirement outside the source contract.
- MISMATCH: the two differ in both directions or encode materially different relations.
- INCOMPLETE: the supplied material is insufficient to decide equivalence. Do not use INCOMPLETE merely because you found a concrete difference.

Return exactly one YAML object:
subject_signature: {subject_signature}
verdict: EQUIVALENT | WEAKER | STRONGER | MISMATCH | INCOMPLETE
reason: >-
  <short precise justification>
If verdict is EQUIVALENT, emit exactly:
differences: []
Otherwise emit:
differences:
  - >-
    <specific semantic difference>

Echo subject_signature exactly as supplied above.
"""


def build_direct_comparison_prompt(
    subject: TranslationSubject | ClaimTranslationSubject | AssumptionTranslationSubject | ObligationTranslationSubject,
    *,
    focus: str = "balanced",
) -> str:
    focus_instruction = _comparison_focus(focus)
    subject_signature = translation_subject_signature(subject)
    obligation_instruction = (
        "For a design obligation, first verify that the extracted required-property statement is actually supported "
        "by the cited canonical design sections. If the statement adds a requirement not committed to by those "
        "sections, classify it as STRONGER rather than treating the extracted statement as unquestioned authority."
        if isinstance(subject, ObligationTranslationSubject) else ""
    )
    return f"""You are independently auditing whether a typed symbolic formula faithfully translates one canonical natural-language correctness proposition.

SUBJECT SIGNATURE
{subject_signature}

ORIGINAL CANONICAL PROPOSITION
{subject.source_context()}

SYMBOLIC TRANSLATION
{subject.symbolic_context()}

Compare the original contract directly against the formula using the supplied typed predicate meanings and catalog definitions.
Check quantifiers, applicability/precondition boundaries, identity coordinates, provenance direction, cardinality, observation scope, exclusions, and any silent strengthening or weakening.
Also check semantic-dimensionality preservation: distinct entity/state/observation coordinates must not be collapsed, and helper predicate/function meanings must not silently supply a cross-claim temporal, identity, or provenance bridge absent from the canonical source.
{focus_instruction}
{obligation_instruction}

Do not judge whether the original proposition is a good requirement. Judge only translation fidelity.
Verdict semantics are strict:
- WEAKER: the symbolic translation omits at least one source requirement.
- STRONGER: it adds at least one requirement outside the source contract.
- MISMATCH: the two differ in both directions or encode materially different relations.
- INCOMPLETE: the supplied material is insufficient to decide equivalence. Do not use INCOMPLETE merely because you found a concrete difference.

Return exactly one YAML object:
subject_signature: {subject_signature}
verdict: EQUIVALENT | WEAKER | STRONGER | MISMATCH | INCOMPLETE
reason: >-
  <short precise justification>
If verdict is EQUIVALENT, emit exactly:
differences: []
Otherwise emit:
differences:
  - >-
    <specific semantic difference>

Echo subject_signature exactly as supplied above.
"""


def parse_roundtrip_translation(text: str) -> RoundTripTranslation:
    data = _parse_yaml_object(text, "round-trip translation")
    if set(data) != {"subject_signature", "status", "natural_language", "ambiguities"}:
        raise TranslationReviewError(
            "round-trip output must contain exactly subject_signature, status, natural_language, ambiguities"
        )
    subject_signature = _parse_subject_signature(
        data["subject_signature"], "round-trip subject_signature"
    )
    try:
        status = TranslationStatus(data["status"])
    except (TypeError, ValueError) as exc:
        raise TranslationReviewError(f"invalid round-trip status {data.get('status')!r}") from exc
    natural_language = data["natural_language"]
    ambiguities = data["ambiguities"]
    if not isinstance(natural_language, str):
        raise TranslationReviewError("natural_language must be a string")
    if not isinstance(ambiguities, list) or not all(
        isinstance(item, str) for item in ambiguities
    ):
        raise TranslationReviewError("ambiguities must be a list of strings")
    if status == TranslationStatus.TRANSLATED and not natural_language.strip():
        raise TranslationReviewError("TRANSLATED requires non-empty natural_language")
    return RoundTripTranslation(
        status=status,
        natural_language=natural_language.strip(),
        ambiguities=tuple(ambiguities),
        subject_signature=subject_signature,
    )


def parse_comparison_review(text: str) -> ComparisonReview:
    data = _parse_yaml_object(text, "comparison review")
    if set(data) != {"subject_signature", "verdict", "reason", "differences"}:
        raise TranslationReviewError(
            "comparison output must contain exactly subject_signature, verdict, reason, differences"
        )
    subject_signature = _parse_subject_signature(
        data["subject_signature"], "comparison subject_signature"
    )
    try:
        verdict = ComparisonVerdict(data["verdict"])
    except (TypeError, ValueError) as exc:
        raise TranslationReviewError(f"invalid comparison verdict {data.get('verdict')!r}") from exc
    reason = data["reason"]
    differences = data["differences"]
    if not isinstance(reason, str) or not reason.strip():
        raise TranslationReviewError("comparison reason must be a non-empty string")
    if not isinstance(differences, list) or not all(
        isinstance(item, str) for item in differences
    ):
        raise TranslationReviewError("differences must be a list of strings")
    if verdict == ComparisonVerdict.EQUIVALENT and differences:
        raise TranslationReviewError("EQUIVALENT review must have an empty differences list")
    return ComparisonReview(verdict, reason.strip(), tuple(differences), subject_signature)



def parse_roundtrip_translation_or_incomplete(text: str) -> RoundTripTranslation:
    try:
        return parse_roundtrip_translation(text)
    except TranslationReviewError as exc:
        return RoundTripTranslation(
            status=TranslationStatus.INCOMPLETE,
            natural_language="",
            ambiguities=(f"invalid reviewer artifact: {exc}",),
        )


def parse_comparison_review_or_incomplete(text: str) -> ComparisonReview:
    try:
        return parse_comparison_review(text)
    except TranslationReviewError as exc:
        return ComparisonReview(
            verdict=ComparisonVerdict.INCOMPLETE,
            reason=f"invalid reviewer artifact: {exc}",
            differences=(),
        )


def aggregate_translation_assurance(
    *,
    direct_reviews: Iterable[ComparisonReview],
    roundtrip_reviews: Iterable[ComparisonReview],
    min_direct_reviews: int = 2,
    min_roundtrip_reviews: int = 2,
) -> TranslationAssuranceSummary:
    direct = tuple(direct_reviews)
    roundtrip = tuple(roundtrip_reviews)

    all_reviews = direct + roundtrip
    rejected = [
        review
        for review in all_reviews
        if review.verdict
        in {
            ComparisonVerdict.WEAKER,
            ComparisonVerdict.STRONGER,
            ComparisonVerdict.MISMATCH,
        }
    ]
    if rejected:
        return TranslationAssuranceSummary(
            status=AssuranceStatus.REJECTED,
            direct_reviews=direct,
            roundtrip_reviews=roundtrip,
            reason="at least one independent semantic reviewer found a translation mismatch",
        )

    incomplete = [
        review for review in all_reviews if review.verdict == ComparisonVerdict.INCOMPLETE
    ]
    if incomplete:
        return TranslationAssuranceSummary(
            status=AssuranceStatus.INCOMPLETE,
            direct_reviews=direct,
            roundtrip_reviews=roundtrip,
            reason="at least one independent semantic reviewer could not determine equivalence",
        )

    if len(direct) < min_direct_reviews or len(roundtrip) < min_roundtrip_reviews:
        return TranslationAssuranceSummary(
            status=AssuranceStatus.INCOMPLETE,
            direct_reviews=direct,
            roundtrip_reviews=roundtrip,
            reason=(
                f"insufficient completed reviews: direct={len(direct)}/{min_direct_reviews}, "
                f"roundtrip={len(roundtrip)}/{min_roundtrip_reviews}"
            ),
        )

    if all(review.verdict == ComparisonVerdict.EQUIVALENT for review in all_reviews):
        return TranslationAssuranceSummary(
            status=AssuranceStatus.TRUSTED,
            direct_reviews=direct,
            roundtrip_reviews=roundtrip,
            reason="all required independent reviewers judged the translation equivalent",
        )

    return TranslationAssuranceSummary(
        status=AssuranceStatus.INCOMPLETE,
        direct_reviews=direct,
        roundtrip_reviews=roundtrip,
        reason="review set did not satisfy a terminal assurance condition",
    )


def _comparison_focus(focus: str) -> str:
    if focus == "weakening":
        return (
            "Adversarial focus: look especially for omitted requirements, dropped guards, "
            "weakened cardinality, lost identity components, or existential witnesses that "
            "the symbolic/round-trip version no longer requires."
        )
    if focus == "strengthening":
        return (
            "Adversarial focus: look especially for stronger universal scope, missing applicability "
            "guards, extra equality/cardinality requirements, or guarantees accidentally extended "
            "outside the original boundary."
        )
    if focus == "balanced":
        return "Adversarial focus: search symmetrically for both weakening and strengthening."
    raise TranslationReviewError(f"unknown comparison focus {focus!r}")


def _parse_subject_signature(value: Any, where: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise TranslationReviewError(f"{where} must be a sha256 hex string")
    return value


def _parse_yaml_object(text: str, what: str) -> dict[str, Any]:
    text = _strip_writer_completion_sentinel(text)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TranslationReviewError(f"invalid YAML in {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise TranslationReviewError(f"{what} must be exactly one YAML mapping")
    return data



_WRITER_COMPLETION_RE = re.compile(r"^WRITERSUBAGENTCOMPLETE[A-Z0-9]+$")


def _strip_writer_completion_sentinel(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not _WRITER_COMPLETION_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines).strip()


def _contract_class_interpretation(
    contract_class: str, observation_scope: str | None
) -> str:
    if contract_class == "state_invariant":
        return (
            "A state_invariant must hold at every state inside its observation_scope. "
            "Therefore any transition whose resulting state remains in that scope may not "
            "produce a post-state that violates the invariant. A lifecycle-preservation "
            "sentence that merely restates continued truth of the same invariant across "
            "in-scope states need not be represented as a separate transition predicate."
        )
    if contract_class == "provenance_binding":
        return (
            "A provenance_binding constrains the identity/provenance relation between its "
            "source and bound symbols for every modeled occurrence covered by the formula. "
            "It does not imply liveness, delivery, ordering, or additional temporal behavior "
            "unless those are explicitly encoded."
        )
    if contract_class == "safety_contract":
        return (
            "A safety_contract forbids the explicitly described bad transition/state pattern "
            "inside its applicability boundary. It does not imply progress or eventual delivery "
            "unless the canonical contract says so."
        )
    if contract_class == "equivalence_relation":
        return (
            "An equivalence_relation states the canonical semantic equality criterion between the modeled "
            "representations/frontiers named by the contract. It does not imply reachability or liveness."
        )
    return (
        "No additional Harness-level semantic interpretation is defined for this contract class."
    )

def _formula_function_names(node: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        if set(node) == {"call"} and isinstance(node["call"], dict):
            fn = node["call"].get("fn")
            if isinstance(fn, str):
                names.add(fn)
        for value in node.values():
            names.update(_formula_function_names(value))
    elif isinstance(node, list):
        for value in node:
            names.update(_formula_function_names(value))
    return names

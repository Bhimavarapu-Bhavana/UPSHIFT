"""Benchmark-specific verification inputs for the profile-label migration.

All benchmark-specific expectations are read from the benchmark's own
metadata file. This module never hard-codes expected labels, and it never
accepts an arbitrary module path, file path, or command from a caller.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .verifier import (
    CandidateVerifier,
    UnknownCandidateError,
    VerificationCandidate,
    VerificationCase,
    VerificationReport,
)

__all__ = [
    "BENCHMARK_ROOT",
    "CandidateSpec",
    "KNOWN_CANDIDATES",
    "build_cases",
    "load_candidate",
    "load_metadata",
    "verify_candidate",
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = _REPOSITORY_ROOT / "demo" / "migration_benchmark"
METADATA_PATH = BENCHMARK_ROOT / "migration_metadata.json"


@dataclass(frozen=True)
class CandidateSpec:
    """A statically known candidate location inside the controlled benchmark."""

    candidate_id: str
    path: str
    module: str
    class_name: str
    method_name: str


KNOWN_CANDIDATES: Tuple[CandidateSpec, ...] = (
    CandidateSpec(
        candidate_id="old_baseline",
        path="demo/migration_benchmark/old/profile_service.py",
        module="demo.migration_benchmark.old.profile_service",
        class_name="ProfileLabelService",
        method_name="label_for",
    ),
    CandidateSpec(
        candidate_id="correct_migration",
        path="demo/migration_benchmark/candidates/correct_migration/profile_service.py",
        module=(
            "demo.migration_benchmark.candidates.correct_migration.profile_service"
        ),
        class_name="ProfileLabelService",
        method_name="label_for",
    ),
    CandidateSpec(
        candidate_id="regression_migration",
        path=(
            "demo/migration_benchmark/candidates/regression_migration/"
            "profile_service.py"
        ),
        module=(
            "demo.migration_benchmark.candidates.regression_migration.profile_service"
        ),
        class_name="ProfileLabelService",
        method_name="label_for",
    ),
)

_KNOWN_CANDIDATES: Mapping[str, CandidateSpec] = {
    spec.candidate_id: spec for spec in KNOWN_CANDIDATES
}


def load_metadata() -> Dict[str, Any]:
    """Load the benchmark metadata that defines the verification cases."""

    return json.loads(METADATA_PATH.read_text(encoding="utf-8"))


def build_cases(metadata: Optional[Mapping[str, Any]] = None) -> Tuple[VerificationCase, ...]:
    """Build ordered verification cases from the benchmark's preserved behavior."""

    data = dict(load_metadata() if metadata is None else metadata)
    preserved = data.get("preserved_behavior") or ()
    if not isinstance(preserved, Sequence):
        raise ValueError("preserved_behavior must be a sequence of behavior records")

    cases = []
    for record in preserved:
        if not isinstance(record, Mapping):
            raise ValueError("each preserved_behavior record must be a mapping")
        user_id = record.get("user_id")
        expected = record.get("expected_label")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("preserved_behavior entry is missing user_id")
        if not isinstance(expected, str):
            raise ValueError("preserved_behavior entry is missing expected_label")
        cases.append(
            VerificationCase(
                case_id=user_id,
                description=f"Profile label for {user_id}",
                inputs=(user_id,),
                expected=expected,
                required=True,
            )
        )
    return tuple(cases)


def load_candidate(candidate_id: str) -> VerificationCandidate:
    """Load one statically known benchmark candidate as a narrow callable."""

    try:
        spec = _KNOWN_CANDIDATES[candidate_id]
    except KeyError:
        known = ", ".join(sorted(_KNOWN_CANDIDATES))
        raise UnknownCandidateError(
            f"unknown candidate {candidate_id!r}; known candidates: {known}"
        ) from None

    module = importlib.import_module(spec.module)
    factory = getattr(module, spec.class_name)
    method: Callable[..., Any] = getattr(factory(), spec.method_name)

    def resolve(*inputs: Any) -> Any:
        return method(*inputs)

    resolve.__name__ = spec.method_name
    return VerificationCandidate(
        candidate_id=spec.candidate_id,
        path=spec.path,
        resolve=resolve,
    )


def verify_candidate(
    candidate_id: str,
    *,
    cases: Optional[Sequence[VerificationCase]] = None,
    migration_name: Optional[str] = None,
) -> VerificationReport:
    """Verify a known benchmark candidate against the benchmark's own cases."""

    metadata = load_metadata()
    resolved_cases = tuple(build_cases(metadata) if cases is None else cases)
    name = migration_name or str(metadata.get("benchmark") or "unknown-migration")
    candidate = load_candidate(candidate_id)
    return CandidateVerifier().verify(
        candidate, resolved_cases, migration_name=name
    )

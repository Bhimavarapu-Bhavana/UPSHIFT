"""Deterministic, explainable risk analysis based on impact evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Optional, Tuple

from .impact_analyzer import ImpactReport, MigrationDescription


LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"

DIRECT_OLD_API_USAGE = "direct_old_api_usage"
RENAMED_SYMBOLS = "renamed_symbols"
TEST_IMPACT = "test_impact"
MULTIPLE_IMPACTED_FILES = "multiple_impacted_files"
MIXED_API_STATE = "mixed_api_state"

FACTOR_ORDER = (
    DIRECT_OLD_API_USAGE,
    RENAMED_SYMBOLS,
    TEST_IMPACT,
    MULTIPLE_IMPACTED_FILES,
    MIXED_API_STATE,
)

__all__ = [
    "DIRECT_OLD_API_USAGE",
    "FACTOR_ORDER",
    "HIGH",
    "LOW",
    "MEDIUM",
    "MIXED_API_STATE",
    "MULTIPLE_IMPACTED_FILES",
    "RENAMED_SYMBOLS",
    "RiskAnalyzer",
    "RiskFactor",
    "RiskReport",
    "TEST_IMPACT",
]


@dataclass(frozen=True)
class RiskFactor:
    """One explainable contribution to the deterministic risk score."""

    category: str
    severity: str
    score: int
    evidence: str
    reason: str
    affected_paths: Tuple[str, ...] = ()
    affected_symbols: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskReport:
    """Deterministic risk summary derived from an existing impact report."""

    migration_name: str
    old_api: str
    target_api: str
    risk_level: str
    risk_score: int
    risk_factors: Tuple[RiskFactor, ...]
    impacted_file_count: int
    direct_old_reference_count: int
    target_reference_count: int
    metadata_listed_file_count: int
    affected_test_files: Tuple[str, ...]
    renamed_symbol_count: int
    explanations: Tuple[str, ...]


class RiskAnalyzer:
    """Read-only risk analyzer that consumes Phase 4 impact evidence."""

    def analyze(
        self,
        migration: MigrationDescription,
        impact_report: ImpactReport,
    ) -> RiskReport:
        """Return an explainable risk report without scanning or modifying files."""

        if not isinstance(migration, MigrationDescription):
            raise TypeError("migration must be a MigrationDescription")
        if not isinstance(impact_report, ImpactReport):
            raise TypeError("impact_report must be an ImpactReport")

        impacted_paths = tuple(impact.path for impact in impact_report.impacted_files)
        direct_count = len(impact_report.direct_references)
        target_count = len(impact_report.target_references)
        metadata_count = len(impact_report.metadata_listed_files)
        renamed_symbols = migration.renamed_symbols
        renamed_count = len(renamed_symbols)

        old_paths = tuple(
            sorted({reference.path for reference in impact_report.direct_references})
        )
        target_paths = tuple(
            sorted({reference.path for reference in impact_report.target_references})
        )
        renamed_names = tuple(
            sorted({name for pair in renamed_symbols for name in pair})
        )
        renamed_names_set = set(renamed_names)
        renamed_paths = tuple(
            sorted(
                {
                    reference.path
                    for reference in (
                        impact_report.direct_references + impact_report.target_references
                    )
                    if reference.symbol in renamed_names_set
                }
            )
        )
        test_files = self._affected_test_files(impacted_paths, impact_report)

        factors = []
        if direct_count:
            score = direct_count * 2
            factors.append(
                self._factor(
                    category=DIRECT_OLD_API_USAGE,
                    score=score,
                    evidence=self._count_evidence(direct_count, "direct OLD API reference"),
                    reason="Direct OLD API references can require call-site updates.",
                    affected_paths=old_paths,
                    affected_symbols=tuple(
                        sorted(
                            {
                                reference.symbol
                                for reference in impact_report.direct_references
                            }
                        )
                    ),
                )
            )

        if renamed_count:
            score = renamed_count * 3
            factors.append(
                self._factor(
                    category=RENAMED_SYMBOLS,
                    score=score,
                    evidence=self._count_evidence(renamed_count, "renamed field or symbol"),
                    reason="Renamed symbols can change caller expectations.",
                    affected_paths=renamed_paths,
                    affected_symbols=renamed_names,
                )
            )

        if test_files:
            score = len(test_files) * 2
            factors.append(
                self._factor(
                    category=TEST_IMPACT,
                    score=score,
                    evidence=self._count_evidence(len(test_files), "affected test file"),
                    reason="Affected tests provide behavior that must be preserved and verified.",
                    affected_paths=test_files,
                )
            )

        if len(impacted_paths) > 1:
            score = len(impacted_paths) - 1
            factors.append(
                self._factor(
                    category=MULTIPLE_IMPACTED_FILES,
                    score=score,
                    evidence=f"{len(impacted_paths)} files are potentially impacted.",
                    reason="Multiple impacted files increase coordination and review scope.",
                    affected_paths=tuple(sorted(impacted_paths)),
                )
            )

        if direct_count and target_count:
            mixed_paths = tuple(sorted(set(old_paths) | set(target_paths)))
            factors.append(
                self._factor(
                    category=MIXED_API_STATE,
                    score=2,
                    evidence="Both OLD and TARGET API references are present.",
                    reason=(
                        "Mixed references indicate a partially migrated compatibility "
                        "state; this is not an automatic regression finding."
                    ),
                    affected_paths=mixed_paths,
                )
            )

        risk_score = sum(factor.score for factor in factors)
        risk_level = self._risk_level(risk_score)
        explanations = tuple(
            f"{factor.evidence} {factor.reason}" for factor in factors
        ) or ("No impact evidence was available for this migration.",)

        return RiskReport(
            migration_name=migration.name,
            old_api=impact_report.old_api,
            target_api=impact_report.target_api,
            risk_level=risk_level,
            risk_score=risk_score,
            risk_factors=tuple(factors),
            impacted_file_count=len(impacted_paths),
            direct_old_reference_count=direct_count,
            target_reference_count=target_count,
            metadata_listed_file_count=metadata_count,
            affected_test_files=test_files,
            renamed_symbol_count=renamed_count,
            explanations=explanations,
        )

    @staticmethod
    def _factor(
        category: str,
        score: int,
        evidence: str,
        reason: str,
        affected_paths: Tuple[str, ...] = (),
        affected_symbols: Tuple[str, ...] = (),
    ) -> RiskFactor:
        return RiskFactor(
            category=category,
            severity=RiskAnalyzer._severity_for_score(score),
            score=score,
            evidence=evidence,
            reason=reason,
            affected_paths=affected_paths,
            affected_symbols=affected_symbols,
        )

    @staticmethod
    def _severity_for_score(score: int) -> str:
        if score < 6:
            return LOW
        if score < 12:
            return MEDIUM
        return HIGH

    @staticmethod
    def _risk_level(score: int) -> str:
        return RiskAnalyzer._severity_for_score(score)

    @staticmethod
    def _count_evidence(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'} detected."

    @staticmethod
    def _is_test_path(path: str) -> bool:
        parsed = PurePosixPath(path)
        filename = parsed.name
        return (
            "tests" in parsed.parts
            or "test" in parsed.parts
            or filename.startswith("test_")
            or filename.endswith("_test.py")
            or filename == "conftest.py"
        )

    @classmethod
    def _affected_test_files(
        cls,
        impacted_paths: Iterable[str],
        impact_report: ImpactReport,
    ) -> Tuple[str, ...]:
        candidates = set(impacted_paths) | set(impact_report.metadata_listed_files)
        return tuple(sorted(path for path in candidates if cls._is_test_path(path)))

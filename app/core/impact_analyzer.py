"""Deterministic, read-only impact analysis for UPSHIFT migrations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike]

EXCLUDED_DIRECTORY_NAMES: FrozenSet[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "secrets",
    }
)

TEXT_SUFFIXES: FrozenSet[str] = frozenset(
    {".cfg", ".ini", ".json", ".md", ".py", ".pyi", ".toml", ".txt", ".yaml", ".yml"}
)

__all__ = [
    "ImpactAnalyzer",
    "ImpactReport",
    "FileImpact",
    "MigrationDescription",
    "SymbolReference",
    "UnsafeRepositoryPathError",
]


class UnsafeRepositoryPathError(ValueError):
    """Raised when a supplied path resolves outside the repository root."""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _symbol_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    if value is None:
        return ()

    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError as error:
            raise ValueError(f"{field_name} must be a string collection") from error

    symbols = []
    for item in values:
        symbols.append(_required_text(item, field_name))
    return tuple(symbols)


def _path_text(value: Any, field_name: str) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise ValueError(f"{field_name} must be a path") from error

    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ValueError(f"{field_name} must be a non-empty path")
    return raw


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _entry_point_symbols(entry_point: str) -> Tuple[str, ...]:
    call_target = entry_point.split("(", 1)[0].strip()
    return tuple(part.strip() for part in call_target.split(".") if part.strip())


@dataclass(frozen=True)
class MigrationDescription:
    """Symbols and explicit file hints describing one migration."""

    name: str
    old_api: str
    target_api: str
    old_symbols: Tuple[str, ...] = ()
    target_symbols: Tuple[str, ...] = ()
    renamed_symbols: Tuple[Tuple[str, str], ...] = ()
    affected_files: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        object.__setattr__(self, "old_api", _required_text(self.old_api, "old_api"))
        object.__setattr__(self, "target_api", _required_text(self.target_api, "target_api"))

        old_symbols = _symbol_tuple(self.old_symbols, "old_symbols")
        target_symbols = _symbol_tuple(self.target_symbols, "target_symbols")
        object.__setattr__(self, "old_symbols", tuple(sorted(set(old_symbols))))
        object.__setattr__(self, "target_symbols", tuple(sorted(set(target_symbols))))

        renamed_symbols = []
        for pair in self.renamed_symbols:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError("renamed_symbols must contain (old, target) pairs")
            renamed_symbols.append(
                (
                    _required_text(pair[0], "renamed old symbol"),
                    _required_text(pair[1], "renamed target symbol"),
                )
            )
        object.__setattr__(
            self, "renamed_symbols", tuple(sorted(set(renamed_symbols)))
        )

        affected_files = []
        for path in self.affected_files:
            affected_files.append(_path_text(path, "affected_files entry"))
        object.__setattr__(self, "affected_files", tuple(sorted(set(affected_files))))

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any],
        name: Optional[str] = None,
    ) -> "MigrationDescription":
        """Build a focused description from the benchmark metadata shape."""

        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping")

        old_contract = _mapping(metadata.get("old_contract"), "old_contract")
        target_contract = _mapping(metadata.get("target_contract"), "target_contract")
        old_api = _required_text(old_contract.get("entry_point"), "old_contract.entry_point")
        target_api = _required_text(
            target_contract.get("entry_point"), "target_contract.entry_point"
        )
        old_fields = _symbol_tuple(old_contract.get("fields"), "old_contract.fields")
        target_fields = _symbol_tuple(
            target_contract.get("fields"), "target_contract.fields"
        )

        affected_files = list(_symbol_tuple(metadata.get("affected_files"), "affected_files"))
        expected_migration = metadata.get("expected_migration")
        if isinstance(expected_migration, Mapping):
            for key in ("affected_files", "affected_source_files", "affected_tests"):
                affected_files.extend(
                    _symbol_tuple(expected_migration.get(key), f"expected_migration.{key}")
                )

        migration_name = name
        if migration_name is None:
            migration_name = metadata.get("benchmark") or metadata.get("name")

        return cls(
            name=_required_text(migration_name, "migration name"),
            old_api=old_api,
            target_api=target_api,
            old_symbols=_entry_point_symbols(old_api) + old_fields,
            target_symbols=_entry_point_symbols(target_api) + target_fields,
            renamed_symbols=tuple(zip(old_fields, target_fields)),
            affected_files=tuple(affected_files),
        )


@dataclass(frozen=True)
class SymbolReference:
    """A textual symbol match with a stable repository-relative location."""

    path: str
    symbol: str
    line: int
    column: int


@dataclass(frozen=True)
class FileImpact:
    """Structured impact evidence for one repository file."""

    path: str
    reasons: Tuple[str, ...]
    metadata_listed: bool
    old_symbols: Tuple[str, ...]
    target_symbols: Tuple[str, ...]


@dataclass(frozen=True)
class ImpactReport:
    """Deterministic impact report for one migration description."""

    migration_name: str
    old_api: str
    target_api: str
    old_symbols: Tuple[str, ...]
    target_symbols: Tuple[str, ...]
    renamed_symbols: Tuple[Tuple[str, str], ...]
    impacted_files: Tuple[FileImpact, ...]
    direct_references: Tuple[SymbolReference, ...]
    target_references: Tuple[SymbolReference, ...]
    metadata_listed_files: Tuple[str, ...]


class ImpactAnalyzer:
    """Read-only, deterministic textual impact analyzer."""

    def __init__(self, repository_root: PathLike) -> None:
        root = Path(repository_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("repository_root must be an existing directory")
        self.repository_root = root

    def analyze(self, migration: MigrationDescription) -> ImpactReport:
        """Return potentially impacted files without executing or modifying code."""

        if not isinstance(migration, MigrationDescription):
            raise TypeError("migration must be a MigrationDescription")

        metadata_paths = {
            self._normalize_relative_path(path) for path in migration.affected_files
        }
        old_by_path = {}
        target_by_path = {}

        for path, text in self._iter_text_files():
            old_references = self._find_references(path, text, migration.old_symbols)
            target_references = self._find_references(path, text, migration.target_symbols)
            if old_references:
                old_by_path[path] = old_references
            if target_references:
                target_by_path[path] = target_references

        impacted_paths = set(metadata_paths) | set(old_by_path) | set(target_by_path)
        impacted_files = []
        for path in sorted(impacted_paths):
            old_references = old_by_path.get(path, ())
            target_references = target_by_path.get(path, ())
            metadata_listed = path in metadata_paths
            impacted_files.append(
                FileImpact(
                    path=path,
                    reasons=self._reasons(
                        path=path,
                        old_references=old_references,
                        target_references=target_references,
                        metadata_listed=metadata_listed,
                        renamed_symbols=migration.renamed_symbols,
                    ),
                    metadata_listed=metadata_listed,
                    old_symbols=tuple(sorted({ref.symbol for ref in old_references})),
                    target_symbols=tuple(sorted({ref.symbol for ref in target_references})),
                )
            )

        direct_references = self._flatten_references(old_by_path)
        target_references = self._flatten_references(target_by_path)
        return ImpactReport(
            migration_name=migration.name,
            old_api=migration.old_api,
            target_api=migration.target_api,
            old_symbols=migration.old_symbols,
            target_symbols=migration.target_symbols,
            renamed_symbols=migration.renamed_symbols,
            impacted_files=tuple(impacted_files),
            direct_references=direct_references,
            target_references=target_references,
            metadata_listed_files=tuple(sorted(metadata_paths)),
        )

    def _normalize_relative_path(self, candidate: PathLike) -> str:
        raw = _path_text(candidate, "repository path")
        path = Path(raw)
        if not path.is_absolute():
            path = self.repository_root / path

        try:
            resolved = path.resolve()
            relative = resolved.relative_to(self.repository_root)
        except (OSError, ValueError) as error:
            raise UnsafeRepositoryPathError(
                f"path escapes repository root: {raw!r}"
            ) from error

        if not relative.parts:
            raise UnsafeRepositoryPathError("path must identify a file inside the repository")
        return relative.as_posix()

    @staticmethod
    def _is_excluded_directory(name: str) -> bool:
        return name in EXCLUDED_DIRECTORY_NAMES or name.endswith(".egg-info")

    @staticmethod
    def _is_excluded_file(name: str) -> bool:
        lowered = name.lower()
        return (
            lowered == ".env"
            or lowered.startswith(".env.")
            or lowered.endswith((".pyc", ".pyo", ".pyd"))
            or lowered.endswith(".log")
        )

    def _iter_text_files(self) -> Iterable[Tuple[str, str]]:
        for directory, directory_names, file_names in os.walk(
            self.repository_root, topdown=True, followlinks=False
        ):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not self._is_excluded_directory(name)
            )
            for name in sorted(file_names):
                if self._is_excluded_file(name):
                    continue

                path = Path(directory) / name
                try:
                    resolved = path.resolve()
                    relative = resolved.relative_to(self.repository_root)
                except (OSError, ValueError):
                    continue

                if not resolved.is_file() or resolved.suffix.lower() not in TEXT_SUFFIXES:
                    continue

                try:
                    text = resolved.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                yield relative.as_posix(), text

    @staticmethod
    def _find_references(
        path: str, text: str, symbols: Sequence[str]
    ) -> Tuple[SymbolReference, ...]:
        if not symbols:
            return ()

        patterns = [
            (symbol, re.compile(rf"(?<!\w){re.escape(symbol)}(?!\w)"))
            for symbol in sorted(symbols)
        ]
        references = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            for symbol, pattern in patterns:
                for match in pattern.finditer(line):
                    references.append(
                        SymbolReference(
                            path=path,
                            symbol=symbol,
                            line=line_number,
                            column=match.start() + 1,
                        )
                    )
        return tuple(
            sorted(references, key=lambda reference: (reference.line, reference.column, reference.symbol))
        )

    @staticmethod
    def _flatten_references(
        references_by_path: Mapping[str, Tuple[SymbolReference, ...]]
    ) -> Tuple[SymbolReference, ...]:
        return tuple(
            reference
            for path in sorted(references_by_path)
            for reference in references_by_path[path]
        )

    @staticmethod
    def _reasons(
        path: str,
        old_references: Tuple[SymbolReference, ...],
        target_references: Tuple[SymbolReference, ...],
        metadata_listed: bool,
        renamed_symbols: Tuple[Tuple[str, str], ...],
    ) -> Tuple[str, ...]:
        old_to_new = dict(renamed_symbols)
        new_to_old = {new: old for old, new in renamed_symbols}
        reasons = []

        for reference in old_references:
            renamed_to = old_to_new.get(reference.symbol)
            if renamed_to:
                reasons.append(
                    "potentially impacted: OLD reference "
                    f"{reference.symbol!r} (renamed to {renamed_to!r}) "
                    f"at line {reference.line}"
                )
            else:
                reasons.append(
                    "potentially impacted: OLD reference "
                    f"{reference.symbol!r} at line {reference.line}"
                )

        for reference in target_references:
            renamed_from = new_to_old.get(reference.symbol)
            if renamed_from:
                reasons.append(
                    "potentially impacted: TARGET reference "
                    f"{reference.symbol!r} (renamed from {renamed_from!r}) "
                    f"at line {reference.line}"
                )
            else:
                reasons.append(
                    "potentially impacted: TARGET reference "
                    f"{reference.symbol!r} at line {reference.line}"
                )

        if metadata_listed:
            reasons.append(
                "potentially impacted: file is listed in migration metadata as affected"
            )

        return tuple(sorted(set(reasons)))

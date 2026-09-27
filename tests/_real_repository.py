"""The one real temporary repository every Phase 12-15 test builds on.

A single fixture, shared by all four phases, so the pass path and the fail path
run against *the same* repository shape and a difference in outcome can only
come from the migration, not from the fixture.

It is a genuine directory on the local filesystem with genuine files, and the
executor changes those files for real. Nothing here is a mock, an in-memory
stand-in, or benchmark data.

The repository deliberately contains three things that must never be reachable:

    .env                  a credential-shaped file
    config/credentials.py a name matching the sensitive-name rule
    vendor/notes.md       inside an excluded directory

The bounded reader is expected to refuse all three, and a test asserts that a
plan cannot name any of them, which is what makes "the executor cannot reach
outside the read set" a measured property rather than a claim.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Tuple

__all__ = [
    "EXCLUDED_DIRECTORY",
    "FORBIDDEN_LABELS",
    "MIGRATION",
    "OLD_MODULE",
    "OLD_SERVICE_PATH",
    "OTHER_CONSUMER_PATH",
    "TARGET_MODULE",
    "RealRepositoryFixture",
    "build_real_repository",
    "complete_plan",
    "incomplete_plan",
]

#: The module being migrated, and its replacement.
OLD_MODULE = "legacy_profile"
TARGET_MODULE = "profile_directory"

#: The two files that reference the old module.
OLD_SERVICE_PATH = "legacy_pkg/service.py"
OTHER_CONSUMER_PATH = "legacy_pkg/consumer.py"

#: A directory the bounded reader excludes by name, verified against
#: ``app.repository.real_input.EXCLUDED_DIRECTORY_NAMES`` by a test below.
EXCLUDED_DIRECTORY = "secrets"

#: Labels a plan must never be able to target. Each is really present on disk
#: and really refused by the bounded reader, which is asserted by a test rather
#: than assumed.
FORBIDDEN_LABELS: Tuple[str, ...] = (
    ".env",
    "config/credentials.py",
    f"{EXCLUDED_DIRECTORY}/notes.md",
    f"{EXCLUDED_DIRECTORY}/api_key.txt",
)

#: The exact text each of the two referencing files contains.
_SERVICE_TEMPLATE = '''"""Service layer for the profile resolve_profile."""

from legacy_profile import resolve_profile


def describe(name):
    """Return the profile label for a name."""
    return resolve_profile(name)
'''

_CONSUMER_TEMPLATE = '''"""Consumer of the profile resolve_profile."""

import legacy_profile


def label(name):
    """Return the profile label for a name."""
    return legacy_profile.resolve_profile(name)
'''

_README = """# Real repository fixture

A real directory used by the UPSHIFT migration executor tests.
"""

#: The declaration. Identifier-only, validated by the existing boundary code.
MIGRATION = {
    "name": "profile_service_lookup",
    "old_api": "legacy_profile.resolve_profile",
    "target_api": "profile_directory.resolve_profile",
    "old_symbols": ["legacy_profile.resolve_profile"],
    "target_symbols": ["profile_directory.resolve_profile"],
}


#: The import block each referencing file carries. consumer.py is rewritten as a
#: whole block because it references the old module twice - once on the import
#: line and once at the call site - and a plan may touch a file only once, so a
#: partial rewrite could not be expressed.
_CONSUMER_IMPORT_AND_BODY = """import legacy_profile


def label(name):
    \"\"\"Return the profile label for a name.\"\"\"
    return legacy_profile.resolve_profile(name)
"""

_CONSUMER_MIGRATED = _CONSUMER_IMPORT_AND_BODY.replace(OLD_MODULE, TARGET_MODULE)


def complete_plan() -> Tuple[dict, ...]:
    """A plan that migrates **every** reference, so verification must pass.

    Both files are rewritten so that no admitted file mentions the old module
    anywhere. The repository-wide reference count therefore reaches zero, and
    the existing Phase 6 verifier reports PASS from the real files.

    Returns:
        Two operations, one per referencing file.
    """

    return (
        {
            "operation": "replace_exact",
            "path": OLD_SERVICE_PATH,
            "old_content": f"from {OLD_MODULE} import resolve_profile",
            "new_content": f"from {TARGET_MODULE} import resolve_profile",
            "expected_occurrences": 1,
        },
        {
            "operation": "replace_exact",
            "path": OTHER_CONSUMER_PATH,
            "old_content": _CONSUMER_IMPORT_AND_BODY,
            "new_content": _CONSUMER_MIGRATED,
            "expected_occurrences": 1,
        },
    )


def incomplete_plan() -> Tuple[dict, ...]:
    """A plan that migrates only one of the two references.

    This is the intentionally failing migration of Phase 14. The executor
    applies it without complaint, because each operation's own precondition
    holds. Verification then fails for a real reason: the untouched file still
    references the old module, so the repository-wide reference count is not
    zero. The failure is discovered, never scripted.
    """

    return (
        {
            "operation": "replace_exact",
            "path": OLD_SERVICE_PATH,
            "old_content": "from legacy_profile import resolve_profile",
            "new_content": "from profile_directory import resolve_profile",
            "expected_occurrences": 1,
        },
    )


class RealRepositoryFixture:
    """One real repository on disk, plus the content the tests expect.

    Attributes:
        root: The repository directory.
        service_text: Original content of :data:`OLD_SERVICE_PATH`.
        consumer_text: Original content of :data:`OTHER_CONSUMER_PATH`.
        migrated_service_text: ``service_text`` after the plan is applied.
        migrated_consumer_text: ``consumer_text`` after the plan is applied.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.service_text = _SERVICE_TEMPLATE
        self.consumer_text = _CONSUMER_TEMPLATE
        self.migrated_service_text = _SERVICE_TEMPLATE.replace(OLD_MODULE, TARGET_MODULE)
        self.migrated_consumer_text = _CONSUMER_TEMPLATE.replace(OLD_MODULE, TARGET_MODULE)
    @property
    def allowed_roots(self) -> Tuple[str, ...]:
        """The operator approval this repository needs."""

        return (str(self.root),)

    def read(self, relative_path: str) -> str:
        """Read a repository file, for asserting a real change landed."""

        return (self.root / Path(*relative_path.split("/"))).read_text(
            encoding="utf-8", newline=""
        )

    def exists(self, relative_path: str) -> bool:
        """True when a repository-relative file exists."""

        return (self.root / Path(*relative_path.split("/"))).is_file()

    def close(self) -> None:
        """Delete the repository."""

        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "RealRepositoryFixture":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_real_repository() -> RealRepositoryFixture:
    """Create the real repository described in this module's docstring.

    Returns:
        A :class:`RealRepositoryFixture` whose directory the caller owns and
        must close.
    """

    root = Path(tempfile.mkdtemp(prefix="upshift-real-repository-"))
    (root / "legacy_pkg").mkdir()
    (root / "legacy_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "legacy_pkg" / "service.py").write_text(
        _SERVICE_TEMPLATE, encoding="utf-8", newline=""
    )
    (root / "legacy_pkg" / "consumer.py").write_text(
        _CONSUMER_TEMPLATE, encoding="utf-8", newline=""
    )
    (root / "README.md").write_text(_README, encoding="utf-8", newline="")

    # Present on disk and genuinely refused by the bounded reader, so the tests
    # can prove the executor cannot reach them rather than assuming it.
    (root / ".env").write_text("SECRET=hunter2\n", encoding="utf-8", newline="")
    (root / "config").mkdir()
    (root / "config" / "credentials.py").write_text(
        "PASSWORD = 'nope'\n", encoding="utf-8", newline=""
    )
    (root / EXCLUDED_DIRECTORY).mkdir()
    (root / EXCLUDED_DIRECTORY / "notes.md").write_text(
        "excluded\n", encoding="utf-8", newline=""
    )
    (root / EXCLUDED_DIRECTORY / "api_key.txt").write_text(
        "excluded\n", encoding="utf-8", newline=""
    )
    return RealRepositoryFixture(root)

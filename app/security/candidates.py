"""Controlled-candidate allowlist for UPSHIFT presentation surfaces.

Phase 10.1 security boundary enforced here:

* The only caller-supplied value that can reach an UPSHIFT engine is a
  candidate identifier, and it must match the static benchmark allowlist
  exactly. There is no parameter for a filesystem path, a module path, a URL, or
  a command anywhere in the dashboard request contract.
* Path-like and command-like values are refused explicitly and separately from
  unknown identifiers, so a traversal or shell-injection attempt fails loudly
  instead of being silently coerced into a default.
* No shell, subprocess, dynamic import, or network access happens in this
  module.
* Nothing is written; no repository file and no Git state is modified.

This module validates identity only. It performs no analysis and holds no
migration state.
"""

from __future__ import annotations

import re
from typing import Tuple

from app.verification.profile_label_benchmark import KNOWN_CANDIDATES
from app.verification.verifier import UnknownCandidateError

__all__ = [
    "CONTROLLED_CANDIDATE_IDS",
    "PROHIBITED_DASHBOARD_CAPABILITIES",
    "UnknownCandidateError",
    "is_controlled_candidate",
    "known_candidate_ids",
    "looks_like_path_or_command",
    "require_controlled_candidate",
]

#: The only candidate identifiers a presentation surface may ask about.
CONTROLLED_CANDIDATE_IDS: Tuple[str, ...] = tuple(
    spec.candidate_id for spec in KNOWN_CANDIDATES
)

PROHIBITED_DASHBOARD_CAPABILITIES: Tuple[str, ...] = (
    "arbitrary shell execution",
    "arbitrary python execution",
    "arbitrary subprocess execution",
    "arbitrary filesystem path input",
    "arbitrary repository selection",
    "arbitrary git commands",
    "git reset or force operations",
    "credential or secret collection",
    "network requests",
    "arbitrary MCP tool invocation",
    "automatic approval bypass",
    "automatic migration acceptance",
    "unrestricted file writes",
    # Real-repository mode reads a repository, so the two entries above are
    # stated precisely here: selection is bounded, never arbitrary, and the
    # content that is read is never run.
    "repository selection outside an operator-allowed boundary",
    "execution of repository code",
)

# A controlled identifier is a short lower_snake_case token. Anything carrying a
# separator, a shell metacharacter, whitespace, or a drive letter fails here
# before the allowlist is consulted.
_CONTROLLED_IDENTIFIER = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")

_PATH_OR_COMMAND_CHARACTERS: Tuple[str, ...] = (
    "/",
    "\\",
    ";",
    "|",
    "&",
    "$",
    "`",
    "<",
    ">",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    '"',
    "'",
    "*",
    "?",
    "!",
    "#",
    "~",
    "^",
    "=",
    ",",
    "\n",
    "\r",
    "\t",
    "\x00",
    " ",
)

_CONTROLLED_CANDIDATE_SET = frozenset(CONTROLLED_CANDIDATE_IDS)


def known_candidate_ids() -> Tuple[str, ...]:
    """Return the statically allowlisted candidate identifiers."""

    return CONTROLLED_CANDIDATE_IDS


def looks_like_path_or_command(value: object) -> bool:
    """Report whether ``value`` carries filesystem-path or shell syntax.

    This is a defensive pre-check. A controlled identifier can never contain
    these characters, so a positive result means the caller supplied a path, a
    command, or an injection attempt rather than a candidate name.
    """

    if not isinstance(value, str):
        return True
    if not value or not value.strip():
        return True
    if any(character in value for character in _PATH_OR_COMMAND_CHARACTERS):
        return True
    return not _CONTROLLED_IDENTIFIER.match(value)


def is_controlled_candidate(value: object) -> bool:
    """Report whether ``value`` is an allowlisted candidate identifier."""

    if looks_like_path_or_command(value):
        return False
    return value in _CONTROLLED_CANDIDATE_SET


def require_controlled_candidate(value: object) -> str:
    """Return the validated candidate identifier or refuse the request.

    Args:
        value: The raw candidate identifier supplied by the caller.

    Returns:
        The validated identifier.

    Raises:
        UnknownCandidateError: If ``value`` is not a string, is empty, carries
            path or command syntax, or is simply not allowlisted. The message
            never echoes a filesystem path or command back to the caller.
    """

    known = ", ".join(CONTROLLED_CANDIDATE_IDS)

    if not isinstance(value, str):
        raise UnknownCandidateError(
            f"candidate_id must be a string; known candidates: {known}"
        )

    if looks_like_path_or_command(value):
        raise UnknownCandidateError(
            "candidate_id must be a plain controlled identifier, not a path, "
            f"URL, or command; known candidates: {known}"
        )

    if value not in _CONTROLLED_CANDIDATE_SET:
        raise UnknownCandidateError(
            f"unknown candidate; known candidates: {known}"
        )

    return value

"""Baseline application service using the legacy profile API."""

from typing import Optional

from .legacy_directory import LegacyDirectory


class ProfileLabelService:
    def __init__(self, directory: Optional[LegacyDirectory] = None) -> None:
        self._directory = directory if directory is not None else LegacyDirectory()

    def label_for(self, user_id: str) -> str:
        profile = self._directory.lookup(user_id)
        if profile is None:
            return "Unknown user"

        if profile.preferred_name:
            return profile.preferred_name

        full_name = " ".join(
            part.strip()
            for part in (profile.first_name, profile.last_name)
            if part.strip()
        )
        return full_name or "Unnamed user"

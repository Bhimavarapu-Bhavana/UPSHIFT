"""Reference service implementation using the target profile API."""

from typing import Optional

from .profile_directory import ProfileDirectory


class ProfileLabelService:
    def __init__(self, directory: Optional[ProfileDirectory] = None) -> None:
        self._directory = directory if directory is not None else ProfileDirectory()

    def label_for(self, user_id: str) -> str:
        profile = self._directory.get_profile(user_id)
        if profile is None:
            return "Unknown user"

        if profile.display_name:
            return profile.display_name

        full_name = " ".join(
            part.strip()
            for part in (profile.given_name, profile.family_name)
            if part.strip()
        )
        return full_name or "Unnamed user"

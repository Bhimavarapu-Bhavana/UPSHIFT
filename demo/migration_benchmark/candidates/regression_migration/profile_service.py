"""An intentionally incorrect migration that ignores the TARGET display name."""

from typing import Optional

from demo.migration_benchmark.target.profile_directory import ProfileDirectory


class ProfileLabelService:
    def __init__(self, directory: Optional[ProfileDirectory] = None) -> None:
        self._directory = directory if directory is not None else ProfileDirectory()

    def label_for(self, user_id: str) -> str:
        profile = self._directory.get_profile(user_id)
        if profile is None:
            return "Unknown user"

        full_name = " ".join(
            part.strip()
            for part in (profile.given_name, profile.family_name)
            if part.strip()
        )
        return full_name or "Unnamed user"

"""TARGET profile directory contract used by the reference fixture."""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Profile:
    given_name: str
    family_name: str
    display_name: Optional[str]


class ProfileDirectory:
    _profiles: Dict[str, Profile] = {
        "user-001": Profile("Ada", "Lovelace", None),
        "user-002": Profile("Grace", "Hopper", "Amazing Grace"),
        "user-003": Profile("", "Unknown", None),
        "empty-user": Profile("", "", None),
    }

    def get_profile(self, user_id: str) -> Optional[Profile]:
        return self._profiles.get(user_id)

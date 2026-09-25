"""Legacy profile API used by the baseline application."""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class LegacyProfile:
    first_name: str
    last_name: str
    preferred_name: Optional[str]


class LegacyDirectory:
    _profiles: Dict[str, LegacyProfile] = {
        "user-001": LegacyProfile("Ada", "Lovelace", None),
        "user-002": LegacyProfile("Grace", "Hopper", "Amazing Grace"),
        "user-003": LegacyProfile("", "Unknown", None),
        "empty-user": LegacyProfile("", "", None),
    }

    def lookup(self, user_id: str) -> Optional[LegacyProfile]:
        return self._profiles.get(user_id)

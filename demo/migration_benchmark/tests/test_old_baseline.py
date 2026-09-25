"""Baseline behavior tests for the OLD profile contract."""

import pytest

from demo.migration_benchmark.old.legacy_directory import LegacyDirectory
from demo.migration_benchmark.old.profile_service import ProfileLabelService


@pytest.mark.parametrize(
    ("user_id", "expected_label"),
    [
        ("user-001", "Ada Lovelace"),
        ("user-002", "Amazing Grace"),
        ("user-003", "Unknown"),
        ("empty-user", "Unnamed user"),
        ("missing-user", "Unknown user"),
    ],
)
def test_old_service_preserves_application_behavior(user_id, expected_label):
    assert ProfileLabelService().label_for(user_id) == expected_label


def test_old_directory_exposes_the_legacy_contract():
    profile = LegacyDirectory().lookup("user-002")

    assert profile is not None
    assert profile.first_name == "Grace"
    assert profile.last_name == "Hopper"
    assert profile.preferred_name == "Amazing Grace"

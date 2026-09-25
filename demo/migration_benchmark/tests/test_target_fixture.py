"""Behavior tests for the TARGET reference fixture."""

import pytest

from demo.migration_benchmark.target.profile_directory import ProfileDirectory
from demo.migration_benchmark.target.profile_service import ProfileLabelService


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
def test_target_fixture_preserves_application_behavior(user_id, expected_label):
    assert ProfileLabelService().label_for(user_id) == expected_label


def test_target_directory_exposes_the_target_contract():
    profile = ProfileDirectory().get_profile("user-002")

    assert profile is not None
    assert profile.given_name == "Grace"
    assert profile.family_name == "Hopper"
    assert profile.display_name == "Amazing Grace"

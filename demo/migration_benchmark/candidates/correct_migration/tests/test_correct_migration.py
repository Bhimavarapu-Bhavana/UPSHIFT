"""Behavior tests for the correct migration candidate."""

import pytest

from demo.migration_benchmark.candidates.correct_migration.profile_service import (
    ProfileLabelService,
)


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
def test_correct_migration_preserves_all_behavior_cases(user_id, expected_label):
    assert ProfileLabelService().label_for(user_id) == expected_label

"""Opt-in regression evidence for the TARGET display-name contract."""

import os

import pytest

from demo.migration_benchmark.candidates.regression_migration.profile_service import (
    ProfileLabelService,
)


@pytest.mark.skipif(
    os.environ.get("UPSHIFT_RUN_REGRESSION_DEMO") != "1",
    reason="Set UPSHIFT_RUN_REGRESSION_DEMO=1 to demonstrate the expected failure.",
)
def test_regression_candidate_preserves_display_name_contract():
    actual = ProfileLabelService().label_for("user-002")

    assert actual == "Amazing Grace", (
        "Compatibility invariant violated: expected 'Amazing Grace' but the "
        f"regression candidate returned {actual!r} because it ignored display_name."
    )

"""Smoke tests for the initial UPSHIFT scaffold."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "app",
        "app.api",
        "app.core",
        "app.security",
        "app.verification",
        "mcp",
    ],
)
def test_scaffold_packages_are_importable(module_name):
    assert importlib.import_module(module_name)

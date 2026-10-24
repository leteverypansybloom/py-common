"""Tests of packaging and test-suite hygiene."""

import subprocess
import sys
from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement


def _requirement(name: str, extra: str | None = None) -> Requirement:
    """Find one of py-common's declared requirements.

    Args:
        name: The distribution name, in lower case.
        extra: The extra it is declared under, or None for the core
            dependencies.

    Returns:
        The requirement.
    """
    for raw in requires("py-common") or []:
        req = Requirement(raw)
        in_extra = req.marker is not None and "extra" in str(req.marker)
        wanted = extra is not None and f'extra == "{extra}"' in str(req.marker)
        if req.name.lower() == name and (
            wanted or (extra is None and not in_extra)
        ):
            return req
    raise AssertionError(f"py-common doesn't require {name} ({extra})")


class TestDependencyFloors:
    """The package can't install XML-unsafe or known-vulnerable deps."""

    def test_defusedxml_is_a_runtime_dependency(self) -> None:
        """Openpyxl only guards against XML entity attacks with it."""
        assert _requirement("defusedxml")

    @pytest.mark.parametrize("extra", ["sharepoint", "dev"])
    def test_requests_floor_excludes_advisories(self, extra: str) -> None:
        """Requests 2.32.4, which has advisories, can't be installed."""
        spec = _requirement("requests", extra).specifier
        assert not spec.contains("2.32.4")
        assert spec.contains("2.33.0")


def test_live_tests_are_all_marked_integration() -> None:
    """The unit selection never collects a test that calls GCP."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not integration",
            "tests/integration",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 5, done.stdout + done.stderr  # none collected

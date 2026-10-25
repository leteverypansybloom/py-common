"""Tests of packaging and test-suite hygiene."""

import importlib
import re
import subprocess
import sys
from importlib.metadata import requires
from pathlib import Path

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


#: The repository root, holding the Dockerfiles.
ROOT = Path(__file__).resolve().parents[2]


def _image_imports(dockerfile: str) -> list[str]:
    """List the py-common modules a Dockerfile's build check imports.

    Args:
        dockerfile: The Dockerfile's text.

    Returns:
        Each module named after ``import`` in a ``python -c`` line.
    """
    modules = []
    for command in re.findall(r'python -c "([^"]*)"', dockerfile):
        for statement in command.split(";"):
            statement = statement.strip()
            if statement.startswith("import "):
                modules += [
                    name.strip()
                    for name in statement.removeprefix("import ").split(",")
                ]
    return [name for name in modules if name.split(".")[0] == "py_common"]


@pytest.mark.parametrize("name", ["Dockerfile"])
def test_image_build_check_imports_existing_modules(name: str) -> None:
    """The image's build-time import check names modules that exist.

    Args:
        name: The Dockerfile to read.
    """
    modules = _image_imports((ROOT / name).read_text(encoding="utf-8"))

    assert modules, f"{name} has no py-common import check"
    for module in modules:
        importlib.import_module(module)


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

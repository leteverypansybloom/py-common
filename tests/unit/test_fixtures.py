"""Unit tests for make_fixtures().

make_fixtures() writes the README that describes the fixtures, so
these tests keep that README in step with the files actually written
and with the copy committed under tests/data/fixtures/.

RAP Principles:
- Reproducible: Fixtures are regenerated into a temp folder each run
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; uses pytest's tmp_path fixture
"""

from pathlib import Path

import pytest

from py_common.fixtures import make_fixtures

COMMITTED_FIXTURES = Path(__file__).parents[1] / "data" / "fixtures"


class TestFixturesReadme:
    """Test the README.md that make_fixtures() writes."""

    @pytest.fixture
    def output_dir(self, tmp_path):
        """Fixture: folder with freshly generated fixtures."""
        make_fixtures(tmp_path)
        return tmp_path

    def test_readme_states_number_of_files_written(self, output_dir):
        """README's file count matches the .xlsx files written.

        Args:
            output_dir: Folder with generated fixtures

        Returns:
            None

        Raises:
            AssertionError: If the stated count is wrong
        """
        count = len(list(output_dir.glob("*.xlsx")))
        readme = (output_dir / "README.md").read_text()

        assert f"{count} small Excel files" in readme

    def test_readme_lists_every_file_written(self, output_dir):
        """Every .xlsx written has a row in the README table.

        Args:
            output_dir: Folder with generated fixtures

        Returns:
            None

        Raises:
            AssertionError: If a file is missing from the table
        """
        readme = (output_dir / "README.md").read_text()

        for path in output_dir.glob("*.xlsx"):
            assert f"| {path.name} |" in readme

    def test_committed_readme_matches_generated(self, output_dir):
        """The committed fixtures README is what make_fixtures() writes.

        Guards against the committed copy being hand-edited, or the
        generator changing without the fixtures being regenerated.

        Args:
            output_dir: Folder with generated fixtures

        Returns:
            None

        Raises:
            AssertionError: If the two READMEs differ
        """
        generated = (output_dir / "README.md").read_text()
        committed = (COMMITTED_FIXTURES / "README.md").read_text()

        assert committed.splitlines() == generated.splitlines()

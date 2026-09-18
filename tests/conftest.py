"""Pytest configuration and shared fixtures."""

import tempfile
from pathlib import Path

import pytest

from py_common.adapters import LocalObjectStore, LocalSource
from py_common.contract import Column, Contract, Worksheet
from py_common.fixtures import make_fixtures


@pytest.fixture
def temp_dir():
    """Temporary directory that is cleaned up after each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def source_folder(temp_dir):
    """Folder with test fixtures."""
    fixtures_dir = temp_dir / "fixtures"
    make_fixtures(fixtures_dir)
    return fixtures_dir


@pytest.fixture
def local_source(source_folder):
    """LocalSource reading from test fixtures."""
    return LocalSource(source_folder)


@pytest.fixture
def local_store(temp_dir):
    """LocalObjectStore for testing."""
    store_dir = temp_dir / "store"
    return LocalObjectStore(store_dir)


@pytest.fixture
def events_contract():
    """Contract for 'events' source (employer events)."""
    return Contract(
        key_columns=["event_id"],
        worksheets=[
            Worksheet(
                name="Events",
                columns=[
                    Column(
                        name="event_id",
                        data_type="string",
                        nullable=False,
                        unique=True,
                    ),
                    Column(
                        name="employer_id",
                        data_type="string",
                        nullable=False,
                    ),
                    Column(
                        name="attendees",
                        data_type="integer",
                        nullable=False,
                    ),
                ],
            )
        ],
    )


@pytest.fixture
def was_contract():
    """Contract for 'was' source (workplace assessment summary)."""
    return Contract(
        key_columns=["was_id"],
        worksheets=[
            Worksheet(
                name="Results",
                columns=[
                    Column(
                        name="was_id",
                        data_type="string",
                        nullable=False,
                        unique=True,
                    ),
                    Column(
                        name="employer_id",
                        data_type="string",
                        nullable=False,
                    ),
                    Column(
                        name="score",
                        data_type="integer",
                        nullable=True,
                    ),
                ],
            )
        ],
    )

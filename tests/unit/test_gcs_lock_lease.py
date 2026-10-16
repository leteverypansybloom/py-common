"""GCSObjectStore.lock is a lease (review H6).

A killed run no longer blocks every later run forever: its lock
records when it expires, and a later run may take over an expired
lock, atomically, by its generation. A lock that hasn't expired is
never taken.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from _fake_gcs import FakeBucket

from py_common.adapters.gcs import GCSObjectStore

KEY = "locks/abc"
T0 = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def bucket() -> FakeBucket:
    return FakeBucket()


def _store(bucket: FakeBucket, lease_seconds: int = 3600) -> GCSObjectStore:
    with patch("py_common.adapters.gcs.storage.Client"):
        store = GCSObjectStore("p", "store", lease_seconds=lease_seconds)
    store.bucket = bucket
    return store


@pytest.fixture
def clock() -> Iterator[list[datetime]]:
    """The time GCSObjectStore sees; set now[0] to move it."""
    now = [T0]
    with patch("py_common.adapters.gcs._utcnow", lambda: now[0]):
        yield now


def _holder(bucket: FakeBucket) -> dict[str, str]:
    return dict(json.loads(bucket.objects[KEY][0]))


def _leave_lock(bucket: FakeBucket, store: GCSObjectStore) -> None:
    """A run that took the lock at T0 and died without releasing it.

    Written directly, as the store writes it, rather than by entering
    lock() and never leaving: an abandoned generator would release
    the lock when it is garbage-collected.
    """
    expires = T0 + timedelta(seconds=store.lease_seconds)
    bucket.blob(KEY).upload_from_string(
        json.dumps(
            {
                "run_id": "dead-run",
                "acquired_at": T0.isoformat(),
                "expires_at": expires.isoformat(),
            }
        ),
        if_generation_match=0,
    )


def test_lock_records_its_expiry(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    with _store(bucket, lease_seconds=900).lock(KEY):
        holder = _holder(bucket)

    assert holder["acquired_at"] == T0.isoformat()
    assert holder["expires_at"] == (T0 + timedelta(seconds=900)).isoformat()


def test_unexpired_lock_is_never_taken(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    _leave_lock(bucket, _store(bucket))
    clock[0] = T0 + timedelta(seconds=3599)

    with pytest.raises(RuntimeError, match="held by another run"):
        with _store(bucket).lock(KEY):
            pass
    assert _holder(bucket)["acquired_at"] == T0.isoformat()


def test_expired_lock_is_taken_over_and_logged(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    _leave_lock(bucket, _store(bucket))
    dead = _holder(bucket)
    clock[0] = T0 + timedelta(seconds=3601)

    with patch("py_common.adapters.gcs.logger") as logger:
        with _store(bucket).lock(KEY):
            assert _holder(bucket)["run_id"] != dead["run_id"]

    assert KEY not in bucket.objects  # released normally afterwards
    [warning] = logger.warning.call_args_list
    assert "Took over expired lock" in warning.args[0]
    assert dead["run_id"] in str(warning.args)


def test_takeover_race_has_one_winner(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    """Two runs find the same expired lock; only one takes it."""
    _leave_lock(bucket, _store(bucket))
    clock[0] = T0 + timedelta(hours=2)
    winner = _store(bucket)
    loser = _store(bucket)
    held = winner.lock(KEY)
    # The winner's takeover lands between the loser reading the
    # expired lock and writing its own.
    bucket.before_takeover = held.__enter__

    with pytest.raises(RuntimeError, match="took it over first"):
        with loser.lock(KEY):
            pass

    winner_id = _holder(bucket)["run_id"]
    held.__exit__(None, None, None)
    assert KEY not in bucket.objects
    assert winner_id


def test_lock_without_expiry_uses_acquired_at_plus_lease(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    """Locks written by 0.1.x have no expires_at."""
    bucket.blob(KEY).upload_from_string(
        json.dumps({"run_id": "old", "acquired_at": T0.isoformat()}),
        if_generation_match=0,
    )
    store = _store(bucket)

    clock[0] = T0 + timedelta(minutes=30)
    with pytest.raises(RuntimeError):
        with store.lock(KEY):
            pass

    clock[0] = T0 + timedelta(hours=2)
    with store.lock(KEY):
        assert _holder(bucket)["run_id"] != "old"


def test_unreadable_lock_is_never_taken(
    bucket: FakeBucket, clock: list[datetime]
) -> None:
    bucket.blob(KEY).upload_from_string(b"not json", if_generation_match=0)
    clock[0] = T0 + timedelta(days=30)

    with pytest.raises(RuntimeError, match="held by another run"):
        with _store(bucket).lock(KEY):
            pass


def test_lease_must_be_positive() -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        _store(FakeBucket(), lease_seconds=0)


class TestNaiveLeaseTimes:
    """A lock with a time-zone-less time is never taken over."""

    @pytest.mark.parametrize(
        "holder",
        [
            '{"run_id": "x", "expires_at": "2026-01-01T00:00:00"}',
            '{"run_id": "x", "acquired_at": "2026-01-01T00:00:00"}',
        ],
    )
    def test_refused_not_type_error(self, holder: str) -> None:
        bucket = FakeBucket()
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "store")
        store.bucket = bucket
        bucket.blob("locks/k").upload_from_string(
            holder, if_generation_match=0
        )

        with pytest.raises(RuntimeError, match="held by another run"):
            with store.lock("locks/k"):
                pass

        assert bucket.objects["locks/k"][0].decode() == holder

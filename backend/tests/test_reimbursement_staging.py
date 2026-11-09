from __future__ import annotations

import hashlib
import io
import os
import selectors
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from app.services.reimbursement_staging import (
    InvalidStorageKey,
    ReimbursementStaging,
    StagingArea,
    StagingIntegrityError,
    StagingLayoutError,
    StagingLimitExceeded,
    StagingObjectExists,
    StagingObjectNotFound,
    StagingRecoveryBusy,
    StagingReservation,
)

_MAX_BYTES = 2 * 1024 * 1024


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def object_path(root: Path, storage_key: str) -> Path:
    return root.joinpath(*storage_key.split("/"))


def new_staging(root: Path, *, maximum: int = _MAX_BYTES) -> ReimbursementStaging:
    return ReimbursementStaging(root.resolve(), max_object_bytes=maximum)


def test_recovery_directory_guard_shares_writers_but_excludes_cleanup(tmp_path):
    staging = new_staging(tmp_path / "staging")
    other = new_staging(staging.root)
    owner = str(uuid4())
    with staging.recovery_guard(owner), other.recovery_guard(owner):
        with pytest.raises(StagingRecoveryBusy):
            with other.recovery_guard(owner, exclusive=True):
                pytest.fail("cleanup must not overlap recovery writers")
    with staging.recovery_guard(owner, exclusive=True):
        with pytest.raises(StagingRecoveryBusy):
            with other.recovery_guard(owner):
                pytest.fail("recovery must not start during cleanup")
    with other.recovery_guard(owner, exclusive=True):
        pass
    assert list((staging.root / "drafts" / owner).iterdir()) == []


def test_recovery_directory_guard_is_released_when_writer_process_dies(tmp_path):
    staging = new_staging(tmp_path / "staging")
    owner = str(uuid4())
    program = """
import sys
from pathlib import Path
from app.services.reimbursement_staging import ReimbursementStaging
staging = ReimbursementStaging(Path(sys.argv[1]), max_object_bytes=1024)
with staging.recovery_guard(sys.argv[2]):
    print('ready', flush=True)
    sys.stdin.read()
"""
    with subprocess.Popen(
        [sys.executable, "-c", program, str(staging.root), owner],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=5)
                assert process.stdout.readline().strip() == "ready"
            with pytest.raises(StagingRecoveryBusy):
                with staging.recovery_guard(owner, exclusive=True):
                    pytest.fail("another process still owns the recovery directory")
        finally:
            process.kill()
            process.wait(timeout=5)
    with staging.recovery_guard(owner, exclusive=True):
        pass


def test_reservation_is_known_before_write_and_round_trips_privately(tmp_path: Path) -> None:
    root = tmp_path / "persistent-staging"
    staging = new_staging(root)
    owner_id = str(uuid4())
    content = b"persistent reimbursement bytes"
    digest = sha256(content)
    source = staging.new_reservation(
        StagingArea.DRAFTS,
        owner_id,
        "pdf",
        reserved_bytes=len(content),
    )
    destination = staging.new_reservation(
        StagingArea.GENERATED,
        owner_id,
        "xlsx",
        reserved_bytes=len(content),
    )

    assert source.part_storage_key.endswith(".part")
    assert source.storage_key.split("/")[:2] == source.part_storage_key.split("/")[:2]
    stored = staging.write_stream(
        source,
        io.BytesIO(content),
        expected_size=len(content),
        expected_sha256=digest,
    )
    copied = staging.copy(
        source.storage_key,
        destination,
        expected_size=len(content),
        expected_sha256=digest,
    )

    assert stored.size_bytes == copied.size_bytes == len(content)
    assert stored.sha256 == copied.sha256 == digest
    assert (
        staging.read_bytes(
            destination.storage_key,
            expected_size=len(content),
            expected_sha256=digest,
        )
        == content
    )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(object_path(root, source.storage_key).stat().st_mode) == 0o600
    assert staging.delete(
        source.storage_key,
        expected_size=len(content),
        expected_sha256=digest,
    )
    assert not staging.delete(
        source.storage_key,
        expected_size=len(content),
        expected_sha256=digest,
        missing_ok=True,
    )


def test_integrity_and_quota_failures_leave_no_final_or_partial_file(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    content = b"123456"

    cases = [
        (len(content) + 1, len(content) + 1, sha256(content), StagingIntegrityError),
        (len(content), len(content), "0" * 64, StagingIntegrityError),
        (len(content) - 1, None, None, StagingLimitExceeded),
    ]
    for reserved, expected_size, expected_hash, expected_error in cases:
        staging = ReimbursementStaging(root, max_object_bytes=len(content) + 1)
        reservation = staging.new_reservation(
            StagingArea.DRAFTS,
            str(uuid4()),
            "png",
            reserved_bytes=reserved,
        )
        with pytest.raises(expected_error):
            staging.write_bytes(
                reservation,
                content,
                expected_size=expected_size,
                expected_sha256=expected_hash,
            )
        final_path = object_path(root, reservation.storage_key)
        assert not final_path.exists()
        if final_path.parent.exists():
            assert not list(final_path.parent.glob("*.part"))


def test_object_limit_is_required_and_reservation_cannot_exceed_it(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    with pytest.raises(TypeError):
        ReimbursementStaging(root)  # type: ignore[call-arg]
    with pytest.raises(StagingLimitExceeded):
        ReimbursementStaging(root, max_object_bytes=0)

    staging = ReimbursementStaging(root, max_object_bytes=5)
    with pytest.raises(StagingLimitExceeded):
        staging.new_reservation(
            StagingArea.DRAFTS,
            str(uuid4()),
            "pdf",
            reserved_bytes=6,
        )


def test_existing_object_is_never_overwritten(tmp_path: Path) -> None:
    staging = new_staging(tmp_path / "staging")
    first = staging.new_reservation(
        StagingArea.DRAFTS,
        str(uuid4()),
        "jpg",
        reserved_bytes=6,
    )
    staging.write_bytes(first, b"first")
    retry = staging.reserve_existing_key(first.storage_key, reserved_bytes=6)

    with pytest.raises(StagingObjectExists):
        staging.write_bytes(retry, b"second")
    assert (
        staging.read_bytes(
            first.storage_key,
            expected_size=5,
            expected_sha256=sha256(b"first"),
        )
        == b"first"
    )


class _BarrierStream(io.BytesIO):
    def __init__(self, content: bytes, barrier: Barrier) -> None:
        super().__init__(content)
        self._barrier = barrier
        self._waited = False

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        if not chunk and not self._waited:
            self._waited = True
            self._barrier.wait(timeout=5)
        return chunk


def test_concurrent_writers_to_same_key_never_overwrite(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    staging = ReimbursementStaging(root, max_object_bytes=32)
    first = staging.new_reservation(
        StagingArea.GENERATED,
        str(uuid4()),
        "xlsx",
        reserved_bytes=32,
    )
    second = staging.reserve_existing_key(first.storage_key, reserved_bytes=32)
    barrier = Barrier(2)
    payloads = (b"first-complete-payload", b"second-complete-payload")

    def write(reservation: StagingReservation, content: bytes):
        return staging.write_stream(reservation, _BarrierStream(content, barrier))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(write, reservation, payload)
            for reservation, payload in zip((first, second), payloads, strict=True)
        ]
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except Exception as exc:  # noqa: BLE001 - the exact loser is asserted below
                outcomes.append(exc)

    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], StagingObjectExists)
    persisted = object_path(root, first.storage_key).read_bytes()
    assert persisted in payloads
    assert not list(object_path(root, first.storage_key).parent.glob("*.part"))


@pytest.mark.parametrize(
    "storage_key",
    [
        "../outside/file.pdf",
        "/absolute/path.pdf",
        "drafts/not-a-uuid/file.pdf",
        "drafts/11111111-1111-4111-8111-111111111111/../../outside.pdf",
        "drafts/11111111-1111-4111-8111-111111111111/not-a-uuid.pdf",
        "drafts/11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222.exe",
        "drafts\\11111111-1111-4111-8111-111111111111\\file.pdf",
    ],
)
def test_invalid_or_traversing_storage_keys_are_rejected(
    tmp_path: Path,
    storage_key: str,
) -> None:
    staging = new_staging(tmp_path / "staging")
    with pytest.raises(InvalidStorageKey):
        staging.reserve_existing_key(storage_key, reserved_bytes=7)


def test_reservation_rejects_a_part_key_for_another_object(tmp_path: Path) -> None:
    staging = new_staging(tmp_path / "staging")
    first = staging.new_reservation(
        StagingArea.DRAFTS,
        str(uuid4()),
        "pdf",
        reserved_bytes=7,
    )
    second = staging.new_reservation(
        StagingArea.DRAFTS,
        str(uuid4()),
        "pdf",
        reserved_bytes=7,
    )
    mismatched = StagingReservation(first.storage_key, second.part_storage_key, 7)

    with pytest.raises(InvalidStorageKey):
        staging.write_bytes(mismatched, b"content")


def test_symlink_ancestors_owner_and_object_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StagingLayoutError):
        ReimbursementStaging(
            root_link.absolute(),
            max_object_bytes=_MAX_BYTES,
        ).prepare()

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(StagingLayoutError):
        ReimbursementStaging(
            parent_link.absolute() / "staging",
            max_object_bytes=_MAX_BYTES,
        ).prepare()

    root = (tmp_path / "staging").resolve()
    staging = ReimbursementStaging(root, max_object_bytes=_MAX_BYTES)
    staging.prepare()
    owner_id = str(uuid4())
    owner_path = root / "drafts" / owner_id
    owner_path.symlink_to(outside, target_is_directory=True)
    reservation = staging.new_reservation(
        StagingArea.DRAFTS,
        owner_id,
        "pdf",
        reserved_bytes=7,
    )
    with pytest.raises(StagingLayoutError):
        staging.write_bytes(reservation, b"content")
    owner_path.unlink()

    stored = staging.write_bytes(reservation, b"content")
    path = object_path(root, reservation.storage_key)
    path.unlink()
    target = outside / "target.pdf"
    target.write_bytes(b"outside")
    path.symlink_to(target)
    with pytest.raises(StagingLayoutError):
        staging.read_bytes(
            reservation.storage_key,
            expected_size=stored.size_bytes,
            expected_sha256=stored.sha256,
        )
    assert target.read_bytes() == b"outside"


def test_delete_rejects_tampering(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    staging = ReimbursementStaging(root, max_object_bytes=_MAX_BYTES)
    reservation = staging.new_reservation(
        StagingArea.DRAFTS,
        str(uuid4()),
        "pdf",
        reserved_bytes=8,
    )
    stored = staging.write_bytes(reservation, b"expected")
    object_path(root, reservation.storage_key).write_bytes(b"tampered")

    with pytest.raises(StagingIntegrityError):
        staging.delete(
            reservation.storage_key,
            expected_size=stored.size_bytes,
            expected_sha256=stored.sha256,
        )
    assert object_path(root, reservation.storage_key).exists()


def test_cleanup_only_removes_old_unowned_regular_partial_files(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    staging = ReimbursementStaging(root, max_object_bytes=_MAX_BYTES)
    owner_id = str(uuid4())
    reservation = staging.new_reservation(
        StagingArea.DRAFTS,
        owner_id,
        "pdf",
        reserved_bytes=10,
    )
    staging.write_bytes(reservation, b"keep-final")
    owner_path = object_path(root, reservation.storage_key).parent
    old_unowned = owner_path / f".{uuid4()}.{uuid4()}.part"
    old_owned = owner_path / f".{uuid4()}.{uuid4()}.part"
    recent = owner_path / f".{uuid4()}.{uuid4()}.part"
    unrelated = owner_path / "ordinary.tmp"
    symlink_part = owner_path / f".{uuid4()}.{uuid4()}.part"
    outside = tmp_path / "outside-part"
    outside.write_bytes(b"outside")
    for path in (old_unowned, old_owned, recent, unrelated):
        path.write_bytes(path.name.encode())
    symlink_part.symlink_to(outside)
    old_time = datetime.now(UTC) - timedelta(hours=2)
    for path in (old_unowned, old_owned, unrelated, symlink_part):
        os.utime(path, (old_time.timestamp(), old_time.timestamp()), follow_symlinks=False)
    owned_key = f"drafts/{owner_id}/{old_owned.name}"

    removed = staging.cleanup_unowned_parts(
        owned_part_keys={owned_key},
        older_than=datetime.now(UTC) - timedelta(hours=1),
    )

    assert removed == 1
    assert not old_unowned.exists()
    assert old_owned.exists()
    assert recent.exists()
    assert unrelated.exists()
    assert symlink_part.is_symlink()
    assert outside.read_bytes() == b"outside"
    assert object_path(root, reservation.storage_key).exists()


def test_owned_partial_can_be_deleted_exactly(tmp_path: Path) -> None:
    root = (tmp_path / "staging").resolve()
    staging = ReimbursementStaging(root, max_object_bytes=_MAX_BYTES)
    reservation = staging.new_reservation(
        StagingArea.DRAFTS,
        str(uuid4()),
        "pdf",
        reserved_bytes=10,
    )
    final_path = object_path(root, reservation.storage_key)
    final_path.parent.mkdir(parents=True)
    part_path = object_path(root, reservation.part_storage_key)
    part_path.write_bytes(b"partial")

    assert staging.delete_reserved_part(reservation)
    assert not part_path.exists()
    assert not staging.delete_reserved_part(reservation, missing_ok=True)


def test_missing_object_has_stable_error(tmp_path: Path) -> None:
    staging = new_staging(tmp_path / "staging")
    key = staging.new_storage_key(StagingArea.GENERATED, str(uuid4()), "xlsx")
    with pytest.raises(StagingObjectNotFound):
        staging.read_bytes(key, expected_size=1, expected_sha256="0" * 64)


def test_staging_root_rejects_lexical_escape_to_filesystem_root(monkeypatch) -> None:
    fchmod_calls = 0

    def record_fchmod(_descriptor: int, _mode: int) -> None:
        nonlocal fchmod_calls
        fchmod_calls += 1

    monkeypatch.setattr(os, "fchmod", record_fchmod)
    with pytest.raises(StagingLayoutError):
        ReimbursementStaging(Path("/tmp/.."), max_object_bytes=1)

    assert fchmod_calls == 0

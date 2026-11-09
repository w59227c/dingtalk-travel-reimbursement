from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import re
import stat
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

_CHUNK_SIZE = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_ALLOWED_EXTENSIONS = frozenset({"jpg", "png", "pdf", "xlsx"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_PART_NAME_PATTERN = re.compile(rf"^\.({_UUID_PATTERN})\.({_UUID_PATTERN})\.part$")


class StagingArea(StrEnum):
    DRAFTS = "drafts"
    GENERATED = "generated"


class ReimbursementStagingError(RuntimeError):
    """Base class for safe, caller-facing staging failures."""


class InvalidStorageKey(ReimbursementStagingError):
    pass


class StagingLayoutError(ReimbursementStagingError):
    pass


class StagingObjectNotFound(ReimbursementStagingError):
    pass


class StagingObjectExists(ReimbursementStagingError):
    pass


class StagingRecoveryBusy(ReimbursementStagingError):
    pass


class StagingIntegrityError(ReimbursementStagingError):
    pass


class StagingLimitExceeded(ReimbursementStagingError):
    pass


@dataclass(frozen=True, slots=True)
class StagingReservation:
    """A persistable reservation made before any bytes are accepted.

    The state service stores all three values while atomically reserving quota.
    A crashed write is therefore distinguishable from an unowned temporary file.
    """

    storage_key: str
    part_storage_key: str
    reserved_bytes: int


@dataclass(frozen=True, slots=True)
class StagedObject:
    storage_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ParsedStorageKey:
    area: StagingArea
    owner_id: str
    object_id: str
    object_name: str
    extension: str


@dataclass(frozen=True, slots=True)
class _ParsedPartKey:
    area: StagingArea
    owner_id: str
    object_id: str
    part_name: str


def _canonical_uuid(value: str) -> str:
    try:
        normalized = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidStorageKey("storage key contains an invalid identifier") from exc
    if normalized != value:
        raise InvalidStorageKey("storage key identifiers must use canonical lowercase UUIDs")
    return normalized


def _normalized_extension(value: str) -> str:
    normalized = value.lower().lstrip(".")
    if normalized != value.lower().strip().lstrip(".") or normalized not in _ALLOWED_EXTENSIONS:
        raise InvalidStorageKey("storage key contains an unsupported extension")
    return normalized


def _normalized_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise StagingIntegrityError("expected SHA-256 is invalid")
    return normalized


def _parse_storage_key(storage_key: str) -> _ParsedStorageKey:
    if not isinstance(storage_key, str) or "\\" in storage_key:
        raise InvalidStorageKey("storage key is invalid")
    parts = storage_key.split("/")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidStorageKey("storage key must have exactly three path segments")
    try:
        area = StagingArea(parts[0])
    except ValueError as exc:
        raise InvalidStorageKey("storage key area is invalid") from exc
    owner_id = _canonical_uuid(parts[1])
    object_stem, separator, extension = parts[2].rpartition(".")
    if not separator:
        raise InvalidStorageKey("storage key extension is missing")
    object_id = _canonical_uuid(object_stem)
    normalized_extension = _normalized_extension(extension)
    return _ParsedStorageKey(
        area=area,
        owner_id=owner_id,
        object_id=object_id,
        object_name=f"{object_id}.{normalized_extension}",
        extension=normalized_extension,
    )


def _parse_part_key(part_key: str) -> _ParsedPartKey:
    if not isinstance(part_key, str) or "\\" in part_key:
        raise InvalidStorageKey("partial storage key is invalid")
    parts = part_key.split("/")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidStorageKey("partial storage key must have exactly three path segments")
    try:
        area = StagingArea(parts[0])
    except ValueError as exc:
        raise InvalidStorageKey("partial storage key area is invalid") from exc
    owner_id = _canonical_uuid(parts[1])
    match = _PART_NAME_PATTERN.fullmatch(parts[2])
    if match is None:
        raise InvalidStorageKey("partial storage key filename is invalid")
    return _ParsedPartKey(
        area=area,
        owner_id=owner_id,
        object_id=_canonical_uuid(match.group(1)),
        part_name=parts[2],
    )


class ReimbursementStaging:
    """Private persistent store addressed only by generated storage keys.

    ``max_object_bytes`` is mandatory, so no call site can accidentally accept
    an unbounded stream. Quota allocation remains a database transaction:
    callers persist :class:`StagingReservation` first, then pass it here.
    """

    def __init__(self, root: Path, *, max_object_bytes: int) -> None:
        normalized_root = Path(os.path.normpath(str(root)))
        if not root.is_absolute() or normalized_root != root:
            raise StagingLayoutError("staging root must be an absolute path")
        if normalized_root == Path(normalized_root.anchor):
            raise StagingLayoutError("staging root must not be a filesystem root")
        if max_object_bytes < 1:
            raise StagingLimitExceeded("maximum object size must be positive")
        if _NOFOLLOW == 0:
            raise StagingLayoutError("this platform cannot enforce no-follow staging paths")
        self._root = normalized_root
        self._max_object_bytes = max_object_bytes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_object_bytes(self) -> int:
        return self._max_object_bytes

    def prepare(self) -> None:
        root_fd = self._open_root(create=True)
        try:
            for area in StagingArea:
                area_fd = self._open_child_directory(root_fd, area.value, create=True)
                os.close(area_fd)
        finally:
            os.close(root_fd)

    @contextmanager
    def recovery_guard(self, draft_id: str, *, exclusive: bool = False) -> Iterator[None]:
        """Fence recovery writers against deletion without holding a database lock.

        The private owner directory is stable and contains no extra lock file.
        Independent writers may share it; deletion must acquire it exclusively.
        Closing the descriptor (including process death) releases the kernel lock.
        """
        owner_fd = self._open_owner_directory_by_id(
            StagingArea.DRAFTS, _canonical_uuid(draft_id), create=True
        )
        try:
            try:
                fcntl.flock(
                    owner_fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise StagingRecoveryBusy("recovery writer or cleanup is active") from None
            except OSError as exc:
                raise StagingLayoutError("recovery directory could not be locked safely") from exc
            yield
        finally:
            os.close(owner_fd)

    def new_storage_key(
        self,
        area: StagingArea,
        owner_id: str,
        extension: str,
    ) -> str:
        normalized_owner = _canonical_uuid(owner_id)
        normalized_extension = _normalized_extension(extension)
        return f"{area.value}/{normalized_owner}/{uuid4()}.{normalized_extension}"

    def new_reservation(
        self,
        area: StagingArea,
        owner_id: str,
        extension: str,
        *,
        reserved_bytes: int,
    ) -> StagingReservation:
        return self.reserve_existing_key(
            self.new_storage_key(area, owner_id, extension),
            reserved_bytes=reserved_bytes,
        )

    def reserve_existing_key(
        self,
        storage_key: str,
        *,
        reserved_bytes: int,
    ) -> StagingReservation:
        """Create a persistable write attempt for a preallocated final key."""

        parsed = _parse_storage_key(storage_key)
        self._validate_reserved_bytes(reserved_bytes)
        part_name = f".{parsed.object_id}.{uuid4()}.part"
        return StagingReservation(
            storage_key=storage_key,
            part_storage_key=f"{parsed.area.value}/{parsed.owner_id}/{part_name}",
            reserved_bytes=reserved_bytes,
        )

    def write_bytes(
        self,
        reservation: StagingReservation,
        content: bytes,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedObject:
        return self.write_stream(
            reservation,
            io.BytesIO(content),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def write_stream(
        self,
        reservation: StagingReservation,
        source: BinaryIO,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedObject:
        parsed, parsed_part = self._validate_reservation(reservation)
        normalized_hash = (
            _normalized_sha256(expected_sha256) if expected_sha256 is not None else None
        )
        if expected_size is not None:
            if expected_size < 1:
                raise StagingIntegrityError("expected size must be positive")
            if expected_size > reservation.reserved_bytes:
                raise StagingLimitExceeded("expected size exceeds the reserved quota")

        owner_fd = self._open_owner_directory(parsed, create=True)
        partial_created = False
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    parsed_part.part_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=owner_fd,
                )
                partial_created = True
                try:
                    os.fchmod(descriptor, 0o600)
                except BaseException:
                    os.close(descriptor)
                    descriptor = None
                    raise
            except FileExistsError as exc:
                raise StagingObjectExists("staging write attempt already exists") from exc
            except OSError as exc:
                raise StagingLayoutError("partial staging object is not safely writable") from exc

            digest = hashlib.sha256()
            size_bytes = 0
            assert descriptor is not None
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ReimbursementStagingError("staging source must return bytes")
                    size_bytes += len(chunk)
                    if size_bytes > reservation.reserved_bytes:
                        raise StagingLimitExceeded("staging object exceeds the reserved quota")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            actual_hash = digest.hexdigest()
            if size_bytes < 1:
                raise StagingIntegrityError("staging object must not be empty")
            if expected_size is not None and size_bytes != expected_size:
                raise StagingIntegrityError("staging object size does not match")
            if normalized_hash is not None and actual_hash != normalized_hash:
                raise StagingIntegrityError("staging object SHA-256 does not match")

            try:
                os.link(
                    parsed_part.part_name,
                    parsed.object_name,
                    src_dir_fd=owner_fd,
                    dst_dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise StagingObjectExists("staging object already exists") from exc
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise StagingObjectExists("staging object already exists") from exc
                raise StagingLayoutError(
                    "staging object could not be installed atomically"
                ) from exc
            os.fsync(owner_fd)
            os.unlink(parsed_part.part_name, dir_fd=owner_fd)
            partial_created = False
            os.fsync(owner_fd)
            return StagedObject(
                storage_key=reservation.storage_key,
                size_bytes=size_bytes,
                sha256=actual_hash,
            )
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if partial_created:
                try:
                    os.unlink(parsed_part.part_name, dir_fd=owner_fd)
                    os.fsync(owner_fd)
                except FileNotFoundError:
                    pass
            # Never remove the final name. A successful hard-link is already a
            # complete authoritative object, even if a later fsync/unlink failed.
            raise
        finally:
            os.close(owner_fd)

    @contextmanager
    def open_verified(
        self,
        storage_key: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> Iterator[BinaryIO]:
        parsed = _parse_storage_key(storage_key)
        expected_hash = _normalized_sha256(expected_sha256)
        owner_fd = self._open_owner_directory(parsed, create=False)
        try:
            descriptor = self._open_regular_file(owner_fd, parsed.object_name)
        finally:
            os.close(owner_fd)
        stream = os.fdopen(descriptor, "rb")
        try:
            self._verify_stream(stream, expected_size, expected_hash)
            stream.seek(0)
            yield stream
        finally:
            stream.close()

    def read_bytes(
        self,
        storage_key: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes:
        with self.open_verified(
            storage_key,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        ) as stream:
            return stream.read()

    def copy(
        self,
        source_key: str,
        destination: StagingReservation,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> StagedObject:
        with self.open_verified(
            source_key,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        ) as source:
            return self.write_stream(
                destination,
                source,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )

    def delete(
        self,
        storage_key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        missing_ok: bool = False,
    ) -> bool:
        parsed = _parse_storage_key(storage_key)
        expected_hash = _normalized_sha256(expected_sha256)
        try:
            owner_fd = self._open_owner_directory(parsed, create=False)
        except StagingObjectNotFound:
            if missing_ok:
                return False
            raise
        try:
            try:
                descriptor = self._open_regular_file(owner_fd, parsed.object_name)
            except StagingObjectNotFound:
                if missing_ok:
                    return False
                raise
            with os.fdopen(descriptor, "rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                self._verify_stream(stream, expected_size, expected_hash)
            try:
                current_stat = os.stat(
                    parsed.object_name,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise StagingObjectNotFound("staging object does not exist") from None
            if (
                not stat.S_ISREG(current_stat.st_mode)
                or current_stat.st_dev != opened_stat.st_dev
                or current_stat.st_ino != opened_stat.st_ino
            ):
                raise StagingIntegrityError("staging object changed during deletion")
            os.unlink(parsed.object_name, dir_fd=owner_fd)
            os.fsync(owner_fd)
            return True
        finally:
            os.close(owner_fd)

    def delete_reserved_part(
        self,
        reservation: StagingReservation,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """Delete exactly one database-owned partial write after state transition."""

        parsed, parsed_part = self._validate_reservation(reservation)
        try:
            owner_fd = self._open_owner_directory(parsed, create=False)
        except StagingObjectNotFound:
            if missing_ok:
                return False
            raise
        try:
            try:
                entry_stat = os.stat(
                    parsed_part.part_name,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise StagingObjectNotFound("partial staging object does not exist") from None
            if not stat.S_ISREG(entry_stat.st_mode):
                raise StagingLayoutError("partial staging object is not a regular file")
            os.unlink(parsed_part.part_name, dir_fd=owner_fd)
            os.fsync(owner_fd)
            return True
        finally:
            os.close(owner_fd)

    def discard_reservation(self, reservation: StagingReservation) -> int:
        """Delete every local name owned by an unfinalized reservation.

        The final name may already exist if a process died after the atomic
        hard-link but before its database finalize. Both names are generated,
        database-owned keys, so removing them together is the only safe way to
        release that reservation's quota.
        """

        parsed, parsed_part = self._validate_reservation(reservation)
        try:
            owner_fd = self._open_owner_directory(parsed, create=False)
        except StagingObjectNotFound:
            return 0
        removed = 0
        try:
            for name in (parsed_part.part_name, parsed.object_name):
                try:
                    entry_stat = os.stat(name, dir_fd=owner_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise StagingLayoutError("reserved staging object is not a regular file")
                os.unlink(name, dir_fd=owner_fd)
                removed += 1
            if removed:
                os.fsync(owner_fd)
            return removed
        finally:
            os.close(owner_fd)

    def cleanup_unowned_parts(
        self,
        *,
        owned_part_keys: Collection[str],
        older_than: datetime,
    ) -> int:
        """Remove only old regular ``.part`` files absent from ownership data."""

        normalized_owned = {
            f"{parsed.area.value}/{parsed.owner_id}/{parsed.part_name}"
            for parsed in (_parse_part_key(key) for key in owned_part_keys)
        }
        cutoff = older_than.replace(tzinfo=UTC) if older_than.tzinfo is None else older_than
        cutoff_timestamp = cutoff.timestamp()
        try:
            root_fd = self._open_root(create=False)
        except StagingObjectNotFound:
            return 0
        removed = 0
        try:
            for area in StagingArea:
                try:
                    area_fd = self._open_child_directory(root_fd, area.value, create=False)
                except StagingObjectNotFound:
                    continue
                try:
                    with os.scandir(area_fd) as owners:
                        owner_names = [entry.name for entry in owners]
                    for owner_name in owner_names:
                        try:
                            _canonical_uuid(owner_name)
                            owner_fd = self._open_child_directory(
                                area_fd,
                                owner_name,
                                create=False,
                            )
                        except (InvalidStorageKey, StagingLayoutError, StagingObjectNotFound):
                            continue
                        try:
                            with os.scandir(owner_fd) as entries:
                                candidates = [
                                    (entry.name, entry.stat(follow_symlinks=False))
                                    for entry in entries
                                    if _PART_NAME_PATTERN.fullmatch(entry.name) is not None
                                    and entry.is_file(follow_symlinks=False)
                                ]
                            directory_changed = False
                            for name, entry_stat in candidates:
                                part_key = f"{area.value}/{owner_name}/{name}"
                                if (
                                    part_key in normalized_owned
                                    or entry_stat.st_mtime > cutoff_timestamp
                                ):
                                    continue
                                try:
                                    os.unlink(name, dir_fd=owner_fd)
                                except FileNotFoundError:
                                    continue
                                directory_changed = True
                                removed += 1
                            if directory_changed:
                                os.fsync(owner_fd)
                        finally:
                            os.close(owner_fd)
                finally:
                    os.close(area_fd)
        finally:
            os.close(root_fd)
        return removed

    def _validate_reserved_bytes(self, reserved_bytes: int) -> None:
        if reserved_bytes < 1:
            raise StagingLimitExceeded("reserved size must be positive")
        if reserved_bytes > self._max_object_bytes:
            raise StagingLimitExceeded("reserved size exceeds the object limit")

    def _validate_reservation(
        self,
        reservation: StagingReservation,
    ) -> tuple[_ParsedStorageKey, _ParsedPartKey]:
        if not isinstance(reservation, StagingReservation):
            raise InvalidStorageKey("a staging reservation is required")
        parsed = _parse_storage_key(reservation.storage_key)
        parsed_part = _parse_part_key(reservation.part_storage_key)
        self._validate_reserved_bytes(reservation.reserved_bytes)
        if (
            parsed.area != parsed_part.area
            or parsed.owner_id != parsed_part.owner_id
            or parsed.object_id != parsed_part.object_id
        ):
            raise InvalidStorageKey("partial key does not belong to the reserved object")
        return parsed, parsed_part

    def _open_root(self, *, create: bool) -> int:
        anchor = self._root.anchor
        try:
            current_fd = os.open(anchor, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        except OSError as exc:
            raise StagingLayoutError("staging root anchor is not safely accessible") from exc

        parts = self._root.parts[1:]
        try:
            for index, name in enumerate(parts):
                is_root = index == len(parts) - 1
                try:
                    next_fd = self._open_child_directory(
                        current_fd,
                        name,
                        create=create and is_root,
                        private=is_root,
                    )
                except StagingObjectNotFound:
                    if is_root:
                        raise StagingObjectNotFound("staging root does not exist") from None
                    raise StagingLayoutError("staging root parent does not exist") from None
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _open_child_directory(
        parent_fd: int,
        name: str,
        *,
        create: bool,
        private: bool = True,
    ) -> int:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise StagingLayoutError("staging directory could not be created safely") from exc
        try:
            entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StagingObjectNotFound("staging directory does not exist") from exc
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
            raise StagingLayoutError("staging path contains an unsafe directory")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=parent_fd,
            )
            if private:
                os.fchmod(descriptor, 0o700)
            return descriptor
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise StagingLayoutError("staging directory is not safely accessible") from exc

    def _open_owner_directory(self, parsed: _ParsedStorageKey, *, create: bool) -> int:
        return self._open_owner_directory_by_id(parsed.area, parsed.owner_id, create=create)

    def _open_owner_directory_by_id(self, area: StagingArea, owner_id: str, *, create: bool) -> int:
        root_fd = self._open_root(create=create)
        try:
            area_fd = self._open_child_directory(root_fd, area.value, create=create)
            try:
                return self._open_child_directory(area_fd, owner_id, create=create)
            finally:
                os.close(area_fd)
        finally:
            os.close(root_fd)

    @staticmethod
    def _open_regular_file(parent_fd: int, name: str) -> int:
        try:
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise StagingObjectNotFound("staging object does not exist") from exc
        except OSError as exc:
            raise StagingLayoutError("staging object is not safely accessible") from exc
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise StagingLayoutError("staging object is not a regular file")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _verify_stream(stream: BinaryIO, expected_size: int, expected_sha256: str) -> None:
        if expected_size < 1:
            raise StagingIntegrityError("expected size must be positive")
        opened_stat = os.fstat(stream.fileno())
        if opened_stat.st_size != expected_size:
            raise StagingIntegrityError("staging object size does not match")
        digest = hashlib.sha256()
        size_bytes = 0
        stream.seek(0)
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
            size_bytes += len(chunk)
        if size_bytes != expected_size or digest.hexdigest() != expected_sha256:
            raise StagingIntegrityError("staging object SHA-256 does not match")

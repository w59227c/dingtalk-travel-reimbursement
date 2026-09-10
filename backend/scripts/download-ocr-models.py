#!/usr/bin/env python3
"""Download and verify the pinned OCR artifacts without installing application dependencies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = BACKEND_ROOT / "app/ocr/model_manifest.json"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MODEL_KINDS = ("detection", "recognition")


class ModelDownloadError(ValueError):
    """An artifact could not be obtained or did not match the pinned manifest."""


def _safe_component(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value not in (".", "..")
        and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None
    )


def _require_https(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ModelDownloadError("model source and redirects must use HTTPS without credentials")


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        manifest = json.load(stream)
    if manifest.get("schemaVersion") != 1 or set(manifest.get("models", {})) != set(MODEL_KINDS):
        raise ModelDownloadError("unsupported OCR model manifest")
    names: set[str] = set()
    for kind in MODEL_KINDS:
        spec = manifest["models"][kind]
        if not isinstance(spec, dict) or not _safe_component(spec.get("directoryName")):
            raise ModelDownloadError("invalid model directory name")
        name = spec["directoryName"]
        if name in names:
            raise ModelDownloadError("duplicate model directory name")
        names.add(name)
        if not isinstance(spec.get("sourceUrl"), str):
            raise ModelDownloadError("missing HTTPS model source")
        _require_https(spec["sourceUrl"])
        files = spec.get("files")
        if not isinstance(files, dict) or not files or not all(map(_safe_component, files)):
            raise ModelDownloadError("model files must have safe flat names")
        hashes = [spec.get("archiveSha256"), spec.get("directoryManifestSha256"), *files.values()]
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in hashes
        ):
            raise ModelDownloadError("model manifest must pin SHA-256 for every artifact")
    return manifest["models"]


def _load_validator(manifest_path: Path) -> ModuleType:
    # Import the same dependency-free validator used at runtime, with an isolated
    # manifest binding so a custom --manifest never mutates application's caches.
    module_spec = importlib.util.spec_from_file_location(
        "ocr_build_model_artifacts", BACKEND_ROOT / "app/ocr/model_artifacts.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    validator = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(validator)
    validator.MODEL_MANIFEST_PATH = manifest_path
    return validator


def _download_archive(
    url: str,
    target: Path,
    expected_sha256: str,
    *,
    attempts: int,
    timeout: float,
) -> None:
    _require_https(url)
    opener = urllib.request.build_opener(_HTTPSRedirectHandler())
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "expense-model-build/1"})
            with opener.open(request, timeout=timeout) as response, target.open("wb") as stream:
                _require_https(response.geturl())
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        expected_size = int(content_length)
                    except ValueError as error:
                        raise ModelDownloadError("invalid model archive Content-Length") from error
                    if not 0 <= expected_size <= MAX_ARCHIVE_BYTES:
                        raise ModelDownloadError("model archive exceeds download size limit")
                size = 0
                digest = hashlib.sha256()
                while chunk := response.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ModelDownloadError("model archive exceeds download size limit")
                    stream.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ModelDownloadError("downloaded model archive SHA-256 does not match manifest")
            return
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise ModelDownloadError(f"model source returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt + 1 == attempts:
                raise ModelDownloadError(
                    f"model download failed after {attempts} attempts"
                ) from error
        if target.exists():
            target.unlink()
        time.sleep(min(2**attempt, 8))


def _extract_archive(archive_path: Path, target: Path, expected_files: set[str]) -> None:
    """Read only one root directory and its exact regular-file children; never extractall."""
    seen: set[str] = set()
    files: set[str] = set()
    members: list[tuple[tarfile.TarInfo, str]] = []
    root: str | None = None
    expanded_size = 0
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if not parts or any(not _safe_component(part) for part in parts):
                raise ModelDownloadError("unsafe path in model archive")
            if name in seen:
                raise ModelDownloadError("duplicate path in model archive")
            seen.add(name)
            if root is None:
                root = parts[0]
            if parts[0] != root:
                raise ModelDownloadError("model archive contains multiple root directories")
            if member.isdir() and len(parts) == 1:
                continue
            if not member.isfile() or len(parts) != 2 or parts[1] not in expected_files:
                raise ModelDownloadError("model archive contains an unexpected file or member type")
            if member.sparse is not None or member.size < 0:
                raise ModelDownloadError(
                    "sparse or invalid files are not allowed in model archives"
                )
            expanded_size += member.size
            if expanded_size > MAX_EXTRACTED_BYTES:
                raise ModelDownloadError("model archive exceeds extraction size limit")
            files.add(parts[1])
            members.append((member, parts[1]))
        if files != expected_files:
            raise ModelDownloadError("model archive is missing manifest files")
        target.mkdir(mode=0o755)
        for member, name in members:
            source = archive.extractfile(member)
            if source is None:
                raise ModelDownloadError("model archive member is unreadable")
            # No archive ownership, permissions, symlinks or metadata reach the filesystem.
            with source, (target / name).open("xb") as destination:
                shutil.copyfileobj(source, destination, length=CHUNK_BYTES)


def download_models(
    manifest_path: Path,
    output: Path,
    *,
    attempts: int = 3,
    timeout: float = 30,
) -> None:
    if not 1 <= attempts <= 5 or not 0 < timeout <= 120:
        raise ModelDownloadError("attempts must be 1–5 and timeout must be in (0, 120] seconds")
    specs = _load_manifest(manifest_path)
    validator = _load_validator(manifest_path)
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            raise ModelDownloadError("model output must be a directory")
        expected_names = {spec["directoryName"] for spec in specs.values()}
        if {entry.name for entry in output.iterdir()} != expected_names or not all(
            validator.model_directory_ready(output / spec["directoryName"], kind)
            for kind, spec in specs.items()
        ):
            raise ModelDownloadError(
                "existing model output failed manifest validation; remove it first"
            )
        print("Existing OCR model output matches the pinned manifest.")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    # Work on the same filesystem as output so publishing both models is one atomic rename.
    with tempfile.TemporaryDirectory(prefix=".ocr-models-", dir=output.parent) as scratch:
        scratch_path = Path(scratch)
        staged_output = scratch_path / "models"
        staged_output.mkdir(mode=0o755)
        for kind, spec in specs.items():
            archive = scratch_path / f"{kind}.tar"
            print(f"Downloading pinned OCR {kind} model...", flush=True)
            _download_archive(
                spec["sourceUrl"],
                archive,
                spec["archiveSha256"],
                attempts=attempts,
                timeout=timeout,
            )
            directory = staged_output / spec["directoryName"]
            _extract_archive(archive, directory, set(spec["files"]))
            if not validator.model_directory_ready(directory, kind):
                raise ModelDownloadError(f"{kind} model files failed manifest SHA-256 validation")
            for path in directory.iterdir():
                path.chmod(0o444)
            directory.chmod(0o555)
            print(f"Verified OCR {kind} model archive and all model files.", flush=True)
        os.rename(staged_output, output)
        output.chmod(0o555)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    arguments = parser.parse_args()
    try:
        download_models(
            arguments.manifest.resolve(),
            arguments.output.absolute(),
            attempts=arguments.attempts,
            timeout=arguments.timeout,
        )
    except (ModelDownloadError, OSError, ValueError, tarfile.TarError) as error:
        print(f"OCR model build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

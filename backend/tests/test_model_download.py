from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import tarfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def downloader():
    script = Path(__file__).resolve().parents[1] / "scripts/download-ocr-models.py"
    spec = importlib.util.spec_from_file_location("model_downloader", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(files: dict[str, bytes], extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        root = tarfile.TarInfo("official_model_infer/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in files.items():
            member = tarfile.TarInfo(f"official_model_infer/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for member in extra or []:
            archive.addfile(member, io.BytesIO(b""))
    return buffer.getvalue()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def artifacts(tmp_path):
    files = {
        "inference.json": b"{}",
        "inference.pdiparams": b"model",
        "inference.yml": b"name: OCR",
    }
    archive = _archive(files)
    hashes = {name: _sha(content) for name, content in files.items()}
    aggregate = _sha("".join(f"{hashes[name]}  ./{name}\n" for name in sorted(hashes)).encode())
    models = {
        kind: {
            "directoryName": f"pinned_{kind}",
            "sourceUrl": f"https://models.example.invalid/{kind}.tar",
            "archiveSha256": _sha(archive),
            "directoryManifestSha256": aggregate,
            "files": hashes.copy(),
        }
        for kind in ("detection", "recognition")
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "models": models}))
    return SimpleNamespace(manifest=manifest, models=models, files=files, archive=archive)


class _Response(io.BytesIO):
    def __init__(self, content, *, headers=None, url="https://models.example.invalid/model.tar"):
        super().__init__(content)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


def _mock_source(monkeypatch, downloader, content, **response_options):
    requests = []

    def open_request(request, *, timeout):
        requests.append((request.full_url, timeout))
        return _Response(content, **response_options)

    monkeypatch.setattr(
        downloader.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request)
    )
    return requests


def test_download_verifies_and_publishes_both_models_read_only(
    tmp_path, monkeypatch, downloader, artifacts
):
    requests = _mock_source(monkeypatch, downloader, artifacts.archive)
    output = tmp_path / "output"
    downloader.download_models(artifacts.manifest, output)
    assert len(requests) == 2
    validator = downloader._load_validator(artifacts.manifest)
    for kind, spec in artifacts.models.items():
        directory = output / spec["directoryName"]
        assert validator.model_directory_ready(directory, kind)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o555
        for name, content in artifacts.files.items():
            assert (directory / name).read_bytes() == content
            assert stat.S_IMODE((directory / name).stat().st_mode) == 0o444
    # Reusing an existing download still validates every byte against the manifest.
    downloader.download_models(artifacts.manifest, output)
    assert len(requests) == 2
    damaged = output / "pinned_recognition/inference.json"
    damaged.chmod(0o644)
    damaged.write_bytes(b"broken")
    with pytest.raises(downloader.ModelDownloadError, match="existing model output failed"):
        downloader.download_models(artifacts.manifest, output)


def test_archive_hash_mismatch_does_not_retry_or_publish(
    tmp_path, monkeypatch, downloader, artifacts
):
    requests = _mock_source(monkeypatch, downloader, b"corrupt archive")
    output = tmp_path / "output"
    with pytest.raises(downloader.ModelDownloadError, match="archive SHA-256"):
        downloader.download_models(artifacts.manifest, output)
    assert len(requests) == 1
    assert not output.exists()
    assert not list(tmp_path.glob(".ocr-models-*"))


@pytest.mark.parametrize("bad_hash", ["file", "directory"])
def test_file_and_directory_hashes_are_checked_before_publish(
    tmp_path, monkeypatch, downloader, artifacts, bad_hash
):
    # Recognition fails after detection succeeds: neither model should be published.
    spec = artifacts.models["recognition"]
    if bad_hash == "file":
        spec["files"]["inference.json"] = "0" * 64
    else:
        spec["directoryManifestSha256"] = "0" * 64
    artifacts.manifest.write_text(json.dumps({"schemaVersion": 1, "models": artifacts.models}))
    _mock_source(monkeypatch, downloader, artifacts.archive)
    output = tmp_path / "output"
    with pytest.raises(downloader.ModelDownloadError, match="recognition model files"):
        downloader.download_models(artifacts.manifest, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".ocr-models-*"))


@pytest.mark.parametrize(
    ("name", "member_type"),
    [
        ("/absolute", tarfile.REGTYPE),
        ("official_model_infer/../escape", tarfile.REGTYPE),
        ("official_model_infer/extra", tarfile.REGTYPE),
        ("official_model_infer/inference.json", tarfile.REGTYPE),
        ("other_root/inference.json", tarfile.REGTYPE),
        ("official_model_infer/link", tarfile.SYMTYPE),
        ("official_model_infer/link", tarfile.LNKTYPE),
        ("official_model_infer/device", tarfile.CHRTYPE),
        ("official_model_infer/fifo", tarfile.FIFOTYPE),
        ("official_model_infer/nested", tarfile.DIRTYPE),
    ],
)
def test_unsafe_or_unexpected_tar_members_are_rejected(
    tmp_path, downloader, artifacts, name, member_type
):
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.linkname = "../../escape"
    archive = tmp_path / "unsafe.tar"
    archive.write_bytes(_archive(artifacts.files, [member]))
    output = tmp_path / "model"
    with pytest.raises(downloader.ModelDownloadError):
        downloader._extract_archive(archive, output, set(artifacts.files))
    assert not output.exists()
    assert not (tmp_path / "escape").exists()


def test_missing_archive_file_is_rejected(tmp_path, downloader, artifacts):
    archive = tmp_path / "missing.tar"
    archive.write_bytes(_archive({"inference.json": b"{}"}))
    with pytest.raises(downloader.ModelDownloadError, match="missing manifest files"):
        downloader._extract_archive(archive, tmp_path / "output", set(artifacts.files))


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "9999999999"}])
def test_download_limit_checks_headers_and_stream(tmp_path, monkeypatch, downloader, headers):
    monkeypatch.setattr(downloader, "MAX_ARCHIVE_BYTES", 3)
    requests = _mock_source(monkeypatch, downloader, b"large", headers=headers)
    with pytest.raises(downloader.ModelDownloadError, match="download size limit"):
        downloader._download_archive(
            "https://models.example.invalid/model.tar",
            tmp_path / "archive",
            _sha(b"large"),
            attempts=3,
            timeout=1,
        )
    assert len(requests) == 1


def test_extraction_limit_is_checked_before_writing(tmp_path, monkeypatch, downloader, artifacts):
    monkeypatch.setattr(downloader, "MAX_EXTRACTED_BYTES", 3)
    archive = tmp_path / "large.tar"
    archive.write_bytes(artifacts.archive)
    output = tmp_path / "output"
    with pytest.raises(downloader.ModelDownloadError, match="extraction size limit"):
        downloader._extract_archive(archive, output, set(artifacts.files))
    assert not output.exists()


def test_network_errors_have_bounded_retries(tmp_path, monkeypatch, downloader):
    requests = []

    def offline(*_args, **_kwargs):
        requests.append(1)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(
        downloader.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=offline)
    )
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    with pytest.raises(downloader.ModelDownloadError, match="after 3 attempts"):
        downloader._download_archive(
            "https://models.example.invalid/model.tar",
            tmp_path / "archive",
            "0" * 64,
            attempts=3,
            timeout=1,
        )
    assert len(requests) == 3


def test_download_recovers_after_transient_http_failure(
    tmp_path, monkeypatch, downloader, artifacts
):
    requests = []

    def temporary_failure(request, *, timeout):
        requests.append(1)
        if len(requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, None)
        return _Response(artifacts.archive)

    monkeypatch.setattr(
        downloader.urllib.request,
        "build_opener",
        lambda *_: SimpleNamespace(open=temporary_failure),
    )
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    target = tmp_path / "archive"
    downloader._download_archive(
        "https://models.example.invalid/model.tar",
        target,
        _sha(artifacts.archive),
        attempts=3,
        timeout=1,
    )
    assert len(requests) == 2
    assert target.read_bytes() == artifacts.archive


def test_https_redirect_downgrade_is_rejected(downloader):
    with pytest.raises(downloader.ModelDownloadError, match="HTTPS"):
        downloader._HTTPSRedirectHandler().redirect_request(
            None, None, 302, "redirect", {}, "http://models.example.invalid/model.tar"
        )

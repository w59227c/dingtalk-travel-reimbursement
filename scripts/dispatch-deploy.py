#!/usr/bin/env python3
"""Transfer a production release over verified SSH; never interpolate secret shell code."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def deployment_target(environ: dict[str, str]) -> tuple[str, str, str, str]:
    host = environ.get("DEPLOY_HOST", "")
    user = environ.get("DEPLOY_USER", "")
    port = environ.get("DEPLOY_PORT", "22") or "22"
    directory = environ.get("DEPLOY_DIR", "/opt/dingtalk-expense")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("DEPLOY_HOST must be a hostname or IPv4 address")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("DEPLOY_USER is invalid")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("DEPLOY_PORT is invalid")
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", directory)
        or ".." in PurePosixPath(directory).parts
        or directory.rstrip("/") in {"", "/", "/etc", "/usr", "/var", "/opt", "/home"}
    ):
        raise ValueError("DEPLOY_DIR must be an absolute dedicated application directory")
    return host, user, port, directory.rstrip("/")


def run(command: list[str], **kwargs: object) -> None:
    subprocess.run(command, check=True, **kwargs)


def release_source(environ: dict[str, str]) -> str:
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY is required for release provenance")
    owner = repository.split("/", 1)[0].lower()
    for service in ("backend", "web"):
        expected = rf"ghcr\.io/{re.escape(owner)}/dingtalk-expense-{service}@sha256:[a-f0-9]{{64}}"
        if not re.fullmatch(expected, environ.get(f"{service.upper()}_IMAGE", "")):
            raise ValueError("Deployment images must be the repository's published GHCR digests")
    return f"https://github.com/{repository}"


def main() -> int:
    # Do not print values or subprocess environments in diagnostics.
    environment = dict(os.environ)
    host, user, port, directory = deployment_target(environment)
    source = release_source(environment)
    revision = environment.get("DEPLOY_REVISION", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("DEPLOY_REVISION must be a complete commit SHA")
    for key in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        if not environment.get(key, "").strip():
            raise ValueError(f"{key} is required")
    release_id = f"{revision}-{uuid.uuid4().hex[:12]}"
    incoming = f"{directory}/incoming/{release_id}"
    destination = f"{user}@{host}"
    with tempfile.TemporaryDirectory(prefix="expense-deploy-") as scratch:
        scratch_path = Path(scratch)
        key_path = scratch_path / "identity"
        hosts_path = scratch_path / "known_hosts"
        key_path.write_text(environment["DEPLOY_SSH_KEY"].rstrip() + "\n")
        hosts_path.write_text(environment["DEPLOY_KNOWN_HOSTS"].rstrip() + "\n")
        key_path.chmod(0o600)
        hosts_path.chmod(0o600)
        env_path = scratch_path / ".env.production"
        run([sys.executable, str(ROOT / "scripts/render-production-env.py"),
             "--output", str(env_path)])
        manifest_path = scratch_path / "release.json"
        manifest_path.write_text(json.dumps({
            "revision": revision,
            "source": source,
            "backend_image": environment["BACKEND_IMAGE"],
            "web_image": environment["WEB_IMAGE"],
            "model_manifest_sha256": hashlib.sha256(
                (ROOT / "backend/app/ocr/model_manifest.json").read_bytes()
            ).hexdigest(),
        }, indent=2) + "\n")
        options = ["-i", str(key_path), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                   "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts_path}",
                   "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=3"]
        ssh = ["ssh", *options, "-p", port, destination]
        run([*ssh, "umask 077; mkdir -p -- " + shlex.quote(incoming)])
        try:
            run(["scp", *options, "-P", port,
                 str(ROOT / "compose.production.yml"),
                 str(ROOT / "scripts/deploy-production.sh"),
                 str(ROOT / "scripts/render-production-env.py"),
                 str(env_path), str(manifest_path), f"{destination}:{incoming}/"])
            run([*ssh, shlex.join(["bash", f"{incoming}/deploy-production.sh",
                                  directory, incoming])])
        finally:
            # The server script copies accepted releases out of incoming. Remove
            # only this invocation's staging directory, including its env file.
            subprocess.run([*ssh, "rm -rf -- " + shlex.quote(incoming)], check=False)
    print("Production deployment completed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        # Subprocess command text and secret values are deliberately omitted.
        print(f"Deployment failed ({type(error).__name__}); inspect the failed step.",
              file=sys.stderr)
        raise SystemExit(1) from None

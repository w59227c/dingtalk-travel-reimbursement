import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RENDERER = ROOT / "scripts/render-production-env.py"
DEPLOYER = ROOT / "scripts/deploy-production.sh"
COMPOSE = ROOT / "compose.production.yml"
SPEC = importlib.util.spec_from_file_location("production_env", RENDERER)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def inputs() -> dict[str, str]:
    return {
        "BACKEND_IMAGE": "ghcr.io/test/backend@sha256:" + "a" * 64,
        "WEB_IMAGE": "ghcr.io/test/web@sha256:" + "b" * 64,
        "DINGTALK_CLIENT_ID": "test-client",
        "DINGTALK_CLIENT_SECRET": "synthetic-value",
        "DINGTALK_CORP_ID": "test-corp",
        "DINGTALK_AGENT_ID": "1234",
        "SESSION_SECRET": "stable-synthetic-session-secret-for-tests",
        "ADMIN_USER_IDS": "user-one,user-two",
    }


def generate(path: Path, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RENDERER), "--output", str(path)],
        env=inputs() | (overrides or {}),
        text=True,
        capture_output=True,
        check=False,
    )


def test_renderer_preserves_special_values_and_session_secret(tmp_path: Path) -> None:
    output = tmp_path / ".env.production"
    secret = " '$NAME ${NAME} $$ \"quotes\" # 中文 \u2028\u2029 \\tab\\t \\' last\\"
    for _ in range(2):
        result = generate(output, {"DINGTALK_CLIENT_SECRET": secret})
        assert result.returncode == 0, result.stderr
        actual = renderer.read_generated(output)
        assert actual["DINGTALK_CLIENT_SECRET"] == secret
        assert actual["SESSION_SECRET"] == inputs()["SESSION_SECRET"]
        assert actual["DINGTALK_OA_WORKER_ENABLED"] == "false"
        assert output.stat().st_mode & 0o777 == 0o600
        assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SESSION_SECRET", "short"),
        ("DINGTALK_CLIENT_SECRET", "private\nsecret"),
        ("DINGTALK_CLIENT_SECRET", "private\rsecret"),
        ("DATABASE_URL", "sqlite:////elsewhere/app.db"),
        ("REIMBURSEMENT_STAGING_DIR", "/tmp/staging"),
        ("BACKEND_IMAGE", "ghcr.io/test/backend:latest"),
        ("WEB_IMAGE", "ghcr.io/test/web@sha256:bad"),
        ("DINGTALK_AGENT_ID", "0"),
        ("ADMIN_USER_IDS", " , "),
        ("COMPOSE_PROJECT_NAME", "../other"),
        ("SESSION_COOKIE_SECURE", "maybe"),
        ("APP_PORT", "65536"),
        ("APP_BIND_ADDRESS", "host:1234"),
    ],
)
def test_renderer_rejects_unsupported_values_without_replacing_file(
    tmp_path: Path, name: str, value: str
) -> None:
    output = tmp_path / ".env.production"
    output.write_text("previous configuration")
    result = generate(output, {name: value})
    assert result.returncode != 0
    assert name in result.stderr
    assert output.read_text() == "previous configuration"
    assert "private" not in result.stdout + result.stderr


@pytest.mark.parametrize("line", ['SESSION_SECRET="$UNESCAPED"', 'A="one"\nA="two"'])
def test_reader_rejects_noncanonical_or_duplicate_keys(tmp_path: Path, line: str) -> None:
    path = tmp_path / "invalid.env"
    path.write_text(line)
    with pytest.raises(ValueError):
        renderer.read_generated(path)


def test_real_compose_parses_special_values_and_production_contract(tmp_path: Path) -> None:
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is unavailable")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    if version.returncode:
        pytest.skip("Docker Compose CLI is unavailable")
    secret = 'literal $TOKEN ${TOKEN} $$ \\n \\\\ \\\' "双引号" # trailing\\'
    output = tmp_path / ".env.production"
    assert generate(output, {"DINGTALK_CLIENT_SECRET": secret}).returncode == 0
    command = ["docker", "compose", "--env-file", str(output), "-f", str(COMPOSE), "config"]
    clean = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    result = subprocess.run(command + ["--environment"], env=clean, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    parsed = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert parsed["DINGTALK_CLIENT_SECRET"] == secret
    model = subprocess.run(
        command + ["--format", "json"], env=clean, capture_output=True, text=True, check=True
    )
    services = json.loads(model.stdout)["services"]
    for name, service in services.items():
        assert "build" not in service, name
        assert service["read_only"]
        assert service["platform"] == "linux/amd64"
        assert "@sha256:" in service["image"]
        assert all(volume["type"] == "volume" for volume in service.get("volumes", []))
    assert services["backend"]["environment"]["DATABASE_URL"] == "sqlite:////app/data/app.db"


def write_release(directory: Path) -> None:
    directory.mkdir(parents=True)
    assert generate(directory / ".env.production").returncode == 0
    shutil.copy(RENDERER, directory / RENDERER.name)
    shutil.copy(COMPOSE, directory / COMPOSE.name)
    (directory / "release.json").write_text(
        json.dumps(
            {
                "revision": "1" * 40,
                "backend_image": inputs()["BACKEND_IMAGE"],
                "web_image": inputs()["WEB_IMAGE"],
                "model_manifest_sha256": "2" * 64,
                "source": "https://github.com/test/repository",
            }
        )
    )


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(__file__).parent
args = sys.argv[1:]
scenario = json.loads((root / 'scenario.json').read_text())
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({'args': args, 'session_secret': os.environ.get('SESSION_SECRET'),
                         'compose_project': os.environ.get('COMPOSE_PROJECT_NAME')}) + '\n')
assert args[:2] == ['--context', 'default']
args = args[2:]
if args[0] == 'info': print('linux/amd64')
elif args[:2] == ['volume', 'inspect']:
    if not scenario.get('existing'): sys.exit(1)
    if '--format' in args:
        name = args[-1]
        logical = 'sqlite_data' if name.endswith('_sqlite_data') else 'reimbursement_staging'
        print('dingtalk-travel-reimbursement|' + logical)
elif args[0] == 'ps':
    if scenario.get('existing'): print('old-backend')
elif args[0] == 'inspect':
    print(json.dumps([{'Mounts': [
        {'Type': 'volume', 'Name': 'dingtalk-travel-reimbursement_sqlite_data',
         'Destination': '/app/data'},
        {'Type': 'volume', 'Name': 'dingtalk-travel-reimbursement_reimbursement_staging',
         'Destination': '/app/staging'}
    ], 'Config': {'Env': ['DATABASE_URL=sqlite:////app/data/app.db']}}]))
elif args[:2] == ['image', 'inspect']:
    revision = '3' * 40 if scenario.get('fail') == 'revision' else '1' * 40
    source = 'https://github.com/test/repository'
    if scenario.get('fail') == 'source': source += '-other'
    print(json.dumps({'org.opencontainers.image.revision': revision,
                      'org.opencontainers.image.source': source}))
elif args[0] == 'compose':
    command = args[args.index('-f') + 2:]
    if command[0] == scenario.get('fail'): sys.exit(9)
elif args[0] == 'run':
    if args[-1] == 'backup' and scenario.get('fail') == 'backup': sys.exit(9)
    if args[-1] not in ('check', 'backup'):
        print(('3' if scenario.get('fail') == 'manifest' else '2') * 64)
"""


@pytest.mark.parametrize(
    "failure", [None, "pull", "revision", "source", "manifest", "backup", "up", "exec"]
)
def test_deployment_preserves_state_and_stops_failed_release(
    tmp_path: Path, failure: str | None
) -> None:
    root = tmp_path / "deployment"
    previous = root / "releases/previous"
    incoming = root / "incoming/candidate"
    write_release(previous)
    write_release(incoming)
    (root / "current").symlink_to("releases/previous")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "docker").write_text(FAKE_DOCKER)
    (fakebin / "flock").write_text("#!/bin/sh\nexit 0\n")
    for name in ("docker", "flock"):
        (fakebin / name).chmod(0o755)
    (fakebin / "scenario.json").write_text(json.dumps({"existing": True, "fail": failure}))
    environment = os.environ | {
        "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
        "SESSION_SECRET": "inherited-must-never-be-used",
        "COMPOSE_PROJECT_NAME": "wrong-inherited-project",
    }
    result = subprocess.run(
        ["bash", str(DEPLOYER), str(root), str(incoming)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = [json.loads(line) for line in (fakebin / "calls.jsonl").read_text().splitlines()]
    assert all(call["session_secret"] is None and call["compose_project"] is None for call in calls)
    operations = []
    for call in calls:
        args = call["args"]
        if "compose" in args:
            operations.append(args[args.index("-f") + 2])
        elif "run" in args:
            operations.append(args[-1])
    assert operations.index("config") < operations.index("pull")
    if failure is None:
        assert result.returncode == 0, result.stderr
        assert (root / "current").resolve() == root / "releases/candidate"
        assert operations.index("stop") < operations.index("backup") < operations.index("up")
        assert operations.index("up") < operations.index("exec")
    else:
        assert result.returncode != 0
        assert (root / "current").resolve() == previous
        assert (root / "releases/candidate/deployment-status").read_text() == "failed\n"
        if failure in {"backup", "up", "exec"}:
            assert "no database or image rollback" in result.stderr
        else:
            assert "stop" not in operations
        if failure in {"up", "exec"}:
            assert operations[-2:] == ["stop", "ps"]
    assert not any("down" in call["args"] for call in calls)


def test_backup_program_preserves_sqlite_and_staging_together(tmp_path: Path) -> None:
    data, staging, output = [tmp_path / name for name in ("data", "staging", "backup")]
    for path in (data, staging, output):
        path.mkdir(mode=0o700)
    with sqlite3.connect(data / "app.db") as database:
        database.execute("CREATE TABLE example (value TEXT)")
        database.execute("INSERT INTO example VALUES ('retained')")
    (staging / "receipt.pdf").write_bytes(b"persistent staging content")
    script = (
        DEPLOYER.read_text().split("backup_program=$(cat <<'PY'\n", 1)[1].split("\nPY\n)", 1)[0]
    )
    for old, replacement in (
        ("/source/data", data),
        ("/source/staging", staging),
        ("/backup", output),
    ):
        script = script.replace(f"Path('{old}')", f"Path({str(replacement)!r})")
    subprocess.run([sys.executable, "-c", script, "backup"], check=True)
    restored = tmp_path / "restored"
    for name in ("sqlite_data", "reimbursement_staging"):
        destination = restored / name
        destination.mkdir(parents=True)
        with tarfile.open(output / f"{name}.tar") as archive:
            archive.extractall(destination, filter="data")
        assert (output / f"{name}.tar").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(restored / "sqlite_data/app.db") as database:
        assert database.execute("SELECT value FROM example").fetchone() == ("retained",)
    assert (
        restored / "reimbursement_staging/receipt.pdf"
    ).read_bytes() == b"persistent staging content"
    assert json.loads((output / "backup.json").read_text())["consistent_after_backend_stop"]

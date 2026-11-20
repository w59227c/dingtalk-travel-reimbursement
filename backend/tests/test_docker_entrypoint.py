import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("invalid_spool", [False, True])
def test_entrypoint_prepares_private_sqlite_temp_before_migration(tmp_path, invalid_spool):
    backend = Path(__file__).parents[1]
    commands = tmp_path / "commands"
    commands.mkdir()
    python = commands / "python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    marker = tmp_path / "migration.json"
    fake_alembic = commands / "alembic"
    fake_alembic.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, stat, sys\n"
        "p=pathlib.Path(os.environ['SQLITE_TMPDIR'])\n"
        "assert p.is_dir() and not p.is_symlink()\n"
        "assert stat.S_IMODE(p.stat().st_mode)==0o700\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'path':str(p),'args':sys.argv[1:]}}))\n"
    )
    fake_alembic.chmod(0o700)
    temp = tmp_path / "runtime-temp"
    if invalid_spool:
        temp.mkdir()
        target = tmp_path / "unrelated"
        target.mkdir(mode=0o755)
        original_mode = stat.S_IMODE(target.stat().st_mode)
        (temp / ".spool").symlink_to(target, target_is_directory=True)
    env = {
        **os.environ,
        "APP_ENV": "test",
        "TEMP_DIR": str(temp),
        "TMPDIR": str(temp / ".spool"),
        "SQLITE_TMPDIR": str(tmp_path / "missing"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'unused.db'}",
        "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        [
            "sh",
            str(backend / "docker-entrypoint.sh"),
            "python",
            "-c",
            "import os; print(os.environ['SQLITE_TMPDIR'])",
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if invalid_spool:
        assert result.returncode != 0
        assert not marker.exists()
        assert stat.S_IMODE(target.stat().st_mode) == original_mode
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(marker.read_text()) == {
            "path": str(temp / ".spool"),
            "args": ["upgrade", "head"],
        }
        assert result.stdout.strip() == str(temp / ".spool")

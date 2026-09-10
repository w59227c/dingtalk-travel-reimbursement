#!/usr/bin/env python3
"""Serialize explicitly supported production inputs without shell evaluation.

Only this generated format is accepted by the deployment script. Values use
JSON double-quote escaping and Compose's $$ escape for literal dollar signs.
Never source the output as a shell script or print it in CI logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

IMAGE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
VARIABLE_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*):([-?])([^}]*)\}")
FIXED_VALUES = {
    "APP_ENV": "production",
    "TARGET_PLATFORM": "linux/amd64",
    "DATABASE_URL": "sqlite:////app/data/app.db",
    "TEMP_DIR": "/tmp/expense",
    "REIMBURSEMENT_STAGING_DIR": "/app/staging",
    "EXCEL_TEMPLATE_PATH": "/app/app/templates/expense_template.xlsx",
    "AUTH_MOCK_ENABLED": "false",
    "OCR_ENABLED": "true",
    "OCR_FAKE_ENABLED": "false",
    "OCR_MODE": "local",
    "OCR_ENGINE": "paddle_static",
    "OCR_DETECTION_MODEL_DIR": "/opt/expense/models/PP-OCRv6_small_det",
    "OCR_RECOGNITION_MODEL_DIR": "/opt/expense/models/PP-OCRv6_small_rec",
}


def compose_variables(compose_file: Path) -> tuple[dict[str, str], set[str]]:
    defaults: dict[str, str] = {}
    required: set[str] = set()
    for name, operator, value in VARIABLE_PATTERN.findall(compose_file.read_text()):
        if operator == "?":
            required.add(name)
        else:
            defaults[name] = value
    return defaults, required


def validate(values: dict[str, str], defaults: dict[str, str], required: set[str]) -> None:
    allowed = defaults.keys() | required | FIXED_VALUES.keys()
    for name, value in values.items():
        if name not in allowed:
            raise ValueError(f"unsupported production setting: {name}")
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in value):
            raise ValueError(f"{name} cannot contain newlines or control characters")
        if name in FIXED_VALUES and value != FIXED_VALUES[name]:
            raise ValueError(f"{name} does not match the supported production configuration")
    for name in required:
        if not values.get(name, "").strip():
            raise ValueError(f"{name} is required")
    for name in ("BACKEND_IMAGE", "WEB_IMAGE"):
        if not IMAGE_PATTERN.fullmatch(values[name]):
            raise ValueError(f"{name} must be a lowercase registry image pinned to @sha256")
    project = values.get("COMPOSE_PROJECT_NAME", defaults["COMPOSE_PROJECT_NAME"])
    if not PROJECT_PATTERN.fullmatch(project):
        raise ValueError("COMPOSE_PROJECT_NAME must be 1-63 lowercase letters, digits, _ or -")
    if len(values["SESSION_SECRET"]) < 32:
        raise ValueError("SESSION_SECRET must contain at least 32 characters; provision it once")
    for name in ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_CORP_ID"):
        if values[name].strip().lower() in {
            "change-me",
            "changeme",
            "placeholder",
            "example",
            "secret",
        }:
            raise ValueError(f"{name} must not be a placeholder")
    if (
        not re.fullmatch(r"[0-9]+", values["DINGTALK_AGENT_ID"])
        or int(values["DINGTALK_AGENT_ID"]) < 1
    ):
        raise ValueError("DINGTALK_AGENT_ID must be a positive integer")
    if not any(item.strip() for item in values["ADMIN_USER_IDS"].split(",")):
        raise ValueError("ADMIN_USER_IDS must contain at least one userId")
    for name in ("SESSION_COOKIE_SECURE", "DINGTALK_OA_WORKER_ENABLED"):
        if values.get(name, defaults[name]) not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
    port = values.get("APP_PORT", defaults["APP_PORT"])
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("APP_PORT must be between 1 and 65535")
    address = values.get("APP_BIND_ADDRESS", defaults["APP_BIND_ADDRESS"])
    import ipaddress

    try:
        ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        raise ValueError("APP_BIND_ADDRESS must be an IPv4 address") from None


def encode(values: dict[str, str]) -> str:
    return "# Generated production configuration. Do not source or commit.\n" + "".join(
        f"{name}={json.dumps(value, ensure_ascii=False).replace('$', '$$')}\n"
        for name, value in sorted(values.items())
    )


def read_generated(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line or line.startswith("#"):
            continue
        name, separator, encoded = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError("configuration must use the unique keys emitted by the renderer")
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            raise ValueError(f"{name} must use the format emitted by the renderer") from None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a quoted string")
        value = value.replace("$$", "$")
        if json.dumps(value, ensure_ascii=False).replace("$", "$$") != encoded:
            raise ValueError(f"{name} must use the format emitted by the renderer")
        values[name] = value
    return values


def write_private(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check-file", type=Path)
    mode.add_argument(
        "--metadata-file", type=Path, help="print project and two image digests as TSV"
    )
    script_dir = Path(__file__).resolve().parent
    adjacent = script_dir / "compose.production.yml"
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=adjacent if adjacent.exists() else script_dir.parent / "compose.production.yml",
    )
    args = parser.parse_args()
    try:
        defaults, required = compose_variables(args.compose_file)
        if args.output:
            allowed = defaults.keys() | required | FIXED_VALUES.keys()
            values = {name: os.environ[name] for name in allowed if os.environ.get(name)}
            # Freeze all defaults into each release, so later script/config changes
            # cannot change the settings of an already generated deployment.
            values = defaults | FIXED_VALUES | values
        else:
            values = read_generated(args.check_file or args.metadata_file)
        validate(values, defaults, required)
        if args.output:
            write_private(args.output, encode(values))
        elif args.metadata_file:
            print(
                "\t".join(
                    values[name] for name in ("COMPOSE_PROJECT_NAME", "BACKEND_IMAGE", "WEB_IMAGE")
                )
            )
    except (ValueError, OSError) as error:
        parser.exit(2, f"production configuration rejected: {error}\n")


if __name__ == "__main__":
    main()

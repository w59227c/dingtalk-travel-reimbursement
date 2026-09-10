#!/usr/bin/env bash
# Run against the exact two final images that will be pushed. No real secrets.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 BACKEND_IMAGE WEB_IMAGE" >&2
  exit 2
fi
backend_image=$1
web_image=$2
repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
smoke_id="expense-smoke-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
backend_container="$smoke_id-backend"
web_container="$smoke_id-web"
offline_container="$smoke_id-offline"
network="$smoke_id-network"

cleanup() {
  status=$?
  trap - EXIT
  if [[ $status -ne 0 ]]; then
    # The script creates only synthetic credentials; never run on production containers.
    docker logs --tail 100 "$backend_container" >&2 2>/dev/null || true
    docker logs --tail 100 "$web_container" >&2 2>/dev/null || true
  fi
  docker rm -f "$offline_container" "$web_container" "$backend_container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Exercise the actual renderer and Compose dotenv parser, then confirm what a
# real container receives. A random project owns all temporary test volumes.
python3 - "$repository_root" "$backend_image" <<'PY'
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

root = Path(sys.argv[1])
compose = root / "compose.production.yml"
renderer = root / "scripts/render-production-env.py"
spec = importlib.util.spec_from_file_location("production_env_renderer", renderer)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
defaults, required = module.compose_variables(compose)
allowed = defaults.keys() | required | module.FIXED_VALUES.keys()
clean_environment = {key: value for key, value in os.environ.items() if key not in allowed}
expected = {
    "DINGTALK_CLIENT_ID": "ci-config-client",
    "DINGTALK_CLIENT_SECRET": r'''ci-'single'-"double"-\path\-$TOKEN-${UNDEFINED}-$$-#value''',
    "DINGTALK_CORP_ID": "ci-config-corp",
    "DINGTALK_AGENT_ID": "1",
    "ADMIN_USER_IDS": "ci-admin",
    "SESSION_SECRET": r'''ci-session-'single'-"double"-\path\-$TOKEN-${UNDEFINED}-$$-#value''',
    "DINGTALK_OA_WORKER_ENABLED": "false",
}
project = "expense-config-smoke-" + uuid.uuid4().hex[:12]
render_environment = clean_environment | expected | {
    "BACKEND_IMAGE": "ghcr.io/ci/smoke-backend@sha256:" + "1" * 64,
    "WEB_IMAGE": "ghcr.io/ci/smoke-web@sha256:" + "2" * 64,
    "COMPOSE_PROJECT_NAME": project,
}
with tempfile.TemporaryDirectory(prefix="expense-dotenv-smoke-") as temporary:
    env_file = Path(temporary) / ".env.production"
    subprocess.run([
        sys.executable, str(renderer), "--compose-file", str(compose), "--output", str(env_file)
    ], env=render_environment, check=True)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    override = Path(temporary) / "compose.smoke.json"
    override.write_text(json.dumps({"services": {"backend": {"image": sys.argv[2]}}}))
    compose_command = [
        "docker", "compose", "--project-name", project, "--env-file", str(env_file),
        "--file", str(compose), "--file", str(override),
    ]
    command = compose_command + [
        "run", "--rm", "--no-deps", "--pull", "never", "-T", "--entrypoint", "python", "backend", "-c",
        "import json, os, sys; expected=json.load(sys.stdin); "
        "assert all(os.environ.get(k) == v for k, v in expected.items()), 'Dotenv round trip failed'",
    ]
    try:
        subprocess.run(command, input=json.dumps(expected), text=True, env=clean_environment, check=True)
    finally:
        # Both project name and volume names come from the same random test ID.
        subprocess.run(
            compose_command + ["down", "--volumes", "--remove-orphans"],
            env=clean_environment, check=True,
        )
print("Generated production dotenv values survive Compose and real container injection")
PY

# Inspect saved layers, including files deleted by a later layer. Spooling one
# layer at a time bounds memory and supports both Docker and OCI export layouts.
python3 - "$backend_image" "$web_image" <<'PY'
import subprocess
import sys
import tarfile
import tempfile
from pathlib import PurePosixPath

canary = b"never-bake-runtime-credentials-9c02e28f"

def copy_and_check(source, destination=None):
    tail = b""
    while chunk := source.read(1024 * 1024):
        if canary in tail + chunk:
            raise SystemExit("Environment canary found in an image layer")
        tail = chunk[-len(canary):]
        if destination is not None:
            destination.write(chunk)

for image in sys.argv[1:]:
    process = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as exported:
            for entry in exported:
                if not entry.isfile():
                    continue
                with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as layer:
                    copy_and_check(exported.extractfile(entry), layer)
                    layer.seek(0)
                    try:
                        archive = tarfile.open(fileobj=layer, mode="r:*")
                    except tarfile.ReadError:
                        continue  # Image config or manifest, already checked above.
                    with archive:
                        for member in archive:
                            path = PurePosixPath(member.name)
                            if path.name == ".env" or path.name.startswith(".env."):
                                if str(path.parent) in {".", "app", "app/frontend", "usr/share/nginx/html"}:
                                    raise SystemExit("Environment file found in an image layer")
                            if member.isfile():
                                copy_and_check(archive.extractfile(member))
        if process.wait() != 0:
            raise SystemExit("Could not inspect saved image layers")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
print("Final image layers contain no environment canaries or application dotenv files")
PY

for image in "$backend_image" "$web_image"; do
  docker image inspect "$image" | python3 -c '
import json, sys
image = json.load(sys.stdin)[0]
assert image["Os"] == "linux" and image["Architecture"] == "amd64", "Expected linux/amd64"
user = image["Config"].get("User", "").split(":")[0]
assert user and user not in {"root", "0"}, "Image must default to a non-root user"
assert "never-bake-runtime-credentials-9c02e28f" not in json.dumps(image), "Environment canary in image config"
'
  docker history --no-trunc "$image" | python3 -c '
import sys
assert "never-bake-runtime-credentials-9c02e28f" not in sys.stdin.read(), "Environment canary in image history"
'
  docker run --rm --platform linux/amd64 --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges --entrypoint sh "$image" -ec '
      for path in /app/.env /app/.env.* /app/frontend/.env /app/frontend/.env.* /usr/share/nginx/html/.env /usr/share/nginx/html/.env.*; do
        if [ -e "$path" ]; then echo "Environment file found in final image" >&2; exit 1; fi
      done
      if [ -d /usr/share/nginx/html ]; then
        if grep -R -F "never-bake-runtime-credentials-9c02e28f" /usr/share/nginx/html >/dev/null; then
          echo "Environment canary found in frontend assets" >&2; exit 1
        fi
      fi
    '
done

# The manifest inside the final image must be the manifest recorded by the release.
expected_manifest=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$repository_root/backend/app/ocr/model_manifest.json")
actual_manifest=$(docker run --rm --platform linux/amd64 --network none --read-only \
  --entrypoint python "$backend_image" -c \
  'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/app/app/ocr/model_manifest.json").read_bytes()).hexdigest())')
[[ "$actual_manifest" == "$expected_manifest" ]] || { echo 'Final image has the wrong model manifest' >&2; exit 1; }

backend_options=(
  --platform linux/amd64 --read-only --cap-drop ALL --security-opt no-new-privileges
  --memory 3g --pids-limit 128
  --tmpfs /app/data:uid=10001,gid=10001,mode=0700,size=268435456
  --tmpfs /app/staging:uid=10001,gid=10001,mode=0700,size=268435456
  --tmpfs /tmp/expense:uid=10001,gid=10001,mode=0700,size=1073741824
  --env APP_ENV=production --env DATABASE_URL=sqlite:////app/data/app.db
  --env REIMBURSEMENT_STAGING_DIR=/app/staging
  --env DINGTALK_CLIENT_ID=ci-smoke-client --env DINGTALK_CLIENT_SECRET=ci-smoke-noncredential
  --env DINGTALK_CORP_ID=ci-smoke-corp --env DINGTALK_AGENT_ID=1 --env ADMIN_USER_IDS=ci-admin
  --env "SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  --env DINGTALK_OA_WORKER_ENABLED=false --env AUTH_MOCK_ENABLED=false
  --env OCR_ENABLED=true --env OCR_FAKE_ENABLED=false
  --env PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1
)

echo 'Checking real OCR initialization and inference with networking disabled'
# Run the repository runtime checker from stdin; only the checker is injected,
# while models and every runtime dependency must already be in the final image.
docker run --rm --name "$offline_container" --network none "${backend_options[@]}" \
  --env TMPDIR=/tmp/expense --entrypoint python -i "$backend_image" - \
  --detection /opt/expense/models/PP-OCRv6_small_det \
  --recognition /opt/expense/models/PP-OCRv6_small_rec < "$repository_root/scripts/check-ocr-runtime.py"

# Internal Docker network gives the Web image access to the backend but no
# external network. The production startup path runs migrations and OCR smoke.
docker network create --internal "$network" >/dev/null
docker run --detach --name "$backend_container" --network "$network" --network-alias backend \
  "${backend_options[@]}" "$backend_image" >/dev/null

probe='import json, urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/ready", timeout=5)); assert r["success"] is True and r["data"]["status"] == "ready" and r["data"]["checks"]["ocr"] == "configured"'
backend_ready=false
for ((attempt = 0; attempt < 90; attempt++)); do
  if docker exec "$backend_container" python -c "$probe" >/dev/null 2>&1; then
    backend_ready=true
    break
  fi
  [[ $(docker inspect --format '{{.State.Running}}' "$backend_container") == true ]] || break
  sleep 2
done
[[ "$backend_ready" == true ]] || { echo 'Production backend did not become ready' >&2; exit 1; }

web_options=(
  --platform linux/amd64 --network "$network" --read-only --cap-drop ALL
  --security-opt no-new-privileges --memory 128m --pids-limit 64
  --tmpfs /var/cache/nginx:uid=101,gid=101,mode=0700,size=33554432
  --tmpfs /var/run:uid=101,gid=101,mode=0700,size=1048576
)
docker run --rm "${web_options[@]}" "$web_image" -t
docker run --detach --name "$web_container" --network-alias web "${web_options[@]}" "$web_image" >/dev/null

web_ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if docker exec "$backend_container" python -c '
import json, urllib.request
with urllib.request.urlopen("http://web:8080/", timeout=5) as response:
    html = response.read().decode()
    assert response.status == 200 and "<html" in html.lower()
    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers
with urllib.request.urlopen("http://web:8080/api/ready", timeout=5) as response:
    report = json.load(response)
    assert report["success"] is True
    assert report["data"]["status"] == "ready"
    assert report["data"]["checks"]["ocr"] == "configured"
'; then
    web_ready=true
    break
  fi
  [[ $(docker inspect --format '{{.State.Running}}' "$web_container") == true ]] || break
  sleep 1
done
[[ "$web_ready" == true ]] || { echo 'Web integration check failed' >&2; exit 1; }
echo 'Final images passed offline OCR, migrations, production readiness and Web/API checks'

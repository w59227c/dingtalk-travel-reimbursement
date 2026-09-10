#!/usr/bin/env bash
# Single-host Linux/amd64 deployment. Never source .env.production.
# Usage: bash deploy-production.sh /opt/dingtalk-expense /opt/dingtalk-expense/incoming/<id>
set -Eeuo pipefail
umask 077

fail() { printf 'Deployment rejected: %s\n' "$*" >&2; exit 1; }
[[ $# == 2 ]] || fail "expected deployment root and incoming release directory"
for command in python3 docker flock; do
  command -v "$command" >/dev/null || fail "required command is missing: $command"
done
[[ $1 == /* && $2 == /* ]] || fail "deployment paths must be absolute"
deployment_root=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$1")
incoming=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$2")
[[ $deployment_root != / ]] || fail "deployment root must be a dedicated directory"
[[ $incoming == "$deployment_root"/incoming/* ]] || fail "incoming must be under deployment_root/incoming"
release_id=${incoming##*/}
[[ $release_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || fail "invalid release directory name"
mkdir -p "$deployment_root/releases" "$deployment_root/backups"
chmod 0700 "$deployment_root" "$deployment_root/releases" "$deployment_root/backups"
exec 9>"$deployment_root/.deploy.lock"
flock -w 600 9 || fail "another deployment still holds the server lock"

for name in compose.production.yml .env.production release.json render-production-env.py; do
  [[ -f $incoming/$name && ! -L $incoming/$name ]] || fail "missing regular release file: $name"
done
[[ ! -e $deployment_root/releases/$release_id ]] || fail "release directory already exists"
chmod 0700 "$incoming"
chmod 0600 "$incoming/.env.production"
renderer="$incoming/render-production-env.py"
metadata=$(python3 "$renderer" --metadata-file "$incoming/.env.production" --compose-file "$incoming/compose.production.yml")
IFS=$'\t' read -r project backend_image web_image <<< "$metadata"
release_identity=$(python3 - "$incoming/release.json" "$backend_image" "$web_image" <<'PY'
import json, re, sys
from pathlib import Path
release = json.loads(Path(sys.argv[1]).read_text())
if release.get("backend_image") != sys.argv[2] or release.get("web_image") != sys.argv[3]:
    sys.exit("Deployment rejected: release image digests do not match the generated configuration")
if not re.fullmatch(r"[a-f0-9]{40}", release.get("revision", "")):
    sys.exit("Deployment rejected: release revision must be a full commit SHA")
if not re.fullmatch(r"[a-f0-9]{64}", release.get("model_manifest_sha256", "")):
    sys.exit("Deployment rejected: release model manifest SHA-256 is missing")
if not re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", release.get("source", "")):
    sys.exit("Deployment rejected: release source must be a GitHub repository URL")
print('\t'.join(release[key] for key in ('revision', 'source', 'model_manifest_sha256')))
PY
)
IFS=$'\t' read -r revision source manifest_sha256 <<< "$release_identity"

# Compose gives inherited shell variables priority over --env-file. Preserve only
# the CLI path, user home and registry authentication directory, and use the local
# default Docker context. APP_*, COMPOSE_*, DOCKER_HOST, etc. cannot override release inputs.
clean_docker() {
  env -i PATH="$PATH" HOME="$HOME" DOCKER_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}" \
    docker --context default "$@"
}
compose() {
  clean_docker compose --project-name "$project" --env-file "$release/.env.production" \
    -f "$release/compose.production.yml" "$@"
}
engine=$(clean_docker info --format '{{.OSType}}/{{.Architecture}}')
[[ $engine == linux/x86_64 || $engine == linux/amd64 ]] || fail "Docker must run on Linux amd64"

release="$deployment_root/releases/$release_id"
mv "$incoming" "$release"
backup="$deployment_root/backups/$release_id"
mkdir "$backup"
writes_stopped=0
started_new=0
successful=0
on_exit() {
  status=$?
  if [[ $successful != 1 ]]; then
    set +e
    # A failed readiness check must not leave a new OA worker writing in the background.
    if [[ $started_new == 1 ]]; then compose stop --timeout 180 backend web; fi
    compose ps --all >"$release/container-status.txt" 2>&1
    printf 'failed\n' >"$release/deployment-status"
    printf 'Deployment failed; current was not changed. Release: %s; backup: %s\n' "$release" "$backup" >&2
    if [[ $writes_stopped == 1 ]]; then
      printf 'Deployment entered maintenance. Verify that writers are stopped and inspect the retained backup and migration state before recovery; no database or image rollback was attempted.\n' >&2
    fi
    [[ $status != 0 ]] || status=1
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
compose config --quiet

if [[ -e $deployment_root/current || -L $deployment_root/current ]]; then
  [[ -L $deployment_root/current ]] || fail "current must be a release symlink"
  previous=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$deployment_root/current")
  [[ $previous == "$deployment_root"/releases/* && -f $previous/.env.production ]] || fail "current points outside the saved releases"
  previous_metadata=$(python3 "$release/render-production-env.py" --metadata-file "$previous/.env.production" --compose-file "$previous/compose.production.yml")
  [[ ${previous_metadata%%$'\t'*} == "$project" ]] || fail "changing the existing Compose project would orphan its data"
  printf '%s\n' "$previous" >"$release/previous-release"
fi

data_volume="${project}_sqlite_data"
staging_volume="${project}_reimbursement_staging"
existing_volumes=0
for logical in sqlite_data reimbursement_staging; do
  volume="${project}_${logical}"
  if clean_docker volume inspect "$volume" >/dev/null 2>&1; then
    labels=$(clean_docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}|{{ index .Labels "com.docker.compose.volume" }}' "$volume")
    [[ $labels == "$project|$logical" ]] || fail "existing volume ownership does not match: $volume"
    existing_volumes=$((existing_volumes + 1))
  fi
done
[[ $existing_volumes == 0 || $existing_volumes == 2 ]] || fail "only one persistent volume exists; inspect data before deploying"
old_backends=$(clean_docker ps -aq --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service=backend)
if [[ -n $old_backends ]]; then
  [[ $old_backends != *$'\n'* && $existing_volumes == 2 ]] || fail "expected one backend using both existing volumes"
  clean_docker inspect "$old_backends" | python3 -c '
import json,sys
container=json.load(sys.stdin)[0]
expected={"/app/data":sys.argv[1], "/app/staging":sys.argv[2]}
mounts={m["Destination"]:m for m in container["Mounts"]}
for target,name in expected.items():
    if mounts.get(target,{}).get("Type")!="volume" or mounts[target].get("Name")!=name:
        sys.exit("Deployment rejected: existing backend uses different persistent storage")
settings=dict(item.split("=",1) for item in container["Config"]["Env"] if "=" in item)
if settings.get("DATABASE_URL","sqlite:////app/data/app.db")!="sqlite:////app/data/app.db":
    sys.exit("Deployment rejected: automatic backup supports only /app/data/app.db")
if settings.get("REIMBURSEMENT_STAGING_DIR","/app/staging")!="/app/staging":
    sys.exit("Deployment rejected: existing staging directory is not /app/staging")
' "$data_volume" "$staging_volume"
fi
if [[ -e $deployment_root/current && $existing_volumes != 2 ]]; then
  fail "saved deployment has lost its data volumes; refusing an empty replacement database"
fi

printf 'Validating release and pulling pinned images.\n'
compose pull --policy always
for image in "$backend_image" "$web_image"; do
  clean_docker image inspect --format '{{json .Config.Labels}}' "$image" | python3 -c '
import json,sys
labels=json.load(sys.stdin) or {}
if labels.get("org.opencontainers.image.revision") != sys.argv[1]:
    sys.exit("Deployment rejected: image revision does not match the release")
if labels.get("org.opencontainers.image.source") != sys.argv[2]:
    sys.exit("Deployment rejected: image source does not match the release")
' "$revision" "$source"
done
actual_manifest=$(clean_docker run --rm --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$backend_image" \
  -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("/app/app/ocr/model_manifest.json").read_bytes()).hexdigest())')
[[ $actual_manifest == "$manifest_sha256" ]] || fail "backend model manifest does not match the release"
backup_program=$(cat <<'PY'
import json, os, shutil, sqlite3, sys, tarfile, tempfile
from pathlib import Path

data, staging, output = Path('/source/data'), Path('/source/staging'), Path('/backup')
size = sum(p.stat().st_size for root in (data, staging) for p in root.rglob('*')
           if p.is_file() and not p.is_symlink())
if shutil.disk_usage(output).free < size * 2 + 512 * 1024 * 1024:
    sys.exit('Insufficient backup disk space: need twice the persistent data size plus 512 MiB')
if sys.argv[1] == 'check':
    sys.exit(0)
# The backend is stopped before either archive is made. Include SQLite WAL/SHM
# sidecars and every staging file together, preserving original ownership/modes.
for name, source in (('sqlite_data', data), ('reimbursement_staging', staging)):
    with tarfile.open(output / (name + '.tar'), 'w') as archive:
        archive.add(source, arcname='.')
database_exists = (data / 'app.db').is_file()
if database_exists:
    # Validate a private copy: SQLite may need to recover WAL or create SHM files.
    # The source volumes stay read-only throughout backup and verification.
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=output) as work:
        copied = Path(work) / 'data'
        shutil.copytree(data, copied, symlinks=True)
        if (copied / 'app.db').is_symlink():
            sys.exit('The SQLite database must not be a symbolic link')
        with sqlite3.connect(copied / 'app.db') as database:
            if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                sys.exit('SQLite backup integrity_check failed')
(output / 'backup.json').write_text(json.dumps({
    'database_path': '/app/data/app.db', 'database_exists': database_exists,
    'archives': ['sqlite_data.tar', 'reimbursement_staging.tar'],
    'consistent_after_backend_stop': True,
}) + '\n')
for file in output.iterdir():
    if file.is_file():
        file.chmod(0o600)
        with file.open('rb') as stream:
            os.fsync(stream.fileno())
        os.chown(file, output.stat().st_uid, output.stat().st_gid)
PY
)
backup_action() {
  clean_docker run --rm --network none --read-only --user 0:0 --cap-drop ALL \
    --cap-add DAC_OVERRIDE --cap-add CHOWN \
    --security-opt no-new-privileges:true \
    --mount "type=volume,source=$data_volume,target=/source/data,readonly" \
    --mount "type=volume,source=$staging_volume,target=/source/staging,readonly" \
    --mount "type=bind,source=$backup,target=/backup" \
    --entrypoint python "$backend_image" -c "$backup_program" "$1"
}
if [[ $existing_volumes == 2 ]]; then backup_action check; fi
printf 'Stopping writers for the SQLite and staging backup.\n'
writes_stopped=1
compose stop --timeout 180 backend web
if [[ $existing_volumes == 2 ]]; then
  backup_action backup
else
  printf '{"fresh_install":true,"archives":[]}\n' >"$backup/backup.json"
fi
cp "$release/release.json" "$backup/target-release.json"
if [[ -n ${previous:-} ]]; then cp "$previous/release.json" "$backup/previous-release.json"; fi

printf 'Starting the release, running migrations and checking readiness.\n'
started_new=1
compose up -d --no-build --wait --wait-timeout 180
compose exec -T web wget -q -O /dev/null http://127.0.0.1:8080/api/ready
printf 'ready\n' >"$release/deployment-status"
ln -s "releases/$release_id" "$deployment_root/.current-$release_id"
python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' \
  "$deployment_root/.current-$release_id" "$deployment_root/current"
successful=1
printf 'Deployment ready. Current release: %s\n' "$release_id"

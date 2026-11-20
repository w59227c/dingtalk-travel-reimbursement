#!/bin/sh
set -eu

sqlite_temp_dir="$(python - <<'PY'
from app.core.config import get_settings
from app.services.multipart_uploads import prepare_spool_directory

print(prepare_spool_directory(get_settings()))
PY
)"
export SQLITE_TMPDIR="$sqlite_temp_dir"

alembic upgrade head
exec "$@"

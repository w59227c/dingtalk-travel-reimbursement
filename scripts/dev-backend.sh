#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
DETECTION_MODEL_DIR="$PROJECT_DIR/backend/models/onnx/PP-OCRv6_small_det"
RECOGNITION_MODEL_DIR="$PROJECT_DIR/backend/models/onnx/PP-OCRv6_small_rec"

case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Linux:x86_64|Linux:amd64)
        ;;
    *)
        echo "本地 OCR 仅支持 macOS arm64 或 Linux x86_64/amd64。" >&2
        exit 2
        ;;
esac

python3 "$SCRIPT_DIR/check-ocr-models.py" \
    --detection "$DETECTION_MODEL_DIR" \
    --recognition "$RECOGNITION_MODEL_DIR"

cd "$PROJECT_DIR/backend"

# Deliberately fixed, synthetic identity for ordinary-browser development.
# These values override the caller's environment so a local request cannot
# choose another identity or accidentally inherit production configuration.
export APP_ENV=development
export AUTH_MOCK_ENABLED=true
export AUTH_MOCK_USER_ID=local-dev-user
export AUTH_MOCK_USER_NAME=本地开发用户
export AUTH_MOCK_DEPARTMENTS=100:本地开发部门
export ADMIN_USER_IDS=local-dev-user
export DINGTALK_CLIENT_ID=local-dev-client
export DINGTALK_CLIENT_SECRET=
export DINGTALK_CORP_ID=local-dev-corp
export SESSION_SECRET=
export SESSION_COOKIE_SECURE=false
export DATABASE_URL=sqlite:///./data/dev.db
export TEMP_DIR=/tmp/dingtalk-travel-reimbursement-dev
export REIMBURSEMENT_STAGING_DIR="$PROJECT_DIR/backend/data/reimbursement-staging"
export REIMBURSEMENT_STAGING_MAX_BYTES=4294967296
export EXCEL_TEMPLATE_PATH=app/templates/expense_template.xlsx
export OCR_ENABLED=true
export OCR_FAKE_ENABLED=false
export OCR_ENGINE=onnxruntime
export OCR_DETECTION_MODEL_DIR="$DETECTION_MODEL_DIR"
export OCR_RECOGNITION_MODEL_DIR="$RECOGNITION_MODEL_DIR"

# Explicit local model paths are mandatory; prevent PaddleX from probing or
# downloading from model hosts at runtime.
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1

mkdir -p data "$TEMP_DIR" "$REIMBURSEMENT_STAGING_DIR"
uv run --frozen --extra dev --extra ocr alembic upgrade head
exec uv run --frozen --extra dev --extra ocr uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload \
    --no-access-log

#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")
CONFIG_FILE=${DINGTALK_DEV_ENV_FILE:-"$PROJECT_DIR/.env.dingtalk-dev"}
if [ "${1:-}" = prod ]; then
    CONFIG_FILE="$PROJECT_DIR/.env"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "正式公司联调需要项目根目录 .env。" >&2
        exit 2
    fi
fi
DETECTION_MODEL_DIR="$PROJECT_DIR/backend/models/onnx/PP-OCRv6_small_det"
RECOGNITION_MODEL_DIR="$PROJECT_DIR/backend/models/onnx/PP-OCRv6_small_rec"

if [ -f "$CONFIG_FILE" ]; then
    set -a
    # This is a local, gitignored shell environment file controlled by the developer.
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
    set +a
elif [ -n "${DINGTALK_DEV_ENV_FILE:-}" ]; then
    echo "指定的钉钉联调配置文件不存在：$CONFIG_FILE" >&2
    exit 2
fi

for variable_name in DINGTALK_CLIENT_ID DINGTALK_CLIENT_SECRET DINGTALK_CORP_ID DINGTALK_AGENT_ID; do
    eval "variable_value=\${$variable_name:-}"
    if [ -z "$variable_value" ]; then
        echo "$variable_name 未配置，不能启动真实钉钉免登联调。" >&2
        exit 2
    fi
done

if [ -z "${SESSION_SECRET:-}" ]; then
    SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    export SESSION_SECRET
    echo "SESSION_SECRET 未配置，已为本次本地联调生成临时随机值。"
elif [ "${#SESSION_SECRET}" -lt 32 ]; then
    echo "SESSION_SECRET 必须至少包含 32 个字符。" >&2
    exit 2
fi

DINGTALK_DEV_COOKIE_SECURE=${DINGTALK_DEV_COOKIE_SECURE:-false}
case "$DINGTALK_DEV_COOKIE_SECURE" in
    true|false)
        ;;
    *)
        echo "DINGTALK_DEV_COOKIE_SECURE 只能是 true 或 false。" >&2
        exit 2
        ;;
esac

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

export APP_ENV=development
export AUTH_MOCK_ENABLED=false
export SESSION_COOKIE_SECURE="$DINGTALK_DEV_COOKIE_SECURE"
export DATABASE_URL=sqlite:///./data/dev-dingtalk.db
export TEMP_DIR=/tmp/dingtalk-travel-reimbursement-dingtalk-dev
REIMBURSEMENT_STAGING_DIR=${REIMBURSEMENT_STAGING_DIR:-"$PROJECT_DIR/backend/data/reimbursement-staging"}
export REIMBURSEMENT_STAGING_DIR
if [ "${1:-}" = prod ]; then
    export DATABASE_URL=sqlite:///./data/dev-dingtalk-prod.db
    export TEMP_DIR=/tmp/dingtalk-travel-reimbursement-dingtalk-prod
    export REIMBURSEMENT_STAGING_DIR="$PROJECT_DIR/backend/data/reimbursement-staging-prod"
    export SESSION_COOKIE_NAME=expense_dingtalk_prod_session
    echo "正式公司本地联调：使用独立数据；提交会进入真实 OA。模板目录需在本环境确认。"
fi
export REIMBURSEMENT_STAGING_MAX_BYTES=${REIMBURSEMENT_STAGING_MAX_BYTES:-4294967296}
export EXCEL_TEMPLATE_PATH=app/templates/expense_template.xlsx
export OCR_ENABLED=true
export OCR_FAKE_ENABLED=false
export OCR_ENGINE=onnxruntime
export OCR_DETECTION_MODEL_DIR="$DETECTION_MODEL_DIR"
export OCR_RECOGNITION_MODEL_DIR="$RECOGNITION_MODEL_DIR"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1

mkdir -p data "$TEMP_DIR" "$REIMBURSEMENT_STAGING_DIR"
uv run --frozen --extra dev --extra ocr alembic upgrade head
# The app already isolates image validation and OCR in fresh spawn processes.
# Uvicorn's macOS reload subprocess cannot reliably create those children.
exec uv run --frozen --extra dev --extra ocr uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --no-access-log

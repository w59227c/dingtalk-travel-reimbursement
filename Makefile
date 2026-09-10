.PHONY: \
	backend-install backend-test backend-lint backend-format \
	frontend-install frontend-test frontend-typecheck frontend-lint frontend-build \
	compose-config compose-config-development nginx-policy-check \
	ocr-models-check ocr-runtime-check verify deploy-check clean \
	dev dev-backend dev-frontend \
	dev-dingtalk dev-dingtalk-backend dev-dingtalk-frontend \
	dev-dingtalk-prod dev-dingtalk-prod-backend dev-dingtalk-prod-frontend \
	deploy up logs down images-build images-check

backend-install:
	cd backend && uv sync --frozen --extra dev --extra ocr

backend-test:
	cd backend && uv run --frozen --extra dev pytest

backend-lint:
	cd backend && uv run --frozen --extra dev ruff check app tests migrations scripts \
		../scripts/render-production-env.py ../scripts/dispatch-deploy.py

backend-format:
	cd backend && uv run --frozen --extra dev ruff format app tests migrations

frontend-install:
	cd frontend && npm ci --ignore-scripts --no-audit --no-fund

frontend-test:
	cd frontend && npm run test

frontend-typecheck:
	cd frontend && npm run typecheck

frontend-lint:
	cd frontend && npm run lint

frontend-build:
	cd frontend && npm run build

compose-config:
	docker compose config --quiet

compose-config-development:
	APP_ENV=development docker compose config --quiet

nginx-policy-check:
	sh scripts/check-nginx-policy.sh

ocr-models-check:
	python3 scripts/check-ocr-models.py \
		--detection backend/models/PP-OCRv6_small_det \
		--recognition backend/models/PP-OCRv6_small_rec

ocr-runtime-check:
	cd backend && PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1 \
		uv run --frozen --extra dev --extra ocr python ../scripts/check-ocr-runtime.py \
			--detection models/PP-OCRv6_small_det \
			--recognition models/PP-OCRv6_small_rec

verify: backend-test backend-lint frontend-test frontend-typecheck frontend-lint frontend-build compose-config-development nginx-policy-check ocr-models-check

deploy-check: compose-config nginx-policy-check ocr-models-check

# Remove only reproducible development artifacts. Runtime databases, uploaded
# files, OCR models, virtual environments, and installed Node dependencies are
# intentionally preserved.
clean:
	rm -rf .ruff_cache backend/.pytest_cache backend/.ruff_cache frontend/dist node_modules
	find backend/app backend/migrations backend/tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +

dev:
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	sh scripts/dev-backend.sh

dev-frontend:
	cd frontend && npm run dev

dev-dingtalk-backend:
	sh scripts/dev-dingtalk-backend.sh

dev-dingtalk-frontend:
	sh scripts/dev-dingtalk-frontend.sh

dev-dingtalk:
	$(MAKE) -j2 dev-dingtalk-backend dev-dingtalk-frontend

dev-dingtalk-prod:
	$(MAKE) -j2 dev-dingtalk-prod-backend dev-dingtalk-prod-frontend

dev-dingtalk-prod-backend:
	sh scripts/dev-dingtalk-backend.sh prod

dev-dingtalk-prod-frontend:
	sh scripts/dev-dingtalk-frontend.sh prod

up:
	docker compose up --build

deploy: deploy-check
	docker compose up --build --detach --wait

# Self-contained production images; these checks use only disposable containers.
images-build:
	docker build --platform linux/amd64 -t expense-backend:check backend
	docker build --platform linux/amd64 -f frontend/Dockerfile -t expense-web:check .

images-check:
	bash scripts/smoke-images.sh expense-backend:check expense-web:check

logs:
	docker compose logs --follow --tail=200

down:
	docker compose down

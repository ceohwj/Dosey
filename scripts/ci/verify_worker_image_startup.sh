#!/usr/bin/env bash

set -euo pipefail

image_tag="ah-05-04-worker-startup-test:latest"

cleanup() {
  docker image rm --force "${image_tag}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ -n "${WORKER_IMAGE_CACHE_FROM:-}" ]]; then
  build_args=(
    buildx build
    --load
    --cache-from "${WORKER_IMAGE_CACHE_FROM}"
    --file apps/ai_worker/Dockerfile
    --tag "${image_tag}"
  )
  if [[ -n "${WORKER_IMAGE_CACHE_TO:-}" ]]; then
    build_args+=(--cache-to "${WORKER_IMAGE_CACHE_TO}")
  fi
  docker "${build_args[@]}" .
else
  docker build --file apps/ai_worker/Dockerfile --tag "${image_tag}" .
fi
docker run --rm \
  --env PROTECTED_RETRIEVAL_ENABLED=false \
  "${image_tag}" \
  uv run --no-sync python -c \
  "from ai_worker.core.config import Config; assert Config.model_fields['PROTECTED_RETRIEVAL_ENABLED'].default is False; import ai_worker.main; import ai_worker.adapters.postgresql_protected_retrieval_control; import ai_worker.adapters.sqlalchemy_source_snapshot_repository; import ai_worker.admin.catalog_writer; print('worker-image-protected-off-import-ok')"

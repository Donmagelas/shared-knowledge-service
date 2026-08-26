#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

usage() {
    cat <<'EOF'
用法：
  ./scripts/create-knowledge-base.sh \
    --tenant TENANT_ID \
    --embedding-url URL \
    --embedding-model MODEL_ID \
    [--embedding-dimension DIMENSION] \
    [--enable-rerank --rerank-url URL --rerank-model MODEL_ID]

Embedding/Rerank API Key 默认从终端隐藏输入；自动化时可分别设置
EMBEDDING_API_KEY 和 RERANK_API_KEY。Key 不作为命令行参数传递。
可用 IDEMPOTENCY_KEY 固定创建请求的幂等键。
EOF
}

tenant_id=""
embedding_url=""
embedding_model=""
embedding_dimension=""
rerank_enabled=false
rerank_url=""
rerank_model=""

while (($#)); do
    case "$1" in
        --tenant) tenant_id="${2-}"; shift 2 ;;
        --embedding-url) embedding_url="${2-}"; shift 2 ;;
        --embedding-model) embedding_model="${2-}"; shift 2 ;;
        --embedding-dimension) embedding_dimension="${2-}"; shift 2 ;;
        --enable-rerank) rerank_enabled=true; shift ;;
        --rerank-url) rerank_url="${2-}"; shift 2 ;;
        --rerank-model) rerank_model="${2-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "${tenant_id}" || -z "${embedding_url}" || -z "${embedding_model}" ]]; then
    echo "缺少必填的租户或 Embedding 参数。" >&2
    usage >&2
    exit 2
fi
if [[ -n "${embedding_dimension}" && ! "${embedding_dimension}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Embedding dimension 必须是正整数。" >&2
    exit 2
fi
if [[ "${rerank_enabled}" == true && ( -z "${rerank_url}" || -z "${rerank_model}" ) ]]; then
    echo "启用 Rerank 时必须提供 --rerank-url 和 --rerank-model。" >&2
    exit 2
fi

embedding_api_key="${EMBEDDING_API_KEY:-}"
if [[ -z "${embedding_api_key}" ]]; then
    read -r -s -p "Embedding API Key: " embedding_api_key
    echo
fi
if [[ -z "${embedding_api_key}" ]]; then
    echo "Embedding API Key 不能为空。" >&2
    exit 2
fi

rerank_api_key=""
if [[ "${rerank_enabled}" == true ]]; then
    rerank_api_key="${RERANK_API_KEY:-}"
    if [[ -z "${rerank_api_key}" ]]; then
        read -r -s -p "Rerank API Key: " rerank_api_key
        echo
    fi
    if [[ -z "${rerank_api_key}" ]]; then
        echo "Rerank API Key 不能为空。" >&2
        exit 2
    fi
fi

idempotency_key="${IDEMPOTENCY_KEY:-create-$(date +%s)-$$}"

# Secret 只经 stdin 进入容器；Python 在容器内组装 JSON，避免 Key 出现在进程参数和日志中。
printf '%s\n' \
    "${tenant_id}" \
    "${embedding_url}" \
    "${embedding_api_key}" \
    "${embedding_model}" \
    "${embedding_dimension:--}" \
    "${rerank_enabled}" \
    "${rerank_url:--}" \
    "${rerank_api_key:--}" \
    "${rerank_model:--}" \
    "${idempotency_key}" | \
docker compose exec -T knowledge-ogx /app/.venv/bin/python -c '
import json
import os
import sys
import urllib.error
import urllib.request

values = sys.stdin.read().splitlines()
if len(values) != 10:
    raise SystemExit("KnowledgeBase 创建输入不完整")
tenant, emb_url, emb_key, emb_model, emb_dim, rerank_enabled, rerank_url, rerank_key, rerank_model, key = values
payload = {
    "tenant_id": tenant,
    "embedding": {
        "base_url": emb_url,
        "api_key": emb_key,
        "model_id": emb_model,
    },
}
if emb_dim != "-":
    payload["embedding"]["dimension"] = int(emb_dim)
if rerank_enabled == "true":
    payload["rerank"] = {
        "base_url": rerank_url,
        "api_key": rerank_key,
        "model_id": rerank_model,
    }

request = urllib.request.Request(
    "http://127.0.0.1:8321/knowledge/v1/knowledge-bases",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": "Bearer " + os.environ["KNOWLEDGE_RUNTIME_TOKEN"],
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    },
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as error:
    print(error.read().decode("utf-8"), file=sys.stderr)
    raise SystemExit(1) from None
'

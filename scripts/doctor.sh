#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker。" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "未找到 Docker Compose 插件。" >&2
    exit 1
fi
if [[ ! -f .env ]]; then
    echo "缺少 .env，请先运行 ./scripts/init-env.sh。" >&2
    exit 1
fi
if grep -qE '^(KNOWLEDGE_RUNTIME_TOKEN|KNOWLEDGE_ADMIN_TOKEN|KNOWLEDGE_CREDENTIAL_MASTER_KEY|POSTGRES_PASSWORD)=.*(local-only|change-me)' .env; then
    echo ".env 仍包含示例凭证，请运行 init-env.sh 生成新配置或手动替换。" >&2
    exit 1
fi

docker compose config --quiet

timeout_seconds="${DOCTOR_TIMEOUT_SECONDS:-180}"
deadline=$((SECONDS + timeout_seconds))
services=(postgres qdrant knowledge-ogx)

while ((SECONDS < deadline)); do
    all_healthy=true
    for service in "${services[@]}"; do
        container_id="$(docker compose ps -q "${service}")"
        if [[ -z "${container_id}" ]]; then
            all_healthy=false
            continue
        fi
        health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
        if [[ "${health}" != "healthy" ]]; then
            all_healthy=false
        fi
    done
    if [[ "${all_healthy}" == true ]]; then
        break
    fi
    sleep 2
done

for service in "${services[@]}"; do
    container_id="$(docker compose ps -q "${service}")"
    if [[ -z "${container_id}" ]]; then
        echo "${service}: 未启动" >&2
        exit 1
    fi
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    echo "${service}: ${health}"
    if [[ "${health}" != "healthy" ]]; then
        echo "${service} 未在 ${timeout_seconds} 秒内就绪。" >&2
        docker compose logs --tail 80 "${service}" >&2
        exit 1
    fi
done

# 从容器内部验证真实 HTTP 路由，避免只检查进程或端口。
docker compose exec -T knowledge-ogx /app/.venv/bin/python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8321/v1/health', timeout=3).read()"

published_port="$(awk -F= '$1 == "OGX_HOST_PORT" {print $2; exit}' .env)"
echo "统一知识库服务可用。Knowledge API 端口：${published_port:-8321}"

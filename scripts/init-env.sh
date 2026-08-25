#!/usr/bin/env bash

set -Eeuo pipefail

# 始终从源码仓库根目录操作，避免调用位置改变 Compose 项目。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

if [[ -e .env ]]; then
    echo "已存在 .env；为避免覆盖本地配置和 Secret，本脚本不会继续。" >&2
    exit 1
fi
if [[ ! -f .env.example ]]; then
    echo "源码仓库缺少 .env.example。" >&2
    exit 1
fi

random_hex() {
    # 只生成十六进制字符，确保结果可以安全写入 Compose .env。
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

runtime_token="$(random_hex)"
admin_token="$(random_hex)"
master_key="$(random_hex)"
postgres_password="$(random_hex)"

cp .env.example .env
sed -i \
    -e "s|^KNOWLEDGE_RUNTIME_TOKEN=.*|KNOWLEDGE_RUNTIME_TOKEN=${runtime_token}|" \
    -e "s|^KNOWLEDGE_ADMIN_TOKEN=.*|KNOWLEDGE_ADMIN_TOKEN=${admin_token}|" \
    -e "s|^KNOWLEDGE_CREDENTIAL_MASTER_KEY=.*|KNOWLEDGE_CREDENTIAL_MASTER_KEY=${master_key}|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${postgres_password}|" \
    .env
chmod 600 .env

if grep -qE '^(KNOWLEDGE_RUNTIME_TOKEN|KNOWLEDGE_ADMIN_TOKEN|KNOWLEDGE_CREDENTIAL_MASTER_KEY|POSTGRES_PASSWORD)=.*(local-only|change-me)' .env; then
    echo "示例凭证替换不完整，已停止。" >&2
    exit 1
fi

echo "已从 .env.example 生成 .env，并设置为仅当前用户可读写。"
echo "下一步：检查 .env 后运行 ./scripts/build-production-image.sh"

#!/usr/bin/env bash

set -Eeuo pipefail

# 始终从源码仓库根目录操作。这里不使用 GNU 专属参数，兼容 macOS BSD 工具。
repo_root="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"
umask 077

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

temporary_env=".env.tmp.$$"
trap 'rm -f "${temporary_env}"' EXIT

# macOS 的 BSD sed 与 GNU sed 对 ``-i`` 的参数要求不同；使用 POSIX awk
# 一次性生成目标文件，避免按操作系统维护两套替换命令。
awk \
    -v runtime_token="${runtime_token}" \
    -v admin_token="${admin_token}" \
    -v master_key="${master_key}" \
    -v postgres_password="${postgres_password}" '
    /^KNOWLEDGE_RUNTIME_TOKEN=/ {
        print "KNOWLEDGE_RUNTIME_TOKEN=" runtime_token
        next
    }
    /^KNOWLEDGE_ADMIN_TOKEN=/ {
        print "KNOWLEDGE_ADMIN_TOKEN=" admin_token
        next
    }
    /^KNOWLEDGE_CREDENTIAL_MASTER_KEY=/ {
        print "KNOWLEDGE_CREDENTIAL_MASTER_KEY=" master_key
        next
    }
    /^POSTGRES_PASSWORD=/ {
        print "POSTGRES_PASSWORD=" postgres_password
        next
    }
    { print }
' .env.example >"${temporary_env}"
mv "${temporary_env}" .env
trap - EXIT
chmod 600 .env

if grep -qE '^(KNOWLEDGE_RUNTIME_TOKEN|KNOWLEDGE_ADMIN_TOKEN|KNOWLEDGE_CREDENTIAL_MASTER_KEY|POSTGRES_PASSWORD)=.*(local-only|change-me)' .env; then
    echo "示例凭证替换不完整，已停止。" >&2
    exit 1
fi

echo "已从 .env.example 生成 .env，并设置为仅当前用户可读写。"
echo "下一步：检查 .env 后运行 ./scripts/build-production-image.sh"

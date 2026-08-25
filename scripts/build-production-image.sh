#!/usr/bin/env bash

set -Eeuo pipefail

# 始终从仓库根目录执行 Compose；路径解析兼容 macOS 自带 BSD 工具。
repo_root="$(CDPATH= cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

for command_name in curl docker; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "缺少构建依赖：${command_name}" >&2
        exit 1
    fi
done

# 复用 Compose 对 .env 的解析结果，同时让当前 Shell 中显式设置的变量保持最高优先级。
compose_environment="$(docker compose -f compose.yaml config --environment)"

resolved_value() {
    local default_value="$2"
    local environment_value
    local compose_value

    # Bash 3.2 不支持带默认值的间接参数展开 ``${!name-}``；macOS 自带
    # ``printenv``，并且调用脚本时显式传入的构建变量本来就需要 export。
    environment_value="$(printenv "$1" 2>/dev/null || true)"

    if [[ -n "${environment_value}" ]]; then
        printf '%s' "${environment_value}"
        return
    fi

    compose_value="$(awk -F= -v name="$1" '$1 == name {sub(/^[^=]*=/, ""); print; exit}' <<<"${compose_environment}")"
    printf '%s' "${compose_value:-${default_value}}"
}

preferred_endpoint="$(resolved_value HF_ENDPOINT https://huggingface.co)"
fallback_endpoint="$(resolved_value HF_FALLBACK_ENDPOINT https://hf-mirror.com)"
tokenizer_model="$(resolved_value DOCLING_TOKENIZER_MODEL sentence-transformers/all-MiniLM-L6-v2)"
tokenizer_revision="$(resolved_value DOCLING_TOKENIZER_REVISION 1110a243fdf4706b3f48f1d95db1a4f5529b4d41)"
layout_revision="$(resolved_value DOCLING_LAYOUT_MODEL_REVISION 8f39ad3c0b4c58e9c2d2c84a38465abf757272d8)"
table_revision="$(resolved_value DOCLING_TABLE_MODEL_REVISION fc0f2d45e2218ea24bce5045f58a389aed16dc23)"
connect_timeout="$(resolved_value HF_PROBE_CONNECT_TIMEOUT_SECONDS 5)"
request_timeout="$(resolved_value HF_PROBE_MAX_TIME_SECONDS 15)"

normalize_endpoint() {
    printf '%s' "${1%/}"
}

probe_file() {
    local endpoint="$1"
    local repository="$2"
    local revision="$3"
    local filename="$4"
    local probe_url

    # 探测固定 revision 的真实模型文件，而不是只检查站点首页。
    probe_url="${endpoint}/${repository}/resolve/${revision}/${filename}"
    echo "检查 Hugging Face 模型端点：${probe_url}"
    curl \
        --fail \
        --silent \
        --show-error \
        --location \
        --range 0-0 \
        --connect-timeout "${connect_timeout}" \
        --max-time "${request_timeout}" \
        --output /dev/null \
        "${probe_url}"
}

probe_endpoint() {
    local endpoint

    endpoint="$(normalize_endpoint "$1")"
    # 三组构建时资产必须来自同一可达端点，避免进入耗时构建后才发现部分仓库不可用。
    probe_file "${endpoint}" "${tokenizer_model}" "${tokenizer_revision}" "config.json" || return 1
    probe_file "${endpoint}" "docling-project/docling-layout-heron" "${layout_revision}" "config.json" || return 1
    probe_file "${endpoint}" "docling-project/docling-models" "${table_revision}" "model_artifacts/tableformer/accurate/tm_config.json"
}

preferred_endpoint="$(normalize_endpoint "${preferred_endpoint}")"
fallback_endpoint="$(normalize_endpoint "${fallback_endpoint}")"

if probe_endpoint "${preferred_endpoint}"; then
    selected_endpoint="${preferred_endpoint}"
    echo "官方或显式配置的 Hugging Face 端点可用。"
elif [[ "${fallback_endpoint}" != "${preferred_endpoint}" ]]; then
    echo "首选 Hugging Face 端点不可达，尝试兼容镜像：${fallback_endpoint}" >&2
    if probe_endpoint "${fallback_endpoint}"; then
        selected_endpoint="${fallback_endpoint}"
        echo "兼容镜像可用。"
    else
        echo "首选端点与兼容镜像均不可达，停止构建。" >&2
        exit 1
    fi
else
    echo "Hugging Face 端点不可达，且没有不同的兼容镜像可供回退。" >&2
    exit 1
fi

export HF_ENDPOINT="${selected_endpoint}"
echo "本次构建使用 HF_ENDPOINT=${HF_ENDPOINT}"

# 额外参数原样传给 docker compose build，例如 --no-cache。
exec docker compose -f compose.yaml build "$@" knowledge-ogx

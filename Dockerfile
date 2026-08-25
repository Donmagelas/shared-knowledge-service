FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG DOCLING_LAYOUT_MODEL_REVISION=8f39ad3c0b4c58e9c2d2c84a38465abf757272d8
ARG DOCLING_TABLE_MODEL_REVISION=fc0f2d45e2218ea24bce5045f58a389aed16dc23
ARG DOCLING_TOKENIZER_MODEL=sentence-transformers/all-MiniLM-L6-v2
ARG DOCLING_TOKENIZER_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41
ARG HF_ENDPOINT=https://huggingface.co
ARG PYPI_SIMPLE_URL=https://pypi.org/simple
ARG PYPI_FILES_URL=https://files.pythonhosted.org/packages

ENV DOCLING_TOKENIZER_MODEL=${DOCLING_TOKENIZER_MODEL} \
    DOCLING_TOKENIZER_REVISION=${DOCLING_TOKENIZER_REVISION} \
    DOCLING_LAYOUT_MODEL_REVISION=${DOCLING_LAYOUT_MODEL_REVISION} \
    DOCLING_TABLE_MODEL_REVISION=${DOCLING_TABLE_MODEL_REVISION} \
    DOCLING_ARTIFACTS_PATH=/app/.cache/docling/models \
    HF_ENDPOINT=${HF_ENDPOINT} \
    HF_HOME=/app/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

# Docling 的 OpenCV 运行时需要 libGL；镜像源可按部署网络覆盖，但包仍由 Debian 签名校验。
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

# 可选镜像只改下载地址；版本与文件哈希仍由原始 uv.lock 校验。
# HybridChunker 的默认 tokenizer 也在构建时固定，首次导入不允许在线下载。
RUN --mount=type=cache,target=/root/.cache/uv \
    cp uv.lock /tmp/uv.lock.source \
    && sed -i \
        -e "s|https://pypi.org/simple|${PYPI_SIMPLE_URL}|g" \
        -e "s|https://files.pythonhosted.org/packages|${PYPI_FILES_URL}|g" \
        uv.lock \
    && uv sync --frozen --no-dev --no-install-project \
    && mv /tmp/uv.lock.source uv.lock \
    && mkdir -p /app/.cache/docling/models /app/.cache/huggingface /app/.cache/tiktoken /data/files \
    && .venv/bin/python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && .venv/bin/python -c "import os; from pathlib import Path; from huggingface_hub import hf_hub_download; from transformers import AutoTokenizer; model=os.environ['DOCLING_TOKENIZER_MODEL']; revision=os.environ['DOCLING_TOKENIZER_REVISION']; AutoTokenizer.from_pretrained(model, revision=revision); hf_hub_download(repo_id=model, filename='sentence_bert_config.json', revision=revision); repo=Path(os.environ['HF_HOME'])/'hub'/f'models--{model.replace(chr(47), chr(45)+chr(45))}'; (repo/'refs').mkdir(parents=True, exist_ok=True); (repo/'refs'/'main').write_text(revision, encoding='utf-8')" \
    && .venv/bin/python -c "import os; from pathlib import Path; from huggingface_hub import snapshot_download; root=Path(os.environ['DOCLING_ARTIFACTS_PATH']); snapshot_download(repo_id='docling-project/docling-layout-heron', revision=os.environ['DOCLING_LAYOUT_MODEL_REVISION'], local_dir=root/'docling-project--docling-layout-heron'); snapshot_download(repo_id='docling-project/docling-models', revision=os.environ['DOCLING_TABLE_MODEL_REVISION'], allow_patterns=['model_artifacts/tableformer/accurate/*'], local_dir=root/'docling-project--docling-models')" \
    && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -c "from docling.chunking import HybridChunker; HybridChunker()" \
    && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -c "from docling.datamodel.base_models import InputFormat; from docling.datamodel.pipeline_options import PdfPipelineOptions; from docling.document_converter import DocumentConverter, PdfFormatOption; converter=DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=PdfPipelineOptions(do_ocr=False))}); converter.initialize_pipeline(InputFormat.PDF)" \
    && groupadd --gid 10001 knowledge \
    && useradd --uid 10001 --gid knowledge --no-create-home --shell /usr/sbin/nologin knowledge \
    && chown -R knowledge:knowledge /app /data/files

# README 只参与本项目打包，不应让文档修改使第三方依赖和 Docling 模型层失效。
COPY --chown=knowledge:knowledge README.md ./README.md
COPY --chown=knowledge:knowledge src ./src

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

USER knowledge

# 业务代码独立成轻量缓存层；修改 Provider/API 时不重新安装依赖或下载 Docling 模型。
RUN --mount=type=cache,target=/tmp/uv-cache,uid=10001,gid=10001 \
    UV_CACHE_DIR=/tmp/uv-cache uv sync --frozen --no-dev --no-editable

COPY --chown=knowledge:knowledge config ./config

EXPOSE 8321

ENTRYPOINT ["/app/.venv/bin/ogx", "run", "/app/config/ogx.yaml", "--insecure"]

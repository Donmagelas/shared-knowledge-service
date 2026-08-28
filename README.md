# Stella × Cherry Studio 企业版统一知识库服务

本仓库提供一套独立部署的知识库基础设施。Stella 与 Cherry Studio 企业版通过稳定的 Knowledge API 共用上传、原文件存储、解析、切块、向量化、检索、任务状态和技术对象生命周期；用户、组织、权限、业务知识库展示与挂载关系仍由两侧产品维护。

当前技术基线：

- OGX `1.3.0` 最小 Distribution
- Docling `2.121.0` + Docling `HybridChunker`（最终上限 1000 tokens、相邻 overlap 最多 200 tokens）
- Qdrant Server `1.18.2` / Client `1.18.0`
- PostgreSQL `17.10`
- Python `3.12`、FastAPI、KnowledgeBase 感知 Inference Provider

## 源码构建快速启动

本项目以完整源码仓库交付，不提供固化业务代码的预构建 Knowledge 镜像。使用方可以直接修改 Provider、API、配置、依赖或 Dockerfile，再在自己的环境中构建三服务镜像组合。

已验证的宿主机环境为 Linux amd64、Docker Engine、Docker Compose v2 和 `curl`，不要求安装 Python 或 uv。macOS 使用 Docker Desktop；交付脚本兼容系统自带 Bash 3.2 与 BSD 工具。Apple Silicon 的基础镜像和 Python 依赖已确认存在 arm64 版本，但完整的构建、导入与检索链路仍需在真机上验证后才视为正式支持：

```bash
git clone https://github.com/Donmagelas/shared-knowledge-service.git
cd shared-knowledge-service

./scripts/init-env.sh
# 检查 .env 中的模型访问白名单、S3 等部署选项。
./scripts/build-production-image.sh
docker compose up -d
./scripts/doctor.sh
```

服务启动后可运行 `./scripts/create-knowledge-base.sh --help` 查看 KnowledgeBase 创建与 Embedding/Rerank 配置方式；模型 Key 默认通过终端隐藏输入。

需要人工联调 API 时，打开 `http://127.0.0.1:8321/api-docs` 使用 Scalar 页面；页面读取
`/knowledge-openapi.json`，只展示稳定的 `/knowledge/v1/*` 产品接口，不展示 OGX 原生内部接口。Token 由测试人员在页面中临时填写，服务不会把 Runtime/Admin Token 写入 OpenAPI。

首次构建会在 Docker 中还原锁定的 Python 依赖，并下载固定 revision 的 Docling tokenizer、layout 和 TableFormer 模型；耗时和网络要求高于拉取成品镜像。后续只修改 Provider/API 源码时，Docker 会复用依赖和模型缓存层。

PostgreSQL 与 Qdrant 继续使用固定版本的官方镜像；Knowledge 镜像始终从当前工作区源码构建。默认只在 `127.0.0.1:8321` 暴露 Knowledge API，三个 Docker named volume 保存 PostgreSQL、Qdrant 和本地原文件。

专题文档：

- [OGX 方案与 Haystack 方案详情](docs/01-ogx-and-haystack-solutions.md)
- [当前 OGX 方案：改造、能力、资源与优化项](docs/02-ogx-current-implementation.md)
- [Knowledge API Reference 与产品接入](docs/03-api-reference-and-product-integration.md)

详细设计见 [solution.md](docs/changes/shared-knowledge-service/solution.md)，两侧映射见 [product-integration.md](docs/product-integration.md)，开发和运行排障见 [deployment-troubleshooting.md](docs/deployment-troubleshooting.md)。

## 架构与部署组件

生产 Compose 只有三个服务：

| 服务 | 作用 |
| --- | --- |
| `knowledge-ogx` | Knowledge API、OGX Files/FileBatch、Docling Worker、HybridChunker、自定义 Qdrant Provider、KnowledgeBase 感知 Embedding/Rerank Provider |
| `postgres` | OGX 对象、任务、幂等记录、文件技术状态和加密后的 KnowledgeBase 模型凭证 |
| `qdrant` | 每租户一个物理 Collection；每个 Embedding 模型与维度组合对应一个动态 Named Vector，逻辑 KnowledgeBase 通过 Payload 隔离 |

原文件可选择 `inline::localfs` 本地持久卷或 `remote::s3` S3-compatible 后端，外部 API 不变。S3 是连接已有对象存储的部署选项，本仓库不会额外启动对象存储服务。

导入链路：

```text
POST /knowledge/v1/ingest 或 POST /knowledge/v1/ingest/batch
  → 批量请求按文件拆成独立单文件提交
  → OGX File 持久化
  → 单文件 FileBatch
  → Docling 解析
  → Docling HybridChunker（800-token 新内容预算 + 上一 Chunk 最多 200-token overlap）
  → 当前 KnowledgeBase 的远程 Embedding
  → Qdrant 对应 Named Vector + 共享 multilingual BM25
```

检索链路：

```text
POST /knowledge/v1/search
  → knowledge_base_ids + 产品 Filter
  → 每个 KnowledgeBase 独立执行 Dense / BM25 / Hybrid RRF
  → 每个 KnowledgeBase 可选独立远程 Rerank
  → 多 KnowledgeBase 等权外层 RRF（单库不做外层融合）
  → 稳定 SearchHit[]
```

## 已验证的文件类型

当前固定镜像已通过七类、八个非 OCR 样本的统一 API 端到端验证：

| 类型 | 扩展名 | 样本覆盖 |
| --- | --- | --- |
| Markdown / 纯文本 | `.md` / `.txt` | 标题、列表、表格与普通段落 |
| HTML | `.html` | 标题层级、列表和原生表格 |
| 数字 PDF | `.pdf` | 两页可提取文本、表格和页眉页脚 |
| Word | `.docx` | 两页、标题层级、列表、表格和页眉页脚 |
| PowerPoint | `.pptx` | 三页、时间线和原生表格 |
| Excel | `.xlsx` | 两个工作表、公式、原生表格、日期和百分比 |
| CSV | `.csv` | 中文表头和多行数据 |

每个样本都完成了异步 Ingest、Operation 终态、Docling 解析与 `HybridChunker`、Qdrant 写入、BM25 / Dense / Hybrid 检索和结果范围过滤。测试使用确定性 Embedding Stub，因此只证明文件处理与检索协议链路可用，不代表真实语义召回质量，也不表示样本中每种版式元素都已证明完全保真。

当前 `do_ocr=false` 且未配置 VLM，所以不承诺扫描 PDF、纯图片、密码保护/损坏文件、旧版 `.doc/.ppt/.xls`、动态 JavaScript 页面或复杂嵌入对象。以上“已验证”也只针对当前样本，不等于 Docling 所有理论格式和边界情况都已获得产品承诺。

## 配置原则

- Embedding 和 Rerank 都属于 KnowledgeBase；同一租户的不同 KnowledgeBase 可以使用不同模型、维度、URL 和 Key。
- 服务不发现、列举或推荐模型，只接受调用方提交的 `base_url / api_key / model_id / dimension`。
- 空 KnowledgeBase 可以修改 Embedding 模型和维度；首次 Ingest 后模型和维度锁定，URL 与 Key 仍可轮换。当前不提供全量重新向量化。
- Rerank 使用独立 URL、Key、模型和开关，可按 KnowledgeBase 随时修改；只增强 `hybrid`，单个知识库的 Rerank 失败时退回该库的 Qdrant RRF 排名。
- 一个 Search 可以包含同一租户的多个 KnowledgeBase；每个库先独立检索/重排，再做等权外层 RRF。跨租户请求直接拒绝，不做跨 Collection 融合。
- 需要高性能过滤的业务属性由部署方在 `config/ogx.yaml` 的 `payload_indexes` 中声明类型。
- 切块规则是部署级统一配置；修改 `1000/200` 规则只影响新导入文件，已有文件需要重新导入和重新 Embedding。

## 服务间认证

Knowledge API 使用两个静态 Bearer Token：

| Token | 权限 |
| --- | --- |
| Runtime Token | KnowledgeBase、Ingest、Operation、File 和 Search 接口 |
| Admin Token | Runtime 全部能力，以及 KnowledgeBase Embedding/Rerank 配置 |

OGX 原生接口只接受 Admin Token，用于运维诊断，产品正常接入只调用 `/knowledge/v1/*`。Token 和凭证必须由部署 Secret 注入，不得提交到仓库。

## API 概览

### KnowledgeBase

```http
POST   /knowledge/v1/knowledge-bases
GET    /knowledge/v1/knowledge-bases/{knowledge_base_id}
GET    /knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config
PUT    /knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config
PUT    /knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config
DELETE /knowledge/v1/knowledge-bases/{knowledge_base_id}
```

创建技术 KnowledgeBase：

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/knowledge-bases \
  -H "Authorization: Bearer $KNOWLEDGE_RUNTIME_TOKEN" \
  -H 'Idempotency-Key: create-company-kb-1' \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "tenant-a",
    "embedding": {
      "base_url": "https://model.example/v1",
      "api_key": "replace-with-embedding-secret",
      "model_id": "embedding-model-id",
      "dimension": 1024
    },
    "rerank": {
      "base_url": "https://model.example/v1",
      "api_key": "replace-with-rerank-secret",
      "model_id": "rerank-model-id"
    }
  }'
```

`dimension` 可省略，由服务通过 Embedding 探针确定；`rerank` 整段可省略。创建完成后，查询配置的响应不会返回 API Key。业务名称、公司/部门/产品归属和挂载关系不进入该请求；企业版在自己的数据库中保存业务对象与返回 `knowledge_base_id` 的映射。Stella 通常只在部署初始化时创建一个隐藏 KnowledgeBase。

空库可以通过两个 `PUT` 接口更换 Embedding；一旦导入文件，只允许保持模型 ID 与维度不变并轮换 URL/Key。Rerank 配置不影响持久索引，可随时启用、修改或关闭。

### 异步 Ingest 与 Operation

```http
POST /knowledge/v1/ingest
POST /knowledge/v1/ingest/batch
GET  /knowledge/v1/operations/{operation_id}
POST /knowledge/v1/operations/{operation_id}/retry
```

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/ingest \
  -H "Authorization: Bearer $KNOWLEDGE_RUNTIME_TOKEN" \
  -H 'Idempotency-Key: ingest-file-1' \
  -F 'file=@./knowledge.md;type=text/markdown' \
  -F 'knowledge_base_id=vs_example' \
  -F 'attributes={"department_id":"product-a"}'
```

首次可靠接受返回 HTTP `202`；相同幂等请求重放返回 HTTP `200` 和原 Operation。`operation_id` 在服务内全局唯一，查询和重试不要求调用方重复传入 KnowledgeBase；响应会回显对应的 `knowledge_base_id` 和 `file_id`。状态统一为 `processing / completed / failed / cancelled`。最终失败且原文件仍存在时，可调用 Retry 接口复用原文件创建唯一的子 Operation。

批量接口使用重复的 `files` 字段、一个 `knowledge_base_id` 和一份共用 `attributes`。它不创建公开 Batch 对象，而是按请求顺序返回多个独立 File/Operation；调用方继续分别查询和重试每个 Operation。默认限制为单文件 `100 MiB`、单批 20 个文件、单批总计 `500 MiB`，均可通过 `.env` 配置：

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/ingest/batch \
  -H "Authorization: Bearer $KNOWLEDGE_RUNTIME_TOKEN" \
  -H 'Idempotency-Key: ingest-product-a-batch-1' \
  -F 'files=@./policy.pdf;type=application/pdf' \
  -F 'files=@./manual.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document' \
  -F 'knowledge_base_id=vs_example' \
  -F 'attributes={"department_id":"product-a"}'
```

### File

```http
POST   /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query
GET    /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}
GET    /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}/download
DELETE /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}
```

下载接口在服务端校验 `file_id` 属于路径中的 KnowledgeBase，随后返回上传时保存的原始字节；它不返回 Docling 解析结果或 Chunk。产品后端先完成用户权限检查，再代理该文件流。V1 仍不提供替换、主动取消任务、用户管理或知识库挂载 API。超过保留期的未提交、最终失败和孤儿原文件由 Knowledge 进程内部自动清理，不增加公开清理接口。

### Search

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/search \
  -H "Authorization: Bearer $KNOWLEDGE_RUNTIME_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "退款申请需要什么材料",
    "knowledge_base_ids": ["vs_company", "vs_product_a"],
    "filters": {"type":"eq", "key":"department_id", "value":"product-a"},
    "mode": "hybrid",
    "limit": 10
  }'
```

`mode` 支持 `hybrid / dense / bm25`。服务会为每个 `knowledge_base_id` 分别执行一次带精确 `vector_store_id` 条件的查询，并与产品 Filter 做 AND；调用方不能通过 Filter 覆盖保留字段。多库查询在各库完成本地 RRF 与可选 Rerank 后做等权外层 RRF。每个命中固定返回 `knowledge_base_id / file_id / filename / chunk_id / content / locator / score / attributes`。

## 修改与重新构建

```bash
./scripts/build-production-image.sh
docker compose up -d
./scripts/doctor.sh
```

构建默认使用固定 revision 的 Docling tokenizer、layout 和 Accurate TableFormer 资产，并在镜像中离线初始化 PDF Pipeline。默认 Hugging Face 端点为兼容镜像；使用方也可改为自己的镜像或内部制品库。运行时不会临时下载模型资产。

本机 amd64 历史测量中，镜像约 `1.08 GB`。七类文件连续验证时，OGX 冷态约 `1.07 GiB`；OGX `1.3.0` 默认的两个 Worker 都懒加载 Docling 模型后，稳态约 `2.2 GiB`，瞬时峰值约 `2.46 GiB`。这些数字使用确定性模型 Stub 和小型合成文件，只用于量级参考，不是生产配额或 SLA。

## 验证

```bash
uv sync --frozen
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run pytest -q tests/unit

QDRANT_INTEGRATION_URL=http://127.0.0.1:6333 \
  uv run pytest -q tests/integration

KNOWLEDGE_E2E_URL=http://127.0.0.1:8321 \
KNOWLEDGE_FAILURE_E2E=1 \
  uv run pytest -q tests/e2e/test_knowledge_api_contract.py
```

在独立 E2E Compose 项目中重跑文件格式矩阵：

```bash
COMPOSE_PROJECT_NAME=document-format-validation \
OGX_HOST_PORT=18321 \
QDRANT_HOST_PORT=16333 \
  docker compose -f compose.yaml -f compose.e2e.yaml up -d --build --wait --wait-timeout 300

COMPOSE_PROJECT_NAME=document-format-validation \
  uv run python tests/evaluation/validate_document_formats.py \
    --base-url http://127.0.0.1:18321

COMPOSE_PROJECT_NAME=document-format-validation \
OGX_HOST_PORT=18321 \
QDRANT_HOST_PORT=16333 \
  docker compose -f compose.yaml -f compose.e2e.yaml down -v
```

E2E 使用 `compose.e2e.yaml` 中的确定性 Embedding/Rerank Stub；该 Stub 不属于生产部署。真实业务语料评测与模型选型应单独执行，不能用确定性 Stub 的结果代替效果结论。

## 许可证

本项目使用 [MIT License](LICENSE)。

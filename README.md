# Stella × Cherry Studio 企业版统一知识库服务

本仓库实现独立部署的统一知识库基础设施。当前基线是 OGX `v1.3.0` 最小 Distribution、自定义 Qdrant Provider、PostgreSQL 和 Qdrant。

方案与实施顺序分别见：

- [方案设计](docs/changes/shared-knowledge-service/solution.md)
- [实施计划](docs/changes/shared-knowledge-service/implementation.md)
- [产品接入契约](docs/product-integration.md)
- [部署问题与解决方法](docs/deployment-troubleshooting.md)

## 当前进度

OGX 原生 MVP 与统一产品 API 已贯通以下链路：

```text
File 上传 → Docling 解析 → HybridChunker → Embedding
          → Dense + Qdrant 原生 multilingual BM25
          → Payload Filter → Qdrant 原生 RRF
          → 可选远程 Rerank → OGX Search
```

共享 Collection、逻辑 VectorStore 隔离、同一 File 多重挂载、scoped delete、异步失败重试、Docling Worker 租约回收和三组件重启恢复均有自动化测试。`/knowledge/v1/ingest` 已使用单文件 OGX FileBatch 异步提交，`/knowledge/v1/search` 已实现单个或多个逻辑知识库的一次 Qdrant 查询，并可按部署开关通过 OGX `Inference.rerank` 调用远程模型。真实 Embedding 与 Stella/企业版真实项目文档的第一轮评测已完成；当前仍未完成的是更大业务语料复核、认证、S3 和备份等生产化工作。

## 本地环境

- Python `3.12`
- uv `0.12.5`
- Docker Engine
- Docker Compose plugin `5.5.0`
- Qdrant Server `1.18.2`
- Qdrant Client `1.18.0`
- PostgreSQL `17.10`

首次生成或更新锁文件：

```bash
uv lock
uv sync --frozen
```

后续只使用冻结依赖还原环境：

```bash
uv sync --frozen
```

## Embedding 前置探针

复制 `.env.example` 为本地 `.env`，通过环境变量提供实际配置。`.env` 已被 Git 忽略，不得提交真实 Endpoint 或 Token。

| 配置项 | 含义 |
| --- | --- |
| `EMBEDDING_BASE_URL` | OpenAI-compatible 模型服务地址 |
| `EMBEDDING_API_KEY` | 模型服务凭证 |
| `EMBEDDING_MODEL` | 当前部署使用的 Embedding 模型 ID |
| `EMBEDDING_DIMENSION` | 该模型实际返回的向量维度 |
| `EMBEDDING_PROBE_BATCH_SIZE` | 前置探针验证的请求批量 |
| `EMBEDDING_TIMEOUT_SECONDS` | 前置探针超时 |

`EMBEDDING_DIMENSION` 在部署初始化 Collection 时确定，当前方案不支持在该部署内修改。`EMBEDDING_MODEL` 由部署配置选择，不在代码中固定。当前范围不实现已有数据的模型热切换或迁移工具。

```bash
set -a
source .env
set +a
uv run knowledge-embedding-preflight
```

成功时只输出模型、向量维度、返回数量、已验证批量和耗时；不会输出 Token、Endpoint 或响应正文。探针会拒绝实际返回维度与 `EMBEDDING_DIMENSION` 不一致的配置。`EMBEDDING_PROBE_BATCH_SIZE` 仅验证指定批量能够成功，不代表自动发现服务端最大批量。

## 可选远程 Rerank

| 配置项 | 含义 |
| --- | --- |
| `RERANK_ENABLED` | 是否在 Hybrid RRF 候选集后执行远程 Rerank，默认 `false` |
| `RERANK_BASE_URL` | Jina-compatible Rerank 服务的版本化 Base URL |
| `RERANK_API_KEY` | Rerank 服务凭证 |
| `RERANK_MODEL` | 远程模型 ID，MVP 默认 `qwen/qwen3-reranker-0.6b` |
| `RERANK_CANDIDATE_LIMIT` | 送入远程模型的最大 RRF 候选数，默认 `50` |

该开关只增强 `hybrid` 模式；显式 `dense` 和 `bm25` 模式保持各自原始排序。开启后，Qdrant 先返回至多 `RERANK_CANDIDATE_LIMIT` 个 RRF 候选，远程模型再裁剪到 Search 请求的 `limit`。远程调用失败或返回无效索引时会降级到原 RRF Top K，不让可选效果层中断基础检索。

### 真实模型与项目文档评测

2026-08-24 使用 2 份 Stella 实际文档、3 份 Cherry Studio 企业版实际文档和 8 条人工标注中文查询，完成两个逻辑知识库的一次跨库检索。私有文档只在本地读取，没有复制进本仓库。三组单次运行结果如下；它们是 MVP 链路验证数据，不是模型选型、并发压测或最终质量承诺。

| 模型 | 维度 | Dense Top1 | Hybrid Top1 | Hybrid 平均延迟 | 5 文档总导入耗时 | 用途 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `qwen/qwen3-embedding-0.6b` | 1024 | 8/8 | 8/8 | 648 ms | 11.47 s | 本轮测试数据 |
| `qwen/qwen3-embedding-4b` | 2560 | 7/8 | 8/8 | 738 ms | 13.61 s | 本轮测试数据 |
| `qwen/qwen3-embedding-8b` | 4096 | 7/8 | 8/8 | 5514 ms | 35.33 s | 本轮测试数据 |

三组模型下 Qdrant 原生 multilingual BM25 和最终 Hybrid 均为 8/8 Top1、Recall@3 1.0、MRR 1.0。这一轮只用于证明真实 Embedding 服务、不同向量维度和完整检索链路能够工作，不在 MVP 架构阶段选定具体模型。模型将在后续使用更大业务语料，综合效果、延迟和成本另行确定。

评测工具不内置业务内容，通过命令行接收本地文档和相关性标注：

```bash
uv run python tests/evaluation/live_retrieval.py \
  --document 'stella:stella-policy:/path/to/stella-policy.md' \
  --document 'enterprise:enterprise-guide:/path/to/enterprise-guide.md' \
  --query 'stella-policy:Stella 的相关规则是什么？' \
  --query 'enterprise-guide:企业版应如何执行对应操作？'
```

## Qdrant 前置探针

目标 Qdrant 启动后执行：

```bash
uv run knowledge-qdrant-preflight
```

探针会创建一个带 Dense、IDF Sparse 和 Payload 的临时 Collection，执行带相同 Filter 的 Qdrant 原生 RRF 查询，并在结束时删除临时 Collection。

自定义 Provider 按租户创建物理 Qdrant Collection；同一租户内的 OGX `VectorStore` 是逻辑知识库，通过 Point 的 `vector_store_id` Payload 区分。创建 VectorStore 时可在 `metadata.tenant_id` 写入可信租户 ID，Provider 会把它稳定映射为不暴露原始 ID 的 Collection 名。没有 `tenant_id` 时走单租户默认 Collection。所有检索和删除都会由服务端强制带上逻辑知识库范围；一次检索混入不同租户的 VectorStore 会直接返回 422。业务过滤字段写入 `attributes`，需要高性能过滤的字段应在 `config/ogx.yaml` 的 `payload_indexes` 中按部署显式声明。

中文 BM25 使用 Qdrant 1.18.2 服务端 `qdrant/bm25`，文档与查询统一传入 `multilingual` tokenizer 配置，Qdrant 负责分词、TF/长度权重和动态 IDF。Dense 与 BM25 候选由 Qdrant 原生 RRF 融合。生产运行路径不依赖 Jieba、mmh3 或 FastEmbed；切换 BM25 编码协议后必须重建索引。

## 最小三服务环境

复制 `.env.example` 为 `.env` 并填写真实 Embedding 配置。先通过构建脚本生成生产镜像，再启动服务：

```bash
./scripts/build-production-image.sh
docker compose up -d
```

构建脚本会分别探测固定 revision 的 tokenizer、layout 和 TableFormer 配置文件；首选 `HF_ENDPOINT` 不可达时，会明确提示并尝试 `HF_FALLBACK_ENDPOINT`，最后把选中的端点传给 Compose。也可以向脚本传入 `docker compose build` 参数，例如 `./scripts/build-production-image.sh --no-cache`。直接执行 `docker compose build` 仍只使用显式配置的 `HF_ENDPOINT`，不会静默切换下载源。

Compose 只启动三个服务：`knowledge-ogx`、`postgres` 和 `qdrant`。Docling Worker 是 `knowledge-ogx` 容器内由 OGX 管理的独立进程，不是第四个部署服务。

镜像构建会按固定 commit 预置 Docling `HybridChunker` 默认使用的 Hugging Face tokenizer、数字 PDF 所需的 Transformers layout 模型和 Accurate TableFormer 模型。不会下载当前运行路径用不到的 ONNX layout 与 Fast TableFormer 变体。构建阶段会在离线模式下初始化一次 PDF Pipeline；生产导入不会临时访问 Hugging Face。升级 Docling、tokenizer 或模型资产必须显式修改 revision 并重建镜像。

当前 amd64 本地构建镜像约 `1.08 GB`。本机 Docker 实测中，OGX 冷启动稳定后约占 `1.03 GiB` 内存，完成一份单页数字 PDF 导入后约占 `1.54 GiB`；PostgreSQL 约 `58 MiB`，Qdrant 约 `120 MiB`。这些数字使用三维确定性 Embedding Stub，只用于 MVP 量级判断，不是生产资源承诺。

## 产品核心 API

创建逻辑知识库仍复用 OGX 辅助接口 `POST /v1/vector_stores`。Stella 通常只在部署初始化时创建一个隐藏 VectorStore；企业版为公司、部门、产品等显式业务知识库分别创建并保存映射。企业版多租户部署创建时必须写入可信 `tenant_id`：

```bash
curl -X POST http://127.0.0.1:8321/v1/vector_stores \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "产品 A 知识库",
    "metadata": {"tenant_id": "tenant-from-trusted-product-context"}
  }'
```

异步导入先持久化原文件和单文件 FileBatch，再立即返回：

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/ingest \
  -F 'file=@./knowledge.md;type=text/markdown' \
  -F 'knowledge_base_id=vs_example' \
  -F 'attributes={"department_id":"product-a"}'
```

响应使用 HTTP `202`，返回 `operation_id`、`file_id`、`knowledge_base_id` 和 `processing`。调用方轮询任务状态：

```bash
curl http://127.0.0.1:8321/knowledge/v1/knowledge-bases/vs_example/operations/batch_example
```

状态为 `processing / completed / failed / cancelled`。当前实现复用 OGX 持久化 FileBatch；单实例服务重启后会恢复未完成任务，失败文件仍按“删除失败挂载后复用原 File 重试”的协议处理。

检索接口接收产品权限层算出的逻辑知识库列表和 Filter：

```bash
curl -X POST http://127.0.0.1:8321/knowledge/v1/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "退款申请需要什么材料",
    "knowledge_base_ids": ["vs_company", "vs_product_a"],
    "filters": {"type": "eq", "key": "department_id", "value": "product-a"},
    "mode": "hybrid",
    "limit": 10
  }'
```

`mode` 支持 `hybrid`、`dense` 和 `bm25`。同一租户的多个 `knowledge_base_ids` 会转换成一条 `vector_store_id IN [...]` 的 Qdrant 查询；跨租户 ID 不会做跨 Collection 融合，而是被拒绝。返回 `hits` 中每项固定包含 `file_id`、`chunk_id`、`content`、`locator`、`score` 和 `attributes`。关闭 Rerank 时 `score` 是检索引擎分数；开启后 Hybrid 命中的 `score` 是模型返回的 `relevance_score`，调用方不应跨模式或跨部署直接比较其绝对值。

## 开发检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

需要完整 OGX 文件链路时，叠加测试专用 Compose 文件启动确定性 Embedding Stub。该 Stub 是第四个测试容器，不属于生产部署：

```bash
docker compose -f compose.yaml -f compose.e2e.yaml up --build -d

COMPOSE_FILE=compose.yaml:compose.e2e.yaml \
OGX_E2E_URL=http://127.0.0.1:8321 \
OGX_EMBEDDING_STUB_SERVICE=embedding-stub \
uv run pytest tests/e2e/test_ogx_native_file_flow.py
```

重启与失败恢复测试必须显式启用，避免普通测试意外重启开发服务：

```bash
COMPOSE_FILE=compose.yaml:compose.e2e.yaml \
OGX_E2E_URL=http://127.0.0.1:8321 \
OGX_EMBEDDING_STUB_SERVICE=embedding-stub \
OGX_RESTART_E2E=1 \
OGX_FAILURE_E2E=1 \
OGX_ASYNC_RESTART_E2E=1 \
OGX_WORKER_CRASH_E2E=1 \
uv run pytest tests/e2e/test_ogx_native_file_flow.py -m recovery
```

Worker 崩溃用例需要等待 OGX 默认 60 秒租约到期，因此只应在完整验收中启用。

Jieba BM25 与当前 Qdrant 原生 multilingual BM25 的小型工程语料对照可显式执行：

```bash
QDRANT_INTEGRATION_URL=http://127.0.0.1:6333 \
uv run python tests/evaluation/compare_bm25.py
```

该脚本会在唯一命名的临时 Collection 中对两条路线计算 Top1、Recall@3、MRR 和客户端耗时，并在结束后删除临时数据。内置语料只用于发现明显回归，不能代替 Stella 与企业版真实文档上的效果评测。

## 许可证

本项目使用 [MIT License](LICENSE)。

# Stella × Cherry Studio 企业版统一知识库服务实施计划

> 状态：步骤 1～20 已完成；KnowledgeBase 级模型配置、动态 Dense Named Vector 和跨 KB 融合已通过增量验收
>
> 方案依据：[solution.md](./solution.md)
>
> 本次终点：每个 KnowledgeBase 可独立配置 Embedding/Rerank，单 KB 保留本地排名，跨 KB 查询在各库完成本地排序后执行等权外层 RRF

## 1. 目标与方案引用

本计划把已确认的 OGX `v1.3.0` 方案分成四个里程碑：

1. **MVP 可行性验证**：最小 OGX Distribution、自定义 Qdrant Provider、三服务 Compose、原生 OGX API 和完整验收矩阵。
2. **统一产品接口**：MVP 后先确认导入语义，再在同一 OGX 进程中挂载外置 Knowledge API，提供 `ingest` 和多知识库 `search`。
3. **生产化**：认证、备份、S3、可观测性、资源边界和运维文档。
4. **KnowledgeBase 级模型与跨库检索**：把租户级模型配置下沉到逻辑 KnowledgeBase，在同一租户 Collection 中按 `model_id + dimension` 动态维护 Dense Named Vector，并只在跨 KB 查询时执行外层 RRF。

实施必须保持方案中的对象模型、权限边界、Collection 组织、任务可靠性边界和非目标，不在编码阶段重新引入 Haystack、Revision、PgQueuer、River 或独立 API 服务。异步导入复用 OGX 单文件 FileBatch 及 PostgreSQL 状态，不新增可部署组件。

步骤 1～14 保留为当前已实现基线和验证证据，其中“租户级模型配置”“固定 `dense` Named Vector”和“一次 Qdrant 查询融合多个 KB”描述的是改造前现状。目标行为以步骤 15～20 为准；本次是明确的不兼容升级，不保留旧租户配置 API，不迁移已有租户 Profile、VectorStore 或 Qdrant Point，开发与验收环境在切换后重新创建数据卷。

### 1.1 2026-08-26 实施快照

| 步骤 | 当前状态 | 已有证据或剩余事项 |
| --- | --- | --- |
| 1. 依赖与探针 | 完成 | `uv.lock`、Embedding/Qdrant 探针、无密钥配置和真实 Endpoint live check 均通过；项目采用 MIT License |
| 2. 最小 Distribution | 完成 | 生产 Compose 只有 OGX、PostgreSQL、Qdrant；无关 OGX 路由未注册 |
| 3. 租户 Collection | 完成 | 每租户一个 Collection、租户内逻辑 VectorStore、稳定复合 Point ID、Payload Index 与 scoped delete 已通过测试 |
| 4. Dense 纵向链路 | 完成 | Stub 下覆盖 Markdown/PDF 与恢复；真实 Qwen3 Embedding 下完成 5 份项目文档导入和跨库 Dense 检索 |
| 5. 通用 Filter | Provider 完成 | 全部 OGX 操作符、保留字段保护和服务端 `vector_store_id` 强制条件已有单元测试；两侧产品契约在步骤 12 完成 |
| 6. 中文 BM25 选型 | 完成 | 工程语料与 5 份两侧真实项目文档均通过；真实语料的 8 条查询为 8/8 Top1、MRR 1.0，继续采用 Qdrant multilingual |
| 7. Hybrid RRF | 完成 | 0.6B/4B/8B 三种真实 Embedding 的 Hybrid 均为 8/8 Top1，且与相同 Payload Filter 共用一条 Qdrant Query |
| 8. 生命周期 | 完成 | 同一 File 双 VectorStore、单侧删除、VectorStore 删除与失败挂载重试均已验证 |
| 9. 恢复 | MVP 完成 | PostgreSQL/Qdrant/OGX 分别重启、Embedding 失败恢复、异步 FileBatch 重启恢复和 Docling Worker 租约回收均通过；Qdrant Upsert 窗口杀进程未单独注入 |
| 10. MVP 验收 | 完成 | 机制、恢复、资源、真实 Embedding、两侧项目文档效果和七类非 OCR 文件矩阵均有新鲜证据，OGX 路线继续 |
| 11. 统一 Knowledge API | 完成 | 同一 OGX 进程提供异步 Ingest、Operation 状态、多知识库 Search 和稳定 SearchHit |
| 12. 两侧契约 | 服务端完成 | Stella 四级范围和企业版显式知识库映射、过滤与 E2E 已固化；产品仓库适配后续各自实施 |
| 13. 生产化 | 部分完成 | Runtime/Admin 认证、凭证加密、本地/S3 可选配置、稳定错误和无效文件清理已完成；真实 S3、备份恢复、监控和更大业务语料仍待验证 |
| 14. 源码交付 | 完成 | 完整源码、Compose 本地构建、Secret 初始化、健康诊断和 KnowledgeBase 创建脚本已就绪；不再维护 GHCR 成品镜像和 Release 部署包 |
| 15. KB 自包含创建 | 完成 | 创建技术 KnowledgeBase 时提交并验证独立 Embedding 与可选 Rerank 配置；旧租户配置 API 已删除 |
| 16. KB 独立导入 | 完成 | 每个 KB 使用自己的模型连接，并按 `model_id + dimension` 幂等创建或复用 Dense Named Vector |
| 17. 单 KB 检索 | 完成 | 各模式使用 KB 配置；Hybrid 可选本库 Rerank；单 KB 不执行外层 RRF |
| 18. 跨 KB 检索 | 完成 | 各 KB 独立检索和可选 Rerank，最后执行稳定、等权的 Python 外层 RRF |
| 19. 配置生命周期 | 完成 | 空 KB 可改 Embedding；已有文件后锁定模型与维度；Rerank 随时可改；删除时清理 KB Profile |
| 20. 增量验收与交付更新 | 完成 | 契约、产品示例和脚本已更新；多模型、多维度、跨 KB、故障恢复、生产镜像和资源采样均通过 |

租户 Collection 与产品调用模拟已按新链路完成一轮本地采样：67 个创建、导入、权限/挂载检索和跨租户拒绝场景全部通过，企业版覆盖 3/5/7 维独立 KB、两套独立 Embedding 凭证、两套独立 Rerank 凭证和关闭 Rerank 的 KB。8 并发下执行 700 个 Hybrid 请求，吞吐约 `14.11 req/s`，平均 `564.34 ms`，P50 `499.23 ms`，P95 `979.30 ms`，最大 `1279.39 ms`。混合阶段 OGX CPU 平均约 `98.59%`、P95 `107.52%`、内存最大约 `1181.70 MiB`；Qdrant CPU 平均约 `5.81%`、P95 `7.14%`、内存最大约 `836.50 MiB`；PostgreSQL CPU 平均约 `5.84%`、P95 `7.74%`、内存最大约 `75.09 MiB`。CPU `100%` 约等于一个逻辑核。

该采样使用确定性 Embedding/Rerank 测试桩和小型 Markdown，只验证接口、隔离、并发路径及本地组件量级，不代表真实模型延迟、检索效果、容量或生产 SLA。它不能与旧版“一个请求只发出一次 Qdrant Query”的 `62.95 req/s` 直接视作等价回归：新版跨 KB 请求会按挂载 KB 数扇出本地检索和可选 Rerank，吞吐下降是真实架构成本。Qdrant 在本次采样前已经过动态 Schema、恢复和文件矩阵预热，因此其内存数字也不是冷启动基线。原始 JSON、CSV 和 Markdown 报告保存在本地 `.reports/`，不进入 Git。

当前镜像已只预置 Docling PDF 默认路径需要的模型：Transformers layout、Accurate TableFormer 和 HybridChunker tokenizer，且都固定到 commit。构建阶段在离线模式初始化 PDF Pipeline，最终镜像约 `1.08 GB`，而不是此前包含未使用 ONNX/Fast 变体时的约 `4.55 GB`。

本机 Docker 基线仅用于量级判断，不是生产配额承诺：七类文件矩阵中 OGX 冷态约 `1.07 GiB`，默认两个 Worker 都懒加载 Docling 模型后常驻约 `2.2 GiB`，瞬时内存峰值约 `2.46 GiB`；Qdrant 预热基线/峰值约 `106/113 MiB`，PostgreSQL 约 `62/65 MiB`。资源数字来自三维确定性 Embedding Stub；另行完成的真实模型评测显示，当前服务上 0.6B/1024 维的 5 文档总导入约 `11.47 s`、Hybrid 平均约 `648 ms`，4B 约 `13.61 s / 738 ms`，8B 约 `35.33 s / 5514 ms`。这些只是不同链路可用性的单次测试数据，不是当前选型、吞吐或 SLA 承诺。

## 2. 全局约束

- Python 使用 `>=3.12`；OGX 固定 `v1.3.0`，升级不与首期实现混在一起。
- 使用 `uv` 管理 Python 依赖并提交锁文件；Docker 镜像和 Qdrant 镜像使用不可漂移的精确版本。
- 不修改 OGX Core。VectorIO 通过外置 Provider 接入；统一产品接口通过 OGX `external_apis_dir` 接入。
- 生产 Compose 始终只有 `knowledge-ogx / postgres / qdrant` 三个服务；测试桩不进入生产 Compose。
- 每租户一个 Qdrant Collection；同租户 VectorStore 是 Payload 中的逻辑范围，不为每个知识库创建 Collection。没有租户 ID 的单租户部署使用默认 Collection。
- Collection 从创建时就使用 Named Vector Schema。BM25 Sparse Vector 在租户内共享；Dense Named Vector 只按 `model_id + dimension` 的稳定标识划分，相同向量空间复用 HNSW，不同向量空间严格分离。
- 每个技术 KnowledgeBase 在创建时提交完整 Embedding 配置和可选完整 Rerank 配置；服务不发现模型列表。Embedding 未提交维度时通过实际探针推断，已提交时验证返回维度。
- KnowledgeBase 首次 Ingest 被可靠接受后锁定 Embedding 模型与维度；URL 和 Key 只有在模型、维度不变且重新探测通过时才能更新。Rerank 不参与持久索引，可随时修改或关闭。本期不实现全量重新向量化。
- 不同 URL/Key 下相同 `model_id + dimension` 是否属于同一向量空间由 NewAPI 部署契约保证；服务不增加模型版本兼容性探测。
- `hybrid` 在每个 KB 内执行 Dense + BM25 与 Qdrant 内层 RRF，并可使用该 KB 的 Rerank；`dense` 与 `bm25` 不执行 Rerank。单 KB 直接返回本地最终排名，两个及以上 KB 才执行等权外层 RRF。
- 本次不兼容旧租户配置端点和既有数据，不增加旧 API Alias、双写、数据迁移或读取回退；切换实现前显式重置 MVP 数据卷。
- 产品负责权限计算；知识库服务校验并完整执行 `knowledge_base_ids + filters`，不推断权限。
- 保留字段 `vector_store_id / file_id / chunk_id` 只能由服务生成，调用方 attributes 不能覆盖。
- 所有凭证只从环境变量或 Secret 注入；示例、日志、测试快照和错误信息不得包含真实 Token。
- 关键边界代码必须带注释或 docstring，尤其是过滤语义、Point ID、共享 Collection 删除和失败恢复。
- 单元测试不得依赖真实模型服务；真实 Embedding 验证作为显式 live test。
- 每一步只有通过自己的验证门，才能进入依赖它的下一步。

## 3. 预期仓库区域

具体文件名可在实现时按 OGX 外置包要求微调，但职责边界保持如下：

```text
shared-knowledge-service/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── compose.yaml
├── .env.example
├── config/
│   ├── ogx.yaml
│   └── apis.d/                 # 已启用统一 Knowledge External API
├── src/shared_knowledge_service/
│   ├── provider/               # 外置 Qdrant VectorIO Provider
│   ├── retrieval/              # Payload、Filter、BM25、RRF 查询构造
│   ├── knowledge_base_inference/ # 由 tenant_inference 改为按 KB Profile 路由模型连接
│   └── api/                    # MVP 通过后增加的 External API
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── evaluation/
│   └── fixtures/
└── docs/changes/shared-knowledge-service/
    ├── solution.md
    └── implementation.md
```

不为 Provider、API 和测试分别创建多个仓库或多个可部署服务。

## 4. 实现步骤

### 步骤 1：建立可重复依赖基线与前置探针

**预期结果**

- Python 项目可以使用 `uv sync --frozen` 完整还原。
- 固定 OGX、Qdrant Server、qdrant-client、Docling 相关依赖的精确版本。
- 通过安全的预检命令确认 Embedding Endpoint、模型、向量维度、批量上限和超时；不打印 Token。
- 第一批源代码按已确定的 MIT License 发布，并保留所复用第三方代码的许可声明。

**改动区域**

- `pyproject.toml`、`uv.lock`、`.python-version`、`.gitignore`、`.env.example`。
- 租户 Embedding Admin API 在保存配置前对调用方提交的确切 Endpoint、模型和维度执行探针。
- `README.md` 中的本地环境和凭证规则。

**依赖**

- 无代码依赖。
- 真实 Embedding live check 需要用户提供可用 Endpoint、Token 和候选模型环境变量。

**验证方法**

- 在全新虚拟环境执行 `uv sync --frozen`。
- 缺少 URL、Key、模型或维度时配置接口明确拒绝；配置完整时调用 `/v1/embeddings` 并校验返回维度，响应和日志不回显 Token。
- 扫描仓库，确认没有真实 Token、内部 URL 或 `.env` 被跟踪。
- 启动目标 Qdrant 镜像并执行最小 Sparse Vector + IDF + RRF 特性探针。

### 步骤 2：启动最小 OGX Distribution 并加载外置 Provider

**预期结果**

- `knowledge-ogx / postgres / qdrant` 可以通过一个 Compose 启动并通过健康检查。
- OGX 只启用 Files、File Processors、Inference、VectorIO。
- 外置 Provider 通过 `get_provider_spec` / `get_adapter_impl` 或对应外置规范加载，不修改 OGX Core。
- Docling 使用 OGX v1.3.0 固定的两个 Worker、`do_ocr=false`、无 VLM、HybridChunker 最终上限 `1000` tokens、相邻 overlap 最多 `200` tokens；固定版本镜像应用最小 overlap 补丁，但不修改 Core 只为减少 Worker 数。

**改动区域**

- `Dockerfile`、`compose.yaml`、`config/ogx.yaml`、Provider spec。
- `src/shared_knowledge_service/provider/` 的配置对象、入口函数和最小 Adapter。
- Compose 健康检查和持久卷配置。

**依赖**

- 步骤 1。

**验证方法**

- `docker compose config --quiet`。
- `docker compose up --build` 后 OGX、PostgreSQL、Qdrant 全部 healthy。
- 查询 OGX health/route/OpenAPI，确认四类目标 API 存在，无关 Agent、Responses、Messages、Tools API 不存在。
- 关闭并重新启动 Compose，确认 PostgreSQL、Qdrant 和原文件卷未被重建。

### 步骤 3：实现共享 Collection 与逻辑 VectorStore 骨架

**预期结果**

- Provider 初始化一个固定物理 Collection，包含 `dense` 和 `bm25` 两个 named vector 配置。
- `vector_store_id`、`file_id` 以及部署声明字段建立正确的 Payload Index。
- 创建任意数量 VectorStore 不创建新 Collection；删除 VectorStore 只清理对应 Payload。
- Point ID 使用 `hash(vector_store_id + chunk_id)`，相同 Chunk 在不同 VectorStore 中不冲突。

**改动区域**

- Provider 配置、Collection 初始化、Point/Payload 映射、注册与注销逻辑。
- Qdrant 集成测试 Fixture。
- Point ID、Payload 和 Collection Schema 单元测试。

**依赖**

- 步骤 2。

**验证方法**

- 连续创建两个 VectorStore，Qdrant Collection 数量保持为 1。
- 写入相同 `chunk_id` 的两个逻辑知识库，得到两个不同 Point。
- 删除一个 VectorStore 后另一个仍可读取，物理 Collection 仍存在。
- 新建第三个 VectorStore 时 Payload Index 和 HNSW 配置没有变化。

### 步骤 4：打通 Dense 原生纵向链路

**预期结果**

- 使用 OGX 原生 API 完成：创建 VectorStore → 上传 File → Docling 解析 → HybridChunker → Embedding → Qdrant Upsert → Dense Search。
- 原文件、OGX 元数据、Chunk Payload 和 Dense Vector 都可追溯。
- OGX 搜索结果可以从 `chunk_content` 稳定恢复文件、Chunk、正文和定位信息。

**改动区域**

- Provider 的 `add_chunks / query_vector / delete_chunks` 及 OGX Adapter 桥接。
- Markdown、纯文本、HTML、数字 PDF、DOCX、PPTX、XLSX 和 CSV 非 OCR Fixture。
- 可重复的原生 API E2E 测试。
- 测试专用的确定性 Embedding Stub，以及真实 Endpoint 的可选 live test。

**依赖**

- 步骤 3。
- 步骤 1 的租户 Embedding 配置探针通过后才能运行真实模型测试。

**验证方法**

- E2E 断言 VectorStoreFile 为 `completed`，Chunk 数量大于 0，Dense Search 返回正确文件和 locator。
- 重启 OGX 后重新搜索，结果仍存在。
- CI 使用确定性 Stub 得到稳定结果；live test 使用真实 Endpoint 且不记录凭证或完整向量。

### 步骤 5：实现通用 Filter 与两侧隔离契约

**预期结果**

- 完整翻译 OGX `eq / ne / gt / gte / lt / lte / in / nin / and / or` 到 Qdrant Filter。
- 原生单 VectorStore 查询自动且不可绕过地加入 `vector_store_id` 条件。
- attributes 只允许写入非保留字段；配置声明字段获得 Payload Index。
- Stella 四级范围与企业版多 `vector_store_id` 范围都能在 Qdrant 查询阶段过滤。

**改动区域**

- Filter AST 翻译、字段路径映射、保留字段校验、索引配置解析。
- Stella 四级权限矩阵、企业版知识库列表、缺失字段与否定条件测试。

**依赖**

- 步骤 3。
- 为减少 Provider 核心文件冲突，默认在步骤 4 后集成；过滤翻译纯函数可以提前编写。

**验证方法**

- 为每种操作符执行正向、反向和嵌套组合测试。
- 对当前 `(user_id, agent_id)` 验证 `system + system_agent + user + user_agent` 四路累加及所有越权反例。
- 验证企业版 `vector_store_id IN [...]` 只返回挂载知识库。
- 记录实际发送给 Qdrant 的 Query，证明 Filter 位于 Dense 查询中而不是结果后处理。
- 调用方试图覆盖 `vector_store_id / file_id / chunk_id` 时请求失败。

### 步骤 6：选择并固定中文 BM25 Encoder

**预期结果**

- 使用同一中文语料比较 Jieba 路线与 Qdrant 服务端原生 multilingual tokenizer。
- 文档和查询端使用同一分词、词项映射和 Sparse 编码协议。
- 只把最终选中的 Encoder 留在生产路径；另一路保留评测证据，不保留无用途的运行时代码。

**改动区域**

- `src/shared_knowledge_service/retrieval/` 中统一 BM25 Encoder 接口和候选实现。
- `tests/evaluation/` 的合成中文语料、查询、相关性标注和评测脚本。
- 在 `solution.md` 未决问题中回填最终选择和理由。

**依赖**

- 步骤 1 的 Python 基线。
- 可以与步骤 2～5 并行研究；接入 Qdrant 依赖步骤 3。

**验证方法**

- 覆盖中文自然语言、专有名词、中英混合、型号、错误码和精确标识符。
- 对比 Recall@k、MRR、索引耗时和依赖体积。
- 重启后同一文本产生相同 Sparse indices；文档与查询编码空间完全一致。
- 明确证明结果是带分数的 BM25 排名，不是 MatchText Filter 或固定分数。

### 步骤 7：完成 BM25 与 Qdrant 原生 Hybrid RRF

**预期结果**

- 每个 Point 同时保存 Dense 与 BM25 Sparse Vector。
- `dense / bm25 / hybrid` 三种模式可用。
- Hybrid 使用两个 Prefetch、同一 Payload Filter 和 Qdrant 原生 RRF；不在 OGX Server 中二次融合。

**改动区域**

- Sparse Upsert、BM25 Query、Hybrid Query 构造和结果恢复。
- Provider `query_keyword / query_hybrid`。
- Dense、BM25、Hybrid 对照 E2E 测试。

**依赖**

- 步骤 4、5、6。

**验证方法**

- 检查 Qdrant Query 请求包含 Dense/Sparse Prefetch、相同 Filter 和 RRF。
- 语义型查询、精确词查询和混合查询分别命中预期结果。
- BM25 结果分数随词频和文档频率变化。
- 调整一个 Filter 后，Dense 与 BM25 候选集同步变化且无越权 Point。

### 步骤 8：闭合对象生命周期与删除语义

**预期结果**

- 同一 File 可挂载多个 VectorStore，互不覆盖。
- 删除 VectorStoreFile、VectorStore 和原 File 遵循方案中的引用与作用域边界。
- 失败挂载可以通过“删除失败挂载 → 重新 attach”恢复。
- 新增逻辑知识库不新增 Collection 或索引。

**改动区域**

- Provider 的 scoped delete、unregister 和辅助查询。
- OGX 原生对象生命周期 E2E 用例。
- 恢复与清理操作说明。

**依赖**

- 步骤 7。

**验证方法**

- 同一 File 挂载两个 VectorStore，删除一侧后另一侧 Dense/BM25/Hybrid 都正常。
- 删除 VectorStore 后 Collection 仍存在，且其他 VectorStore Point 数量不变。
- 按正确顺序删除最后一个挂载与原 File，PostgreSQL、Qdrant、原文件卷均无孤儿。
- 构造 failed VectorStoreFile，按恢复协议重新处理成功且无重复 Point。

### 步骤 9：验证任务、崩溃与重启恢复

**预期结果**

- Docling Worker 崩溃时，OGX 持久任务重新租约并在尝试预算内结束。
- OGX 在 Embedding 或 Qdrant Upsert 阶段崩溃时，不宣称自动恢复；显式恢复协议能够修复。
- PostgreSQL、Qdrant、OGX 分别重启后，已完成数据仍可检索。

**改动区域**

- 测试专用故障注入与延迟 Stub。
- E2E 故障测试、状态断言和数据对账辅助函数。
- 运维恢复说明。

**依赖**

- 步骤 8。

**验证方法**

- 解析中终止 Docling 子进程，观察 Job attempts、租约回收和最终状态。
- Embedding 响应前终止 OGX，重启后执行恢复协议，确认无重复、无串库、无假完成。
- Qdrant Upsert 延迟期间终止 OGX，重复相同复合 Point ID 后数量稳定。
- 分别重启三个服务并重跑原生搜索与删除用例。

### 步骤 10：执行 MVP 验收门与资源测量

**预期结果**

- `solution.md` 中的所有硬性 MVP 验收项都有新鲜证据。
- 得到空闲、解析、Embedding、索引和检索阶段的 CPU、内存、磁盘、耗时基线。
- 明确给出“进入统一 API 阶段”或“停止 OGX 路线”的判断。

**改动区域**

- 测试入口、验收清单和结果记录。
- README 中的 MVP 启动与原生 API 示例。
- `implementation.md` 中回填每项验证证据与结论。

**依赖**

- 步骤 2～9 全部完成。

**验证方法**

- 执行整体验证命令集和全部故障场景。
- 使用 `docker stats --no-stream`、服务指标和测试计时记录资源。
- 清空持久卷后从零部署一次，确认文档步骤可复现。
- 任一隔离、删除、真 BM25、RRF 或恢复硬性项失败时，不进入步骤 11。

### 步骤 11：确定导入语义并在同一 OGX 进程挂载统一 Knowledge API（已完成）

**预期结果**

- 通过 OGX `external_apis_dir` 注册 `/knowledge/v1/ingest` 和 `/knowledge/v1/search`，不增加部署服务、不修改 Core。
- `ingest` 完成 Files upload + 单文件 FileBatch 持久化后返回 HTTP `202`；Operation 状态接口将 OGX Batch 转换为稳定终态。
- `search` 接收一个或多个 `knowledge_base_ids`，在一次 Qdrant 查询中转换为 `vector_store_id IN [...]`。
- 返回稳定 `SearchHit`，不暴露 Qdrant Point 或 OGX EmbeddedChunk。

**改动区域**

- `src/shared_knowledge_service/api/` 的 Protocol、请求响应模型、Provider 和 FastAPI router。
- `config/apis.d/` External API spec 和 `config/ogx.yaml`。
- Provider 内部的可信多 VectorStore 查询入口；复用现有 Filter、BM25 和 RRF 实现。
- API 契约和 E2E 测试。

**依赖**

- 步骤 10 的 MVP 验收门通过。

**验证方法**

- 按已确认的导入语义执行契约测试；一次 `ingest` 请求立即返回 File 与 Operation ID，后台最终产生 VectorStoreFile 和 Qdrant Point。
- 注入 Embedding 失败和 OGX 正常重启，验证失败错误、显式重试与 FileBatch 自动恢复。
- 一次 `search` 同时查询三个知识库，Qdrant 只收到一条 Hybrid Query。
- Stella 单隐藏知识库和企业版多知识库请求均映射正确。
- attributes 不能覆盖保留字段；外部请求不能绕过 `knowledge_base_ids`。
- OpenAPI 中只公开既定字段、必填性和返回结构。

**实测结果**

- 已选择异步分支，不新增独立任务实体；`operation_id` 复用 OGX 单文件 `file_batch_id`。
- `external_apis_dir` 已在同一 OGX 进程注册 `/knowledge/v1/ingest`、Operation 状态与 `/knowledge/v1/search`。
- 两个逻辑知识库的混合检索已验证通过；Provider 单元测试确认 Dense + BM25 + RRF 只调用一次 Qdrant Query API，并使用 `vector_store_id MatchAny`。
- `hybrid / dense / bm25`、业务 Filter、保留 attributes、稳定 SearchHit、异步失败结构和重启恢复均进入自动化测试。

### 步骤 12：固化 Stella 与企业版集成契约（服务端契约已完成）

**预期结果**

- Stella 有固定隐藏 VectorStore 的初始化、四级 attributes、Search Filter 和清理示例。
- 企业版有显式业务知识库 CRUD 映射、挂载列表和多知识库 Search 示例。
- 两侧只需维护权限、业务对象、映射和 UI；解析、切块、索引与检索行为由统一服务维护。

**改动区域**

- 产品无关的 OpenAPI/JSON 示例与契约测试 Fixture。
- Stella 四级矩阵和企业版公司/部门/产品示例。
- 集成说明；本步骤不直接修改 Stella 或企业版仓库。

**依赖**

- 步骤 11。

**验证方法**

- 对每个产品请求样例执行契约测试并断言实际 Qdrant Filter。
- Stella 覆盖全部四级正反例；企业版覆盖创建、修改、挂载、卸载和删除。
- 产品样例中不出现 Qdrant Collection、Point ID 或内部 Provider 参数。

**实测结果**

- `docs/product-integration.md` 已固定共同对象映射、Stella 四级请求和企业版显式业务知识库映射。
- Qdrant 集成测试覆盖 Stella `system / system_agent / user / user_agent` 四路累加，并排除其他用户数据。
- 统一 API E2E 覆盖企业版一次挂载两个业务知识库、跨库检索和业务 Filter；原生 E2E 覆盖 VectorStore 创建、挂载、卸载与删除。
- Stella 与企业版仓库内的实际适配器不属于本仓库实现范围，仍由两侧后续接入。

### 步骤 13：生产化收尾

**预期结果**

- 服务具备最小生产认证、Secret、网络隔离、健康检查、日志、指标、备份和恢复说明。
- 原文件后端可按部署选择本地卷或 S3。
- 固定资源建议来自步骤 10 的实测，而不是预估。
- 公共仓库具备许可证、第三方声明和无密钥发布检查。

**改动区域**

- 生产 Compose 覆盖、S3 配置、认证配置、资源限制、日志与监控配置。
- PostgreSQL、Qdrant Snapshot 和原文件的备份恢复 Runbook。
- 安全、升级、回滚和兼容性文档。

**依赖**

- 步骤 10；S3、认证和运维文档可与步骤 11～12 并行，但不能改变 API/Provider 契约。

**验证方法**

- 未提供 Secret 时生产配置拒绝启动；日志和错误响应不包含凭证。
- Qdrant/PostgreSQL 端口不对产品网络公开。
- 从备份恢复 PostgreSQL、Qdrant 和原文件后，完成一致性抽查和检索。
- 本地卷与 S3 后端运行同一 Files/导入 E2E 契约。
- 依赖、许可证、镜像和 Secret 扫描通过。

### 步骤 14：提供可直接修改的源码交付

**预期结果**

- 使用方拿到完整源码后可以修改 Provider、API、配置、依赖和 Dockerfile。
- 已验证的 Linux amd64 宿主机只需 Docker、Compose 与 curl；macOS 使用 Docker Desktop，交付脚本兼容系统自带 Bash 3.2 与 BSD 工具。Python、uv 和 Docling 模型均在镜像构建阶段处理。
- PostgreSQL 与 Qdrant 使用固定官方镜像，Knowledge 从当前工作区源码构建。

**已实现**

- 根目录 `Dockerfile`、`compose.yaml`、`.env.example`、`pyproject.toml` 与 `uv.lock` 构成可重复的源码构建入口。
- `scripts/init-env.sh` 生成四项本地 Secret；`build-production-image.sh` 处理模型端点和镜像构建；`doctor.sh` 验证三服务与 HTTP 路由；`create-knowledge-base.sh` 隐藏提交 KnowledgeBase 模型 Key。
- README 首页提供 clone、初始化、构建、启动和诊断流程。
- 已移除只服务于成品镜像的 GHCR Release workflow、部署包目录和打包脚本，避免维护两套交付路径。

**本地验证结果**

- 已从排除 `.git`、`.venv`、`.env` 和本地缓存的全新源码副本执行完整流程。
- `init-env.sh` 生成的 `.env` 权限为 `0600`，四项示例凭证均被随机值替换，Compose 配置和全部辅助脚本语法检查通过。
- 四个交付脚本已通过 Bash 3.2 语法检查；`init-env.sh` 已在 Bash 3.2 环境实际生成有效 `.env`，不依赖 GNU `sed -i` 或 `dirname --`。Apple Silicon 完整链路仍待真机验证。
- `build-production-image.sh` 完成固定模型端点探测并从源码构建镜像；依赖和 Docling 模型层命中缓存，只有业务源码层重建。
- 独立 Compose 项目中的 PostgreSQL、Qdrant、Knowledge 三个容器全部 healthy，宿主机 `/v1/health` 返回正常；验证后的临时容器和三个测试数据卷已清理。

### 步骤 15：让技术 KnowledgeBase 自包含模型配置

**预期结果**

- `POST /knowledge/v1/knowledge-bases` 一次接收 `tenant_id`、完整 Embedding 配置和可选完整 Rerank 配置，不再要求先配置租户模型。
- Embedding `dimension` 可省略；省略时调用确切模型并保存实际返回维度，提交时则验证实际维度一致。
- 每个 KB 分别保存 Embedding/Rerank Profile 和加密凭证；两套 URL/Key 相同时仍作为两份完整配置处理，不增加连接引用实体。
- OGX 内部模型资源只保存 opaque KB Profile ID；Inference Provider 根据 Profile 解析正确 KB 的真实 URL、Key、模型和维度。
- 旧 `/tenants/{tenant_id}/embedding-config`、`/rerank-config` 端点和协议直接删除，不保留兼容 Alias 或数据迁移；交付脚本与文档入口在步骤 20 统一收尾。

**改动区域**

- `api/models.py`、`routes.py`、`protocol.py`、`provider.py`：创建请求、非敏感响应、幂等指纹和路由。
- `api/state.py`：把租户 Profile 改成 KnowledgeBase Profile，独立保存两类密文、模型锁状态和反向 Profile 索引。
- `tenant_inference/` 重命名为 `knowledge_base_inference/`，并把 Adapter、Provider spec、配置与错误术语改成 KB 语义。
- `api/upstream.py`：Embedding 维度推断/验证及可选 Rerank 精确探针。
- 旧租户配置单元测试与创建流程所需的 E2E Fixture；README 和脚本在步骤 20 按最终契约统一更新。

**依赖**

- 步骤 1～14 的当前基线。
- `solution.md` 已确认的 KB 创建请求、凭证安全和 NewAPI 模型 ID 契约。
- 开始 E2E 前显式重置 PostgreSQL、Qdrant 与原文件 MVP 数据卷；不尝试读取旧 Profile 或 Point。

**验证方法**

- 同一租户创建两个 KB，分别使用相同和不同 URL、Key、Embedding/Rerank 模型；读取接口只返回非敏感配置。
- 分别覆盖显式维度、自动推断维度、维度不一致、Embedding 探针失败、Rerank 探针失败和 `rerank=null`。
- 创建任一步失败都不遗留 VectorStore、Profile、凭证、内部模型资源或完成态幂等记录。
- 相同 Idempotency-Key 和相同完整配置稳定重放；配置或凭证不同返回幂等冲突，日志与响应不泄露 Key。
- OpenAPI 与路由测试确认旧租户配置端点不存在。

### 步骤 16：按 KnowledgeBase 向量空间完成独立导入

**预期结果**

- 每个租户仍只有一个 Qdrant Collection；Collection 从创建时使用 Named Vector Schema，并共享一个 BM25 Sparse Vector。
- Dense Vector 名由 `model_id + dimension` 的稳定标识生成。相同模型与维度的 KB 幂等复用同一 Named Vector/HNSW，不同模型或维度动态追加独立 Named Vector。
- 每个 Chunk Point 只写入所属 KB 对应的 Dense Named Vector、共享 BM25 Sparse Vector 和既有 Payload，不要求其他 KB 的 Point 补齐新向量。
- Ingest 使用目标 KB 的 Embedding Profile 和独立凭证；首次请求被可靠接受时锁定该 KB 的模型与维度。
- 并发首次使用同一或不同向量空间时，Collection Schema 创建和复用保持幂等。

**改动区域**

- `provider/index.py`：动态 Dense Vector 名、Collection 创建、Qdrant 1.18 `create_vector_name`、Schema 校验、Upsert 和 scoped delete。
- `provider/adapter.py`、`provider/config.py`：从 VectorStore/KB Profile 取得 Dense Vector 名，不再依赖全局固定 `dense` 配置和租户统一维度。
- `api/provider.py`、`api/state.py`：首次 Ingest 的 KB 生命周期锁、Collection Schema 锁和锁定状态持久化。
- Qdrant 单元/集成 Fixture 与并发 Schema 测试。

**依赖**

- 步骤 15。
- Qdrant Server 固定为已验证支持动态 Named Vector 的 `1.18.2`。

**验证方法**

- 同租户两个 KB 使用相同模型与维度，Collection 只有一份对应 Dense Schema；两者仍分别调用自己的 URL/Key。
- 新 KB 使用不同模型或不同维度后只追加一个 Dense Named Vector，已有 Point、BM25、Payload Index 和检索结果不被重建或修改。
- 并发创建相同向量空间不会重复失败，并发创建不同向量空间不会覆盖 Schema。
- 实际 Point 只包含其 KB 的 Dense Vector 与 BM25；按错误 Vector 名查询不能命中该 Point。
- 删除一个 KB 的 Point 不影响其他 KB、租户 Collection、共享 BM25 或 Dense Named Vector Schema；Profile 和内部模型资源的完整删除在步骤 19 验证。

### 步骤 17：闭合单 KnowledgeBase 检索链路

**预期结果**

- Search 只包含一个 `knowledge_base_id` 时，使用该 KB 的配置快照和 Dense Named Vector 执行本地检索。
- `hybrid` 执行 Dense + BM25、Qdrant 内层 RRF，并在该 KB 启用时调用自己的 Rerank；Rerank 失败只回退到内层 RRF。
- `dense` 只调用 Query Embedding 和 Dense 检索；`bm25` 只调用 BM25，不调用 Embedding；两种模式都不执行 Rerank。
- 单 KB 结果不进入外层 RRF，排名和 `score` 保留本 KB 最终阶段输出。
- Stella 固定隐藏 KB 与企业版只挂载一个 KB 的调用路径保持相同请求结构和权限 Filter。

**改动区域**

- `api/provider.py`：按 KB 读取模型快照、选择模式、组织本地候选和稳定 SearchHit。
- `provider/adapter.py`、`provider/index.py`：按指定 Dense Named Vector 执行单 KB Dense/BM25/Hybrid 查询。
- `knowledge_base_inference/`：Embedding 与 Rerank 请求按 opaque KB Profile 解析连接。
- 单 KB 模式、Rerank 降级、四级 Filter 和分数语义测试。

**依赖**

- 步骤 16。

**验证方法**

- 对 `hybrid / dense / bm25` 分别断言 Qdrant、Embedding 和 Rerank 的调用次数及使用的 KB Profile。
- Hybrid 开启、关闭和上游失败时排序符合本地 RRF/Rerank 规则；Dense/BM25 即使配置了 Rerank 也不调用。
- 用 Stella 四级矩阵验证合法结果完整累加且无越权 Point，确认未调用外层 RRF。
- 两个 KB 使用不同 Key 时，分别执行单 KB Search，模型测试桩能证明未串用凭证或内部模型资源。

### 步骤 18：实现跨 KnowledgeBase 分支与等权外层 RRF

**预期结果**

- 两个及以上 `knowledge_base_ids` 必须属于同一租户，但可以使用不同 Embedding/Rerank URL、Key、模型和维度。
- 统一 Search 层为每个 KB 建立独立分支：完成本库召回、Hybrid 内层 RRF 和可选 Rerank 后，再融合各 KB 的最终排名。
- 外层 RRF 等权处理每个 KB；同名次按请求中的 `knowledge_base_ids` 顺序稳定处理，不比较不同模型的原始分数。
- 分支使用服务端受控的并发与候选上限，不向产品 API 暴露内部 Top K、RRF 常数或 Rerank 候选参数。
- 任一 KB 的 Embedding、Qdrant、对象或必需配置失败时整体 Search 失败；空结果正常参与，单个 KB 的可选 Rerank 失败只降级该分支。

**改动区域**

- `api/provider.py`：同租户校验、配置快照、受限并行分支、全部失败语义、外层 RRF 与稳定截断。
- `provider/adapter.py`：删除“所有 KB 必须同模型同维度”和一次 `MatchAny` Hybrid Query 路径，改为每个 KB 调用自己的索引空间。
- 外层 RRF 纯函数及多 KB 单元、集成、E2E 和并发测试。
- 服务端检索配置：分支并发上限、每 KB 候选上限、外层 RRF 固定参数及指标。

**依赖**

- 步骤 17。

**验证方法**

- 同租户跨两个不同维度、不同 Embedding Key、不同 Rerank 模型的 KB 查询成功，每个分支使用正确连接。
- 覆盖一个 KB 开启 Rerank、另一个关闭，以及某个 Rerank 超时后的局部降级。
- 用确定排名输入验证等权 RRF、稳定同分顺序、最终 `limit` 和重复 KB ID 去重；单 KB 请求不得进入该函数。
- 注入一个必需分支失败，确认不返回部分结果；一个分支无命中时返回其他分支的合法融合结果。
- 跨租户请求继续返回明确错误，且不会向第二个 Collection 发出查询。

### 步骤 19：闭合 KnowledgeBase 模型配置生命周期

**预期结果**

- 提供查询 KB 模型配置、更新 KB Embedding 和更新/关闭 KB Rerank 的 Admin 接口，所有响应不返回 Key。
- 空 KB 可以更新 Embedding URL、Key、模型和维度；已有文件后只能在模型与维度不变且探针通过时更新 URL/Key。
- Embedding Key 与 Rerank Key 始终独立提交和保存；不使用“字段缺失表示保留旧 Key”的隐式行为。
- Rerank URL、Key、模型和开关可随时修改，只影响修改后开始的 Search；进行中的请求使用开始时取得的配置快照。
- 删除 KB 同步清理其 Profile、加密凭证、内部模型资源和创建幂等映射，但不删除 Collection 级 Dense Named Vector Schema。

**改动区域**

- `api/models.py`、`routes.py`、`protocol.py`、`provider.py`：三个 KB 配置管理接口和错误语义。
- `api/state.py`、`knowledge_base_inference/`：配置快照、更新、删除和反向 Profile 索引。
- `provider/adapter.py`：空 KB 切换向量空间、已锁定 KB 拒绝模型/维度变化、删除资源对账。
- 生命周期、并发更新、删除恢复和凭证安全测试。

**依赖**

- 步骤 15～18；其中接口模型和纯状态方法可在步骤 18 后半段准备，但最终集成顺序保持串行，避免同时修改 `api/provider.py`。

**验证方法**

- 空 KB 修改到新模型/维度后首次导入进入正确 Dense Named Vector；旧空 Schema 不要求立即删除。
- 已有文件后修改模型或维度返回 `409`；同模型/维度的 URL/Key 更新只有在探针成功后才原子替换旧凭证。
- Rerank 配置在关闭、启用、换 URL/Key/模型和请求执行期间修改时符合快照语义。
- 删除 KB 后无法再通过 Profile ID 调用模型，密文和反向索引均不存在；重复删除仍幂等。

### 步骤 20：执行增量验收并更新交付入口

**预期结果**

- README、API Reference、产品集成说明、OpenAPI 和示例只描述 KB 级模型配置，不再出现租户配置前置步骤。
- Stella 示例保持一个隐藏 KB、四级 attributes/Filter 和单 KB 直返；企业版示例覆盖创建时提交模型配置、单 KB 挂载和跨 KB 挂载。
- 所有原有自动化测试改用新创建契约，并补齐多模型、多维度、独立凭证、动态 Schema、外层 RRF、失败语义和删除生命周期。
- 得到同租户多 Dense Named Vector 以及 1/2/N 个 KB 并行查询的 CPU、内存、延迟和模型调用数量证据。
- 从全新源码和空数据卷完成三容器构建、启动、创建、导入、单/跨 KB 检索、更新、删除与重启恢复。

**改动区域**

- `README.md`、`docs/api-reference.md`、`docs/product-integration.md`、方案与实施快照。
- `scripts/`：移除 `configure-tenant.sh` 正式入口，改为 KB 创建/配置请求示例；不增加模型发现脚本。
- `tests/unit/`、`integration/`、`e2e/`、`evaluation/` 和产品使用模拟。
- 部署诊断、无密钥扫描与本地资源报告。

**依赖**

- 步骤 15～19 全部完成。

**验证方法**

- 执行完整静态、单元、Qdrant 集成、OGX E2E、统一 API 契约、故障恢复和文档示例验证。
- 使用至少两个 Embedding 向量空间和两个不同 Rerank 配置跑企业版跨 KB 场景；使用一个隐藏 KB 跑 Stella 全部四级正反例。
- 比较单 KB 与跨 KB 的 Qdrant/模型调用数、吞吐、P95、CPU 和内存，确认并发上限生效且无不受控扇出。
- 重启 OGX、PostgreSQL 和 Qdrant 后复测所有已完成 KB；验证无 Profile 串用、Named Vector 丢失或错误部分结果。
- 扫描 OpenAPI、仓库和构建产物，确认旧租户配置端点、真实凭证、无效脚本和兼容代码均不存在。

## 5. 并行与阻塞关系

```text
已完成基线：

步骤 1
├── 步骤 2 → 步骤 3 → 步骤 4 → 步骤 5 ─┐
└── 步骤 6（可独立进行编码与评测）─────────┤
                                           ↓
步骤 7 → 步骤 8 → 步骤 9 → 步骤 10（MVP 验收门）
                                           ↓
步骤 11 → 步骤 12
          └───────────────┐
步骤 10 → 步骤 13 → 步骤 14 ┴→ 可源码交付

本次不兼容增量：

步骤 14 基线
    ↓
步骤 15：KB 自包含创建
    ↓
步骤 16：KB 独立导入与动态 Named Vector
    ↓
步骤 17：单 KB 检索
    ↓
步骤 18：跨 KB 检索与外层 RRF
    ↓
步骤 19：模型配置生命周期
    ↓
步骤 20：增量验收与交付更新
```

- 步骤 1～14 的关系只记录已完成基线，不再作为本次执行队列。
- 步骤 15～19 都会修改 `api/provider.py`、状态模型或 Provider 路由，默认串行完成；不为了表面并行拆出第二套模型或 Search 实现。
- 步骤 16 必须等步骤 15 固定 Profile 与创建契约，否则无法稳定计算 Dense Named Vector 和导入路由。
- 步骤 18 必须以步骤 17 的单 KB 最终排名为输入；外层 RRF 不重新实现本地 Dense/BM25/Rerank。
- 步骤 19 放在检索语义稳定后闭合更新与删除，避免配置切换同时改变仍未确定的查询路径。
- 步骤 20 是本次硬验收门；文档示例可以提前准备，但只有新接口、动态 Schema、单/跨 KB 搜索和生命周期全部通过后才能标记完成。

## 6. 整体验证

最终至少执行以下验证层次：

1. **静态与单元验证**
   - `uv sync --frozen`
   - 格式与静态检查
   - KB Profile ID、凭证加密、向量空间名称、Point ID、Payload、Filter、BM25 Encoder、外层 RRF 和请求模型单元测试
2. **Qdrant 集成验证**
   - Collection 与 Payload Index、动态 Dense Named Vector 创建与并发复用
   - 相同/不同模型和维度的 Dense/Sparse Upsert、Filter、内层 RRF、scoped delete 与既有 Point 不变性
3. **OGX 原生 E2E**
   - Files → Docling → HybridChunker → KB 独立 Embedding → 对应 Qdrant Named Vector → Search
   - 不同 KB Profile、对象状态、删除、多重挂载和重启
4. **故障恢复 E2E**
   - Docling Worker、OGX、PostgreSQL、Qdrant 分别中断
   - KB 配置探针失败、动态 Schema 创建竞争、失败挂载删除重试与幂等对账
5. **统一 API 契约 E2E**
   - 创建 KB 时提交完整模型配置、维度推断/验证、配置锁定/更新/删除；旧租户配置端点不存在
   - 单 KB 的 `hybrid / dense / bm25` 调用与分数语义
   - 多 KB 独立检索、可选本地 Rerank、等权外层 RRF、整体失败和跨租户拒绝
6. **两侧产品契约验证**
   - Stella 一个隐藏 KB、四级 attributes/Filter 和单 KB 直返
   - 企业版创建时提交独立模型配置，覆盖单 KB 与跨 KB 挂载、不同 Key/模型/维度/Rerank
7. **真实模型与中文效果验证**
   - 已完成真实 Embedding Endpoint live test：0.6B、4B、8B 分别返回 1024、2560、4096 维向量
   - 重新使用至少两个向量空间和不同 Rerank 配置验证真实 Ingest、单 KB Search 与跨 KB Search；旧的 8/8 Top1 只作为基线，不自动视为新链路通过
8. **从零部署与资源验证**
   - 按不兼容升级规则清空旧数据卷后从 README 重建
   - 分别记录 1/2/N 个 KB、多个 Dense Named Vector 下的 CPU、内存、磁盘、模型调用数和各阶段延迟
9. **交付边界检查**
   - Git diff 只包含本项目文件
   - 无真实凭证、旧租户配置端点、兼容层、内部文件、临时数据和无用途候选实现
   - `solution.md`、`implementation.md` 与最终行为一致

实施完成前不得仅以“服务能启动”判定成功；KB 凭证隔离、动态 Named Vector、过滤隔离、真 BM25、单 KB 直返、跨 KB 外层 RRF、删除和显式恢复协议都是同等级硬性验收项。

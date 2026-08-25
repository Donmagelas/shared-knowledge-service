# OGX 方案与 Haystack 方案详情

> 更新日期：2026-08-25
>
> 适用范围：Stella 与 Cherry Studio 企业版共用的独立知识库服务
>
> 本文比较的是两条完整落地路线，不把框架名称等同于完整产品。

## 1. 共同目标与不变边界

无论选择 OGX 还是 Haystack，目标系统都需要完成同一条知识库链路：

```text
Stella / Cherry Studio 企业版
        ↓ 统一 Knowledge API
KnowledgeBase、File、任务状态与租户模型配置
        ↓
原文件存储 → 文档解析 → 切块 → Embedding → 索引
        ↓
Filter + Dense / BM25 / Hybrid → 可选 Rerank → SearchHit
```

以下职责不因技术路线改变：

| 职责 | 产品侧 | 统一知识库侧 |
| --- | --- | --- |
| 用户、组织、角色和权限规则 | Stella / 企业版维护 | 不理解业务权限模型 |
| 本次允许访问哪些范围 | 计算 `knowledge_base_ids + filters` | 校验并完整执行 |
| 业务知识库名称、展示和挂载关系 | 产品数据库维护 | 只维护技术 KnowledgeBase |
| 原文件、解析、切块、向量和检索 | 不重复实现 | 统一维护 |
| 模型连接 | 产品选择 URL、Key、模型和维度 | 安全保存、调用和执行配置锁定 |

因此，OGX 与 Haystack 的核心差别不是最终能否做出同样的检索，而是哪些通用能力由上游项目提供，哪些能力需要我们自己建设和长期维护。

## 2. OGX 方案

### 2.1 OGX 是什么

OGX 不只是检索流水线框架。它同时提供可运行的应用服务器、API、对象模型、Provider 路由、存储抽象和一部分后台任务能力。对知识库场景最有价值的是 Files、VectorStore 和 FileBatch 这一组现成能力。

OGX `1.3.0` 可直接提供：

| 能力 | OGX 中的表达 | 对本项目的价值 |
| --- | --- | --- |
| 原文件对象与上传 | `File`、Files API | 不必自建原文件 ID、元数据和上传流程 |
| 逻辑索引对象 | `VectorStore` | 可作为技术 KnowledgeBase 的内部对象 |
| 文件与索引关系 | `VectorStoreFile` | 记录某个 File 是否已进入某个逻辑索引 |
| 批量处理对象 | `FileBatch` | 表达异步处理状态和文件级失败 |
| 文件处理 | File Processor Provider | 可接入内置 Docling Provider |
| 模型调用 | Inference Router / Provider | 可把租户模型配置隐藏在内部模型资源后面 |
| 向量存储 | VectorIO Provider | 可接 Qdrant、Weaviate、pgvector 等后端 |
| 后台解析任务 | PostgreSQL 持久任务池 | 文件解析不阻塞 HTTP 请求，可在单实例重启后恢复 |
| 服务与配置 | Distribution YAML、FastAPI Server | 能裁剪出只启用知识库相关能力的 Distribution |

OGX 的 `File / VectorStore / VectorStoreFile / FileBatch` 是技术对象，不等于产品业务对象。比如企业版的“产品 A 知识库”仍先存在企业版数据库中，再映射到 OGX VectorStore；Stella 则可以只维护一个隐藏 VectorStore。

### 2.2 OGX 导入链路

```text
POST /knowledge/v1/ingest
        ↓
统一 API 保存幂等状态
        ↓
OGX Files 保存原文件
        ↓
创建单文件 FileBatch
        ↓
OGX 持久任务：Docling 解析 + HybridChunker
        ↓
OGX Inference Router → 租户 Embedding Provider
        ↓
自定义 Qdrant VectorIO Provider
        ↓
Dense 向量 + BM25 Sparse Vector + Payload 写入 Qdrant
```

需要准确理解 OGX `1.3.0` 的任务边界：持久任务覆盖 File Processor，也就是 Docling 解析和切块；Embedding 与 Qdrant 写入在 File Processor 返回后继续执行，并不是同一个完整数据库事务。我们可以通过幂等写入、状态快照和显式重试把它做成可恢复链路，但不能把它描述成端到端原子任务。

### 2.3 OGX 检索链路

```text
POST /knowledge/v1/search
        ↓
根据 KnowledgeBase 反查租户和向量空间
        ↓
强制组合：vector_store_id IN (...) AND 产品 filters
        ↓
同一 Qdrant Collection 内执行 Dense / BM25 / Hybrid RRF
        ↓
可选租户远程 Rerank；失败时降级为 RRF
        ↓
转换成稳定 SearchHit[]
```

### 2.4 OGX 路线仍需我们维护什么

| 需要维护的部分 | 原因 |
| --- | --- |
| 统一 Knowledge API | OGX 原生 API 暴露 File、VectorStore、FileBatch 细节，不是两侧稳定产品契约 |
| 自定义 Qdrant Provider | OGX 原生 Provider 不直接满足每租户 Collection、逻辑库 Payload 隔离、通用 Filter、原生 BM25 和一次跨逻辑库查询 |
| 租户感知 Inference Provider | 两个产品需要每租户独立 Embedding/Rerank URL、Key 和模型 |
| 产品对象映射 | OGX 不理解 Stella 四级范围或企业版业务知识库挂载 |
| 补充幂等、重试和删除语义 | 原生 FileBatch 不足以形成稳定的产品 Operation 契约 |
| 凭证、认证和错误协议 | 需要对产品提供不泄露内部实现的稳定边界 |

当前实现不修改 OGX Core，而是固定 OGX 版本，通过外置 Provider 和 External API 扩展。这降低了直接维护大型 Fork 的成本，但我们仍需在升级 OGX 时验证内部 VectorStore Mixin、任务和 Provider 接口是否变化。

### 2.5 OGX 路线的主要取舍

| 方面 | 收益 | 代价 |
| --- | --- | --- |
| 初次实现 | 复用 Files、VectorStore、FileBatch、Docling 和 Inference Router，较快形成完整服务 | 必须理解 OGX 内部对象和任务状态，调试链路较深 |
| 自有代码量 | 不必从零建设全部对象与任务层 | 自定义 Provider 与包装 API 会依赖 OGX 内部契约 |
| 灵活性 | Provider 可以替换存储和模型实现 | 流程仍受 OGX FileBatch 和 VectorStore 语义约束 |
| 升级 | 可选择固定版本，也可按收益跟进 | 每次升级必须重跑导入、恢复、删除和检索契约 |
| 长期维护 | 需求继续贴合 OGX 对象时维护面较小 | 若对象、任务或流程越来越偏离 OGX，适配成本会持续上升 |

## 3. Haystack 方案

### 3.1 Haystack 是什么

Haystack 的核心是组件和 Pipeline 编排框架。Pipeline 是有向多重图，可以连接转换器、切分器、Embedder、Retriever、Ranker 等组件；`AsyncPipeline` 可以在依赖允许时并行执行独立分支。

Haystack 的 `DocumentStore` 容易被误解。它不是 KnowledgeBase、文件管理服务或任务队列，而是对数据库或向量数据库的访问接口。官方协议主要包含：

- `count_documents`
- `filter_documents`
- `write_documents`
- `delete_documents`

Retriever 持有 DocumentStore 并执行查询。因此，`QdrantDocumentStore` 与 OGX 的 `File / VectorStore / FileBatch` 不在同一层级；它更接近 OGX VectorIO Provider 的数据库适配部分。

### 3.2 Haystack 可直接提供什么

| 能力 | Haystack 中的表达 | 说明 |
| --- | --- | --- |
| 流程编排 | `Pipeline` / `AsyncPipeline` | 显式连接导入和检索组件，可并行独立分支 |
| 统一文档对象 | `Document` | 保存 Chunk 文本、metadata、ID、Dense/Sparse Embedding |
| 数据库适配 | `DocumentStore` | 对接 Qdrant、Weaviate、Elasticsearch 等 |
| 写入 | `DocumentWriter` | 支持 overwrite、skip、fail 等重复策略 |
| 文档转换 | Converter 组件和集成 | 包括 Docling、DOCX、HTML、Markdown、PDF 等转换器 |
| 切分 | `DocumentSplitter` 或自定义组件 | 可显式控制切分、清洗、父子 Chunk 等规则 |
| Dense/Sparse Embedding | 模型组件和集成 | 可调用远程模型，也可接本地模型 |
| 检索 | 各 DocumentStore 对应 Retriever | Qdrant 集成提供 Dense、Sparse 和 Hybrid Retriever |
| 融合 | `QdrantHybridRetriever` 或 `DocumentJoiner` | 前者可在 Qdrant 侧做 Dense + Sparse RRF；后者便于自定义融合 |
| Rerank | Ranker 组件 | 可插在 Retriever 后面 |
| HTTP 暴露 | 可选 Hayhooks | 可将 Pipeline 包装成 HTTP/MCP，但不是完整知识库管理 API |

Haystack 官方 Qdrant Hybrid Retriever 接收 Dense 与 Sparse Query Embedding、支持 metadata filters，并用 RRF 融合。Sparse 向量如何生成仍由组件决定；使用 FastEmbed Sparse Embedder 不自动等同于我们当前选择的 Qdrant 原生 multilingual BM25。

### 3.3 Haystack 导入链路

若采用 Haystack，完整架构应是：

```text
POST /knowledge/v1/ingest
        ↓
我们建设的 FastAPI、KnowledgeBase/File/Operation 和幂等层
        ↓
我们选择的原文件存储
        ↓
我们选择的任务队列与 Worker
        ↓
Haystack Indexing Pipeline
        ↓
DoclingConverter → Chunker → Embedding → DocumentWriter
        ↓
QdrantDocumentStore
```

Haystack 负责编排 Pipeline 内的转换和写入，但不会替我们持久化“这个文件现在处理到哪一步”“失败能否重试”“哪个业务知识库拥有它”。

### 3.4 Haystack 检索链路

```text
POST /knowledge/v1/search
        ↓
我们建设的租户、KnowledgeBase 和 Filter 校验
        ↓
Haystack Query Pipeline
        ↓
Dense Embedder ──────────────┐
Sparse/BM25 Query Component ─┼→ Qdrant Hybrid Retriever / 自定义融合
                             ↓
                         可选 Ranker
                             ↓
                   我们转换成 SearchHit[]
```

### 3.5 Haystack 路线需要我们维护什么

| 需要维护的部分 | Haystack 是否提供 |
| --- | --- |
| KnowledgeBase、File、Operation 领域模型 | 不提供 |
| 原文件本地/S3 存储及引用生命周期 | 不提供统一产品层实现 |
| 异步任务、重试、租约、取消和崩溃恢复 | Pipeline 本身不提供持久任务语义 |
| FastAPI 产品契约、认证、幂等与错误码 | 不提供；Hayhooks 只能减少暴露 Pipeline 的样板代码 |
| 租户模型配置、凭证加密和向量空间锁定 | 不提供 |
| 租户 Collection 路由和业务 KnowledgeBase 映射 | 需要自行设计 |
| Qdrant Payload 结构和强制权限范围 | 需要配置集成或自定义组件 |
| 文件清理、整库删除和状态对账 | 需要自行实现 |

### 3.6 Haystack 路线的主要取舍

| 方面 | 收益 | 代价 |
| --- | --- | --- |
| 初次实现 | 每个解析、切块、检索步骤都可以明确组合 | 对象层、API、任务、状态和清理都要先建设，首期成本高 |
| 自有代码量 | 流程逻辑直接掌握在项目中，较少依赖框架内部对象 | 永久维护的业务基础设施代码明显更多 |
| 灵活性 | 易于插入自定义 Chunker、双路 Retriever、Rerank 和评测组件 | 灵活性不会自动带来正确的生命周期和一致性 |
| 升级 | Pipeline 和组件边界相对显式 | Haystack Core 与各集成包独立版本，也需要兼容性测试 |
| 长期维护 | 需求变化大、经常调整流程时耦合更低 | 如果需求长期稳定，自建任务和对象层可能比复用 OGX 更贵 |

## 4. 两套路线能力矩阵

| 维度 | OGX 路线 | Haystack 路线 |
| --- | --- | --- |
| 项目定位 | 应用服务器、资源模型、Provider 系统和部分任务基础设施 | 文档/检索组件与 Pipeline 编排框架 |
| File 对象与上传 | 内置 | 自建 |
| KnowledgeBase 对象 | 可用 VectorStore 映射 | 自建；DocumentStore 不是 KnowledgeBase |
| 文件处理状态 | FileBatch 提供基础状态，我们补稳定 Operation | 自建 |
| 持久任务 | File Processor 已有 PostgreSQL 任务池 | 自选队列与 Worker |
| 原文件本地/S3 | Files Provider 可选 | 自建适配或直接调用对象存储 SDK |
| Docling | 内置 Provider | 官方 Docling 集成组件 |
| 切块自由度 | 受 OGX Docling Provider 接口约束 | Pipeline 中可完全显式替换 |
| Embedding 路由 | Inference Router；租户化需自定义 Provider | 选择 Embedder 或自定义组件 |
| Qdrant | 需要自定义 Provider 才满足当前数据组织 | 官方 QdrantDocumentStore/Retriever 可作为基础，特殊组织仍可能扩展 |
| Dense + Sparse + RRF | 自定义 Provider 中使用一次 Qdrant Query | QdrantHybridRetriever 已提供；也可自行编排两路检索 |
| 产品稳定 API | 包装 OGX 原生对象 | 自建或用 Hayhooks 辅助暴露 Pipeline |
| 权限业务模型 | 产品负责 | 产品负责 |
| 初次成本 | 较低到中等 | 较高 |
| 框架耦合 | 较高，集中在 OGX 对象和 Mixin | 较低，集中在 Haystack 组件协议和集成包 |
| 自有维护面 | Provider、包装 API 和补充状态 | 完整对象层、任务层、API、存储生命周期和 Pipeline |

## 5. 初次成本与长期维护成本

不能简单断言“OGX 长期一定更贵”或“Haystack 长期一定更便宜”。更准确的判断是：

| 需求演化 | 更有利的路线 | 原因 |
| --- | --- | --- |
| 需求长期接近 File/VectorStore/FileBatch 模型 | OGX | 可持续复用对象、任务和原文件能力 |
| 需要频繁改变导入阶段、任务边界和领域对象 | Haystack | 自有边界更明确，不必绕过 OGX 语义 |
| 团队希望尽快交付完整服务 | OGX | 首期少建一层对象和任务基础设施 |
| 团队愿意承担较高首期成本换取最大流程控制权 | Haystack | Pipeline 和组件组合更直观 |
| 只想替换检索数据库或模型 | 两者都可 | OGX 用 Provider，Haystack 用 DocumentStore/Component |
| 需要多副本、独立扩缩 API 与 Worker | Haystack 自建路线更直接，但仍需设计 | 当前 OGX 方案基于单实例；Haystack 也不会自动解决分布式任务 |

从维护对象数量看：

```text
OGX：维护 Provider + 统一 API + 少量补充状态
Haystack：维护完整服务骨架 + 任务层 + 对象层 + Pipeline
```

从上游耦合看：

```text
OGX：自有代码较少，但更依赖 OGX 内部对象和任务行为
Haystack：自有代码较多，但每个流程步骤更显式、更容易单独替换
```

## 6. 什么时候选择哪一套

### 6.1 选择 OGX 的前提

- 接受 `File / VectorStore / VectorStoreFile / FileBatch` 作为技术对象模型。
- 接受单实例作为当前部署基线。
- 接受 File Processor 是持久任务，而 Embedding/Qdrant 写入通过幂等和重试补足。
- 愿意固定 OGX 版本，并维护一个外置 Qdrant Provider 和统一 Knowledge API。
- 相比完全控制流程，更看重复用现成对象、文件和任务能力。

### 6.2 选择 Haystack 的前提

- 不希望领域对象和任务行为受 OGX 约束。
- 愿意先建设 KnowledgeBase、File、Operation、原文件和可靠任务层。
- 预计解析、切块、召回、融合和 Rerank 流程会频繁演进。
- 团队愿意长期维护更多自有基础设施代码。

### 6.3 当前项目判断

当前项目已经选择 OGX `1.3.0` 完成 MVP，不是因为 OGX 原生能力无需改造，而是因为以下条件成立：

1. OGX 的 File、VectorStore、FileBatch 与当前技术对象基本可对齐。
2. 需要修改的核心差异可以集中在外置 Qdrant Provider、租户 Inference Provider 和统一 API。
3. 单实例满足目前一个客户一套 Stella、企业版和知识库服务的部署方式。
4. 已通过真实实现验证，OGX 的复用收益仍大于绕开其对象与任务语义的成本。

如果后续出现多副本、复杂 Revision、可编排导入阶段或大量自定义任务需求，应重新评估 Haystack 自建路线，而不是继续无上限扩展 OGX 适配层。

## 7. 参考资料

- [OGX v1.3.0 Release](https://github.com/ogx-ai/ogx/releases/tag/v1.3.0)
- [OGX OpenAI VectorStore Mixin](https://github.com/ogx-ai/ogx/blob/v1.3.0/src/ogx/providers/utils/memory/openai_vector_store_mixin.py)
- [OGX Docling File Processor](https://github.com/ogx-ai/ogx/blob/v1.3.0/src/ogx/providers/inline/file_processor/docling/docling.py)
- [OGX Durable Job 说明](https://github.com/ogx-ai/ogx/blob/v1.3.0/src/ogx/core/jobs/README.md)
- [Haystack Pipelines](https://docs.haystack.deepset.ai/docs/pipelines)
- [Haystack Document Store](https://docs.haystack.deepset.ai/docs/document-store)
- [Haystack QdrantDocumentStore](https://docs.haystack.deepset.ai/docs/qdrant-document-store)
- [Haystack QdrantHybridRetriever](https://docs.haystack.deepset.ai/docs/qdranthybridretriever)
- [Haystack Docling Integration](https://haystack.deepset.ai/integrations/docling)
- [Hayhooks](https://docs.haystack.deepset.ai/docs/hayhooks)

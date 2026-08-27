# 当前 OGX 方案：改造、能力、资源与优化项

> 更新日期：2026-08-26
>
> 代码基线：本仓库当前 `main` 工作区
>
> 状态：MVP 内核、统一 Knowledge API 和两侧调用模拟已经完成；生产运维项仍有待验证。

## 1. 当前技术基线

| 组件 | 当前版本或实现 | 作用 |
| --- | --- | --- |
| OGX | `1.3.0` | Files、VectorStore、FileBatch、File Processor、Inference Router、服务运行时 |
| Docling | `2.121.0` | 文档解析 |
| Chunker | Docling `HybridChunker` | 结构感知切块 |
| Qdrant | Server `1.18.2` / Client `1.18.0` | Dense、BM25 Sparse、Payload Filter 和 RRF |
| PostgreSQL | `17.10` | OGX 元数据、任务、幂等状态和加密后的 KnowledgeBase 模型配置 |
| Python / API | Python `3.12`、FastAPI | OGX Distribution 与统一 Knowledge API |
| 模型 | OpenAI-compatible 远程服务 | 每 KnowledgeBase 独立 Embedding 和可选 Rerank |
| 原文件 | `inline::localfs` 或 `remote::s3` | 部署时二选一，产品 API 不变 |

生产 Compose 只需要三个服务：

```text
knowledge-ogx  ── PostgreSQL
      │
      ├──────── Qdrant
      │
      └──────── 外部 Embedding / Rerank 服务
```

模型服务和已有 S3 不由本 Compose 启动。

## 2. 基于 OGX 做了哪些改造

本项目没有修改 OGX Core，而是固定 OGX `1.3.0`，通过外置 Provider、External API 和配置构造最小知识库 Distribution。

### 2.1 最小 Distribution

只显式启用当前链路需要的 API 和 Provider：

- Files
- File Processors
- Inference
- VectorIO
- 外置 Knowledge API

业务接入只使用 `/knowledge/v1/*`。OGX 原生接口保留给 Admin Token 做诊断和兼容验证，不作为 Stella 或企业版的产品契约。

### 2.2 自定义 Shared Qdrant Provider

这是对 OGX 原生 Qdrant Provider 的主要改造。

| 改造 | 当前实现 |
| --- | --- |
| 租户物理隔离 | 每个租户一个 Qdrant Collection；物理名使用 `tenant_id` 哈希，不暴露原业务 ID |
| 逻辑知识库 | 同租户的 OGX VectorStore 写入同一 Collection，以 `vector_store_id` Payload 区分 |
| Point ID | 使用 `vector_store_id + chunk_id` 生成稳定复合 UUID，防止不同逻辑库冲突 |
| Payload | 保存技术范围、File/Chunk ID、业务 attributes、可恢复的 OGX Chunk 和文本 |
| Payload Index | 固定索引 `vector_store_id / file_id / chunk_id`；业务高频字段由部署配置声明类型 |
| 通用过滤 | 支持 `and / or / eq / ne / in / nin / gt / gte / lt / lte`，并保护保留字段 |
| 范围强制 | 每个检索分支强制加入精确 `vector_store_id = knowledge_base_id` |
| Dynamic Named Vector | 每个 `model_id + dimension` 生成稳定 Named Vector；同一 Collection 可动态增加不同维度 |
| Dense | 调用当前 KnowledgeBase 的 Embedding，查询其 Named Vector |
| BM25 | Qdrant 原生 `qdrant/bm25`，multilingual tokenizer，动态 IDF |
| 单库 Hybrid | 一次 Qdrant Query 使用 Dense 与 BM25 两个 Prefetch，再用原生内层 RRF 融合 |
| 多知识库检索 | 每个库独立检索并按该库配置可选 Rerank；Python Search 层再做等权外层 RRF |
| 删除 | File 和 VectorStore 删除都带逻辑范围，不误删同 Collection 的其他知识库 |

### 2.3 KnowledgeBase 感知 Inference Provider

当前不是每个 KnowledgeBase 启动一个 Provider，而是一个 Provider 根据内部 Profile ID 动态解析连接：

```text
OGX 内部模型 ID
      ↓
KnowledgeBase Embedding / Rerank Profile
      ↓
从 PostgreSQL 读取 AES-GCM 密文
      ↓
使用部署 Master Key 在内存解密
      ↓
调用该 KnowledgeBase 配置的 OpenAI-compatible URL、Key 和模型
```

已实现：

- 每 KnowledgeBase 独立 Embedding URL、Key、模型 ID 和维度。
- 每 KnowledgeBase 独立 Rerank URL、Key、模型 ID 和开关。
- 不获取、不列举、不推荐远程模型列表。
- 配置写入前调用确切 `/embeddings` 或 `/rerank` 接口做探针。
- 空 KnowledgeBase 可修改 Embedding 模型和维度；首次 Ingest 接受后模型与维度锁定，URL/Key 仍可轮换。
- Rerank 不影响持久向量，可随时关闭或修改。
- 默认拒绝不安全的内网/HTTP 模型地址，部署方可显式放行。

### 2.4 统一 Knowledge API

在 OGX 同一个 FastAPI 进程中挂载了稳定产品接口，隐藏 OGX 原生对象细节：

- 携带模型配置创建、查询、删除技术 KnowledgeBase。
- 查询/修改 KnowledgeBase Embedding 与 Rerank 配置。
- 单文件异步 Ingest。
- Operation 状态查询和失败重试。
- File 列表、详情和删除。
- 单租户多 KnowledgeBase 的独立 Dense/BM25/Hybrid 分支与等权外层 RRF。
- Runtime/Admin 双 Token、请求 ID 和统一错误信封。

### 2.5 异步、恢复和生命周期补强

| 场景 | 当前处理 |
| --- | --- |
| 网络重试重复创建 KnowledgeBase | `Idempotency-Key` + 请求指纹，返回原对象 |
| 网络重试重复上传 | 文件内容、文件名、KnowledgeBase 和 attributes 参与指纹，恢复原 File/Batch |
| 异步状态 | 把 OGX FileBatch 映射为稳定 Operation |
| 最终失败重试 | 保留原文件，清理失败挂载，创建唯一子 Operation |
| OGX 正常重启 | 保留仍在处理的 Batch 为 `in_progress`，启动后恢复 |
| File 删除 | 处理中返回 `409 file_busy`；终态同步清理索引和最后一份原文件引用 |
| KnowledgeBase 删除 | 有在途任务返回 `409`；无任务时同步、幂等清理逻辑范围 |
| 无效原文件 | 启动即扫描并周期清理未提交、过期失败和孤儿 File |
| Rerank 上游失败 | 仅对应 KnowledgeBase 回退到 Qdrant 内层 RRF，不让外部模型故障中断整次 Search |

任务边界仍然是：Docling 解析和 HybridChunker 由 OGX PostgreSQL 持久任务执行；Embedding、BM25 编码和 Qdrant Upsert 不在同一个持久任务事务中。当前通过幂等、状态快照和显式重试恢复，不宣称跨 PostgreSQL/Qdrant 原子提交。

### 2.6 构建与交付改造

- 完整源码和 Docker Compose 交付，不提供固化业务代码的成品 Knowledge 镜像。
- Python、OGX、Docling、Qdrant Client 和模型资产固定版本或 commit。
- 构建时预下载当前 PDF Pipeline 使用的 layout、Accurate TableFormer 和 HybridChunker tokenizer。
- 运行时启用 Hugging Face/Transformers 离线模式，不临时下载模型。
- 默认使用可配置的国内 Debian、PyPI 和 Hugging Face 镜像。
- 交付脚本兼容 macOS 自带 Bash 3.2 与 BSD 工具；正式 arm64 全链路仍待真机验证。

## 3. 当前已实现的功能

### 3.1 功能矩阵

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 每 KnowledgeBase Embedding 配置 | 已实现 | 创建时提交；空库可换模型/维度，首次 Ingest 后锁定，URL/Key 可轮换 |
| 每 KnowledgeBase Rerank 配置 | 已实现 | 独立 URL、Key、模型和开关，可随时修改 |
| Dynamic Named Vector | 已实现 | 同租户不同知识库可在一个 Collection 中使用不同模型和维度 |
| 技术 KnowledgeBase 创建/查询/删除 | 已实现 | 创建和删除幂等；创建失败回滚；暂无列表、重命名接口 |
| 本地原文件存储 | 已实现并验证 | Docker named volume |
| S3/S3-compatible 原文件存储 | 已接入配置 | 真实生产 S3 尚未完成 E2E |
| 单文件异步 Ingest | 已实现 | 请求返回 `202`，后台继续处理 |
| Operation 查询 | 已实现 | `processing/completed/failed/cancelled` |
| 失败 Operation 重试 | 已实现 | 复用原文件，不要求重新上传 |
| File 查询、详情和删除 | 已实现 | Cursor 分页，支持状态和 attributes Filter |
| Docling 解析 | 已实现 | 当前关闭扫描 PDF OCR 和 VLM |
| Docling HybridChunker | 已实现 | 当前最终上限 1000 tokens，并前置上一基础 Chunk 最多 200 tokens |
| Dense 检索 | 已实现 | 每 KnowledgeBase 远程 Embedding 和 Named Vector |
| BM25 检索 | 已实现 | Qdrant 原生 multilingual BM25 + 动态 IDF |
| Hybrid 检索 | 已实现 | Qdrant 原生 RRF |
| 可选远程 Rerank | 已实现 | 每 KnowledgeBase 独立配置；仅用于 Hybrid，局部失败降级 |
| 同租户多 KnowledgeBase Search | 已实现 | 各库本地检索/可选重排后，Python 等权外层 RRF |
| 跨租户隔离 | 已实现 | 每租户 Collection；跨租户 Search 整体拒绝 |
| Stella 四级范围 Filter | 已验证 | System、Agent、User、User-Agent 累加与交叉隔离 |
| 企业版挂载检索 | 已验证 | 公司/产品库单挂载、多挂载、无挂载和双租户 |
| 重启与故障恢复 | MVP 已验证 | OGX/PostgreSQL/Qdrant 重启、Embedding 失败和 Worker 租约 |
| 无效原文件回收 | 已实现 | 启动扫描 + 周期扫描 |

### 3.2 当前明确不提供

- 产品用户、组织、角色和权限管理。
- 业务 KnowledgeBase 名称、列表、重命名和挂载关系。
- 原文件下载接口。
- 文件替换、Revision 或已有文件原地修改属性。
- Operation 取消。
- 跨租户 Search 或跨 Collection 融合。
- 已有数据直接切换 Embedding 模型或维度。
- 扫描 PDF OCR、VLM 解析和图片候选 Rerank。
- 多副本 OGX 服务承诺。

## 4. 当前文件解析支持范围

“Docling 理论支持的格式”“OGX Docling Provider 声明的 MIME”和“本项目已经验证的格式”不能混为一谈。

### 4.1 已经在本项目验证

| 格式 | 本轮样本特征 | 统一 API 验证 |
| --- | --- | --- |
| Markdown `.md` | 标题、有序列表、表格 | 通过 |
| 纯文本 `.txt` | 中文段落 | 通过 |
| HTML `.html` | 标题层级、列表、原生表格 | 通过 |
| 数字 PDF `.pdf` | 两页可提取文本、表格、页眉页脚 | 通过 |
| DOCX `.docx` | 两页、标题层级、列表、表格、页眉页脚 | 通过 |
| PPTX `.pptx` | 三页、时间线、原生表格 | 通过 |
| XLSX `.xlsx` | 两个工作表、公式、原生表格、日期和百分比 | 通过 |
| CSV `.csv` | 中文表头和多行数据 | 通过 |

以上八个文件都通过同一套测试：单文件异步 Ingest 进入 `completed`，然后以唯一标记和文件 Payload Filter 验证 BM25 / Dense / Hybrid，并用答案探针确认解析后的正文可检索。连续三轮均为 `8/8` 通过。Dense 使用确定性测试 Stub，所以这是链路与协议验证，不是语义召回质量评测。

### 4.2 Provider 声明可接收，但当前不承诺文本提取

| 格式 | OGX 声明的 MIME | 当前处理原则 |
| --- | --- | --- |
| JPEG / PNG / GIF / BMP / TIFF / WebP | 对应 `image/*` | Provider 声明可接收，但当前关闭 OCR 且未配置 VLM，不承诺可用文本提取 |

### 4.3 当前不应宣称支持的情况

- 扫描 PDF：当前 `do_ocr=false`，很可能只能得到极少或空文本。
- 依赖 VLM 的复杂页面理解：当前 `vlm_model=null`。
- 密码保护、损坏、特殊编码文件和旧版 `.doc/.ppt/.xls`：未形成支持矩阵。
- 动态 JavaScript 页面、Office 复杂嵌入对象、宏、超大文件和复杂版式：当前小型样本不能支撑承诺。
- Docling 枚举中其他格式，如 EPUB、邮件、音视频等：虽然 Docling Core 可能具有相关能力，但 OGX 当前适配器和本项目均未验证。

上传接口目前不会根据扩展名提前维护一张完整白名单；无法解析的文件会在异步 Operation 中变为 `failed`。正式产品 UI 应只展示已经验证并承诺的格式，而不是直接展示 Docling 的全部理论格式。

## 5. 实际资源与性能数据

### 5.1 测试环境与限制

主要资源报告来自：

- WSL2 Linux x86_64。
- 12 个逻辑 CPU。
- 3 维确定性 Embedding/Rerank Stub。
- 小型 Markdown 文件。
- `docker stats` 容器采样；CPU `100%` 约等于一个逻辑 CPU 核。

因此以下数据只适合判断本地组件量级、接口路径和并发行为，不是容量测试、生产配额或 SLA，也不包含远程模型服务自身的 CPU/内存成本。

### 5.2 空闲基线

| 服务 | CPU 平均 | CPU P95 | 内存平均 | 内存最大 |
| --- | ---: | ---: | ---: | ---: |
| Knowledge OGX | 4.53% | 10.62% | 1160.19 MiB | 1160.19 MiB |
| PostgreSQL | 0.96% | 2.80% | 63.34 MiB | 63.90 MiB |
| Qdrant | 1.32% | 1.98% | 152.76 MiB | 153.00 MiB |

OGX 的约 1.1 GiB 基线主要来自 Python、Docling、Torch、Transformers 和已初始化解析 Pipeline，不是 OGX 业务对象本身的纯框架开销。

### 5.3 小文件导入阶段

| 场景 | 服务 | CPU 平均 | CPU 最大 | 内存最大 |
| --- | --- | ---: | ---: | ---: |
| Stella 9 个小型 Markdown | Knowledge OGX | 11.63% | 20.75% | 1161.22 MiB |
| Stella 9 个小型 Markdown | PostgreSQL | 3.03% | 5.44% | 65.67 MiB |
| Stella 9 个小型 Markdown | Qdrant | 3.99% | 14.13% | 156.70 MiB |
| 企业版 6 个小型 Markdown | Knowledge OGX | 13.86% | 23.08% | 1156.10 MiB |
| 企业版 6 个小型 Markdown | PostgreSQL | 2.94% | 4.09% | 63.95 MiB |
| 企业版 6 个小型 Markdown | Qdrant | 9.22% | 25.01% | 159.20 MiB |

早期单页数字 PDF 测量中，OGX 冷启动稳定后约 `1.03 GiB`，完成导入后约 `1.54 GiB`。新的七类文件验证进一步表明，不能把首个 Worker 加载后的数字当成最终稳态：OGX `1.3.0` 默认启动两个任务 Worker，两者都懒加载 Docling 模型后容器常驻约 `2.2 GiB`。

### 5.4 七类文件导入与资源观察

在新建的隔离 Compose 项目中依次导入八个小型样本，并连续执行三轮完整矩阵：

| 观察项 | 结果 |
| --- | ---: |
| 每轮格式结果 | `8/8` 通过 |
| 数字 PDF 单次导入 | 约 `4.8 s` |
| OGX 冷态内存 | 约 `1.07 GiB` |
| OGX 两 Worker 预热后常驻 | 约 `2.2 GiB` |
| OGX 瞬时内存峰值 | 约 `2.46 GiB` |
| OGX 瞬时 CPU 峰值 | 约 `815%` |
| Qdrant 预热基线 / 峰值 | 约 `106 / 113 MiB` |
| PostgreSQL 预热基线 / 峰值 | 约 `62 / 65 MiB` |

Docker CPU `100%` 约等于一个逻辑核，所以 `815%` 是解析期的短时多核峰值，不是长时间平均占用。前两轮稳态内存分别上升，是两个 Worker 分别首次加载 Docling 模型；第三轮没有继续出现同等幅度增长。这可以解释本次短测，但不代替长时稳定性和大文件压测。

### 5.5 混合检索并发

2026-08-26 在新链路下重跑 700 个 Hybrid 请求、并发 8。负载混合 Stella 单库与企业版单库/双库/三库请求；企业版同租户三个库分别使用 3/5/7 维 Embedding，其中两个库使用不同 Rerank Key/模型，第三个库不启用 Rerank：

| 指标 | 结果 |
| --- | ---: |
| 总耗时 | 49.627 s |
| 吞吐 | 14.11 req/s |
| 平均延迟 | 564.34 ms |
| P50 | 499.23 ms |
| P95 | 979.30 ms |
| 最大延迟 | 1279.39 ms |

| 服务 | CPU 平均 | CPU P95 | 内存最大 |
| --- | ---: | ---: | ---: |
| Knowledge OGX | 98.59% | 107.52% | 1181.70 MiB |
| PostgreSQL | 5.84% | 7.74% | 75.09 MiB |
| Qdrant | 5.81% | 7.14% | 836.50 MiB |

单次正确性矩阵中，企业版 Hybrid 的代表延迟为：单库约 `70～109 ms`、双库约 `82～111 ms`、三库约 `134～135 ms`。这不是严格的逐级基准，只能证明分支数增加会增加模型调用和融合开销。

改造前同一组 700 请求基线约 `62.95 req/s`、P95 `181.76 ms`；新链路为了支持每库独立模型，必须按库执行模型与 Qdrant 分支，不能再把多个库合并为一次查询，因此性能下降是实际架构代价。默认并发上限 8 防止无界扇出，后续应按真实模型延迟和常见挂载库数量调优。

这轮仍使用本地确定性模型 Stub，因此真实远程 Embedding/Rerank 的网络和模型延迟通常会更高。Qdrant 在本轮之前已经经过动态 Schema、重启与故障测试，进程分配内存未回落到冷启动水平，`836.50 MiB` 不能与旧的 `165.70 MiB` 直接归因比较；判断 Named Vector 数量的长期内存/磁盘成本仍需专门的全新进程曲线测试。

### 5.6 真实远程 Embedding 链路参考

使用 5 份项目文档的单次评测：

| 模型量级 / 维度 | 5 文档总导入 | Hybrid 平均延迟 |
| --- | ---: | ---: |
| 0.6B / 1024 | 11.47 s | 648 ms |
| 4B / 2560 | 13.61 s | 738 ms |
| 8B / 4096 | 35.33 s | 5514 ms |

三组模型都完成 8 条真实查询的 8/8 Top1，但样本太小，不能据此确定生产模型。数据同时包含模型服务、网络和知识库调用，不能用于单独比较 OGX 或 Qdrant 性能。

### 5.7 镜像大小

- 当前 amd64 Knowledge 镜像约 `1.08 GB`。
- 早期包含未使用 ONNX/Fast TableFormer 变体时约 `4.55 GB`。
- 当前只保留 PDF 默认路径需要的 Transformers layout、Accurate TableFormer 和 HybridChunker tokenizer。

## 6. 当前参数与可调范围

| 参数 | 当前值 | 配置位置/方式 | 当前限制 |
| --- | --- | --- | --- |
| Embedding 模型 | 每 KnowledgeBase 配置 | 创建请求 / Admin API | 第一次 Ingest 后锁定；空库可切换 |
| Embedding 维度 | 每 KnowledgeBase `1～65536`，可探测 | 创建请求 / Admin API | 第一次 Ingest 后锁定；不同维度使用不同 Named Vector |
| Chunk 最终上限 | 1000 tokens | `config/ogx.yaml` | 所有租户共用；产品请求不能单独指定 |
| Chunk overlap | 最多 200 tokens | `config/ogx.yaml` + 固定 OGX 构建补丁 | HybridChunker 使用 800-token 新内容预算，再前置上一基础 Chunk 尾部；原子结构已经超限时不再追加 overlap |
| Chunk tokenizer | `all-MiniLM-L6-v2` 固定 revision | Docker 构建参数 | 只用于切块计数，与实际 Embedding 模型可能不同 |
| Search `limit` | 默认 10，范围 1～50 | 每次 Search 请求 | Dense/BM25 最终 TopK |
| Hybrid Prefetch | `max(k × 4, 20)` | Provider 硬编码 | Dense/BM25 两路相同候选上限 |
| 单库 Rerank 候选数 | 默认 50，范围 1～200 | `RERANK_CANDIDATE_LIMIT` | 当前 KnowledgeBase 启用 Rerank 时生效 |
| 跨库分支候选数 | 默认 50，范围 1～200 | `KNOWLEDGE_SEARCH_BRANCH_CANDIDATE_LIMIT` | 多 KnowledgeBase 外层融合前每库保留的候选数 |
| 跨库并发 | 默认 8 | `KNOWLEDGE_SEARCH_BRANCH_CONCURRENCY` | 限制一次 Search 同时发起的独立知识库分支 |
| RRF | Qdrant 内层 RRF + Python 外层 RRF `k=60` | `KNOWLEDGE_OUTER_RRF_K` | 多库等权；当前未开放每库权重 |
| BM25 | multilingual tokenizer、`language=none` | Provider 固定 | 升级 Qdrant 或更换 tokenizer 必须重建并重评 |
| 上传大小 | 100 MiB | OGX API 默认值 | 当前没有产品级覆盖配置 |
| 高性能 Filter 字段 | 部署声明类型 | `payload_indexes` | 范围比较只允许声明为 integer/float/datetime 的字段 |

## 7. 仍可打磨和优化的部分

### 7.1 高优先级：超大文档 Embedding 分批

当前 OGX 会把一个文件产生的所有 Chunk 一次性提交给 Embedding 服务。已观察到含 5000 个独立标题和段落的 HTML 可以完成 Docling 解析，但 Embedding 请求因列表长度限制失败。

建议：

1. 增加可配置的 Embedding batch size。
2. 每批使用稳定 Chunk ID 幂等写入。
3. 明确某一批失败时的残留 Point 清理和重试语义。
4. 用真实大 PDF、DOCX 和 HTML 验证内存峰值。

### 7.2 高优先级：继续评测 1000/200 切块规则

OGX `1.3.0` 原生 Docling Provider 不使用已存在的 overlap 配置。本项目在固定版本镜像构建时应用最小补丁：HybridChunker 先按 800-token 新内容预算保持结构切块，再把上一基础 Chunk 末尾最多 200 tokens 前置到当前 Chunk，最终目标不超过 1000 tokens。补丁使用原文 offset mapping，不通过 tokenizer decode 改写表格或标点。

真实 Markdown 教材验证中，原本分离的“表 5-1”标题与表体被合入同一 728-token Chunk。查询“输出表5-1内容”时，该 Chunk 在纯 BM25 排第 28、Dense 排第 36，进入 50 条候选后由现有 Rerank 提升到 Hybrid 第 1。这个结果证明 overlap 解决了当前内容完整性问题，但不证明 1000/200 已适合所有文档。

仍需继续覆盖：

- 标题、代码块、跨页表格和超长不可拆结构。
- 普通问答相对原切块的召回回归、索引体积和 Embedding/Rerank 成本。
- 是否需要让 Chunk tokenizer 与实际 Embedding tokenizer 对齐。

首期继续保持部署级统一规则，不把任意 Chunk 参数开放给每个 Ingest，避免同一服务混入不同切块语义。

### 7.3 高优先级：扩大中文 BM25 评测

当前生产路径使用 Qdrant 原生 multilingual BM25。Jieba 搜索分词 + whitespace tokenizer 只保留在评测依赖中，两者都由 Qdrant 计算 BM25 与动态 IDF。小型工程语料中两者均为 9/9 Top1；上述真实教材查询中，完整表格 Chunk 的纯 BM25 排名分别为 multilingual 第 28、Jieba 第 17。Jieba有所改善但仍未进入 Top10，暂不足以支持增加生产依赖和全量重建 Sparse Vector。

下一轮应覆盖：

- 中文自然语言和长词。
- 公司、部门、产品专有名词。
- 中英混合、型号、错误码、订单号和精确标识符。
- 同义词、空格和标点差异。
- 大语料下 Recall@K、MRR、索引体积和延迟。

只有原生 multilingual 明显不满足目标语料时，才考虑切换 Jieba 或其他 Sparse Encoder；切换会要求全量重建 Sparse Vector，不能作为无成本运行时开关。

### 7.4 中优先级：TopK、RRF 与 Rerank 参数

当前参数偏保守且易理解，但没有按真实语料调优：

```text
无 Rerank：
Dense Top max(limit × 4, 20) ─┐
BM25  Top max(limit × 4, 20) ─┴→ RRF → limit

有 Rerank，默认 limit=10：
Dense Top 200 ─┐
BM25  Top 200 ─┴→ RRF Top 50 → Rerank Top 10
```

建议用真实语料比较：

- Dense 与 BM25 是否需要不同 Prefetch TopK。
- `4×` 倍数和最小 20 是否过小或浪费。
- Rerank 候选 20/50/100 的效果、延迟和费用。
- 是否需要开放部署级 RRF `k` 或权重。

不建议首期把所有内部候选参数直接暴露给产品 Search API；优先做部署级配置和离线评测，外部 API 保持稳定。

### 7.5 中优先级：Embedding 模型和维度

- 维度越大，Qdrant Dense 存储、内存、网络和计算量通常近似随维度增长。
- 同维度的不同模型也不共享向量空间，不能直接切换。
- 当前在首次 Ingest 后锁定是正确约束。
- 应在真实中文语料上联合比较质量、远程延迟、调用成本和 Qdrant 资源，而不是只按模型参数量选择。

当前不需要提前实现模型迁移工具；只有业务明确要求已有租户切换模型时，再设计全量重向量化和 Collection 切换。

### 7.6 文档格式与解析质量

- 当前 DOCX、PPTX、XLSX、CSV 和两页数字 PDF 已完成小型样本 E2E；下一步应补大文件、复杂版式、嵌入对象和异常文件。
- OCR 不是首期必备；若启用，应把模型资产、镜像大小、CPU/内存和扫描件质量一起评估。
- 两个 Worker 都加载 Docling 后约 `2.2 GiB` 常驻已是真实部署需要评估的成本；先比较单 Worker 修改的内存/吞吐取舍，不因此直接新增独立解析服务。

### 7.7 生产运维

仍需在真实部署完成：

- S3 后端 E2E。
- PostgreSQL、Qdrant Snapshot 和原文件一致性备份恢复。
- 请求、Operation、Embedding、Qdrant 和清理指标。
- 更大业务语料和长时间稳定性测试。
- Qdrant Upsert 窗口进程终止的专项故障注入。
- Apple Silicon 真机构建、导入和检索验证。

## 8. 当前结论

当前实现已经证明 OGX 路线可以满足两侧的核心知识库链路，主要改造面集中在 Shared Qdrant Provider、租户 Inference Provider 和统一 Knowledge API，并没有演变成修改 OGX Core 的大型 Fork。

现阶段最值得继续投入的不是增加更多管理概念，而是：

1. 解决超大文档 Embedding 分批。
2. 用真实语料确定 BM25、Chunk 和 TopK 参数。
3. 补齐承诺文件格式的 E2E。
4. 完成 S3、备份、监控和真实部署验证。

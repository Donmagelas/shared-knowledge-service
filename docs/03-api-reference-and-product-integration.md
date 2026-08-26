# Knowledge API Reference 与产品接入

本文描述当前代码已经实现的统一 Knowledge API，以及 Stella 和 Cherry Studio 企业版应如何使用。它是当前实现契约，不是未来接口草案。

当前版本前缀：**/knowledge/v1**

默认本地地址：**http://localhost:8321**

## 1. 边界与核心对象

### 1.1 API 面向谁

Knowledge API 只面向 Stella 后端和 Cherry Studio 企业版后端，不直接面向终端用户。两侧先完成用户认证、权限计算和业务对象管理，再以服务身份调用本 API。

统一知识库不提供用户、组织、角色、权限分配或“挂载关系”管理。

### 1.2 对象关系

| 对象 | 含义 | 持久化表达 |
| --- | --- | --- |
| Tenant | 相互隔离的向量数据空间和存储路由 | 一个独立 Qdrant Collection；物理名称由服务生成 |
| KnowledgeBase | 可独立配置模型、上传、查询和删除的技术知识库 | OGX VectorStore；在租户 Collection 中写为 vector_store_id Payload |
| File | 一份不可变的原始上传文件 | 原文件在本地文件系统或 S3；技术元数据在 PostgreSQL/OGX |
| Operation | 一次异步导入尝试 | 对单文件 OGX FileBatch 的稳定包装 |
| Chunk | 文件解析、切分后的检索单元 | Qdrant Point，包含 Dense、BM25 Sparse Vector 和 Payload |
| attributes | 产品写入的通用业务过滤属性 | Qdrant 的 attributes.<字段> Payload |

关键关系如下：

~~~text
Tenant
├── 一个 Qdrant Collection
└── 多个技术 KnowledgeBase
    ├── 一个 Embedding 配置
    ├── 一个可选 Rerank 配置
    └── 多个 File
        ├── 一个当前 Operation
        └── 多个 Chunk
~~~

KnowledgeBase 是技术对象，不等于两侧的完整业务对象：

- Stella 通常只创建一个不向用户展示的隐藏 KnowledgeBase，四级范围写入 attributes。
- 企业版为公司、部门、产品等显式业务知识库各创建一个技术 KnowledgeBase，并在自己的数据库中保存一对一映射。

## 2. 通用协议

### 2.1 鉴权

所有公开接口都要求：

~~~http
Authorization: Bearer <service-token>
~~~

部署时配置两种服务 Token：

| Token | 权限 |
| --- | --- |
| Runtime Token | KnowledgeBase、Ingest、Operation、File 和 Search 接口 |
| Admin Token | Runtime Token 的超集；另外可查询和修改 KnowledgeBase 的 Embedding/Rerank 配置 |

产品运行时应使用 Runtime Token；只有可信配置流程使用 Admin Token。OGX 原生接口不是稳定产品契约，并且只允许 Admin Token 访问。

### 2.2 请求 ID

调用方可选传入：

~~~http
X-Request-ID: product-generated-id
~~~

允许字母、数字、连字符、下划线和点，最长 128 字符。不传或格式不合法时，服务自动生成。所有响应都会回传 X-Request-ID；错误正文同时包含 request_id。

### 2.3 幂等

以下接口要求 Idempotency-Key：

- 创建技术 KnowledgeBase。
- 提交文件 Ingest。

相同 Key 与相同请求重复调用时返回第一次创建的对象，HTTP 状态由首次的 201/202 变为 200。相同 Key 用于不同请求时返回 409 idempotency_conflict。

建议产品以稳定业务动作 ID 作为 Key，例如：

- 创建知识库：create-kb:<业务知识库 ID>
- 上传文件：ingest:<业务文件 ID>:<内容版本或哈希>

不要为自动重试生成新 Key，否则服务会把它当成新的知识库或文件。

### 2.4 JSON 与 multipart

除 Ingest 外，请求和响应均使用 application/json。

Ingest 使用 multipart/form-data，因为一个请求同时包含文件二进制和结构化字段。调用方不需要先上传 File 再发第二次 Ingest 请求。

### 2.5 错误格式

所有同步错误使用稳定信封：

~~~json
{
  "error": {
    "code": "invalid_request",
    "message": "请求字段不合法",
    "details": {}
  },
  "request_id": "req_0123456789"
}
~~~

产品分支逻辑只能依赖 error.code；message 是开发者说明，可能调整。

异步导入接受后发生的错误不通过原 Ingest 请求返回，而体现在 Operation 的 status=failed 和 last_error 中。

## 3. KnowledgeBase 模型配置

Embedding 和 Rerank 都属于技术 KnowledgeBase，而不是 Tenant。Tenant 只决定 Qdrant Collection 路由；同一 Collection 中的不同 KnowledgeBase 可以使用不同模型、维度、URL 和 Key。

服务不提供模型列表，只接受调用方提交的 OpenAI-compatible `base_url / api_key / model_id`。Embedding 的 `dimension` 可选：省略时服务会立即调用一次 Embedding 并以真实响应长度确定维度；显式提交时，探针结果不一致会拒绝请求。

### 3.1 查询模型配置

**GET /knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config**

权限：Admin Token。

响应包含 Embedding、可选 Rerank 及锁定状态，永远不返回明文 API Key：

~~~json
{
  "knowledge_base_id": "vs_example",
  "embedding": {
    "base_url": "https://model.example.com/v1",
    "model_id": "embedding-model-id",
    "dimension": 1024,
    "credential_configured": true,
    "locked": false,
    "updated_at": "2026-08-25T10:00:00Z"
  },
  "rerank": {
    "enabled": true,
    "base_url": "https://model.example.com/v1",
    "model_id": "rerank-model-id",
    "credential_configured": true,
    "updated_at": "2026-08-25T10:00:00Z"
  }
}
~~~

### 3.2 修改 Embedding

**PUT /knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config**

权限：Admin Token。每次都完整提交 `base_url / api_key / model_id`，`dimension` 可省略。

~~~json
{
  "base_url": "https://model.example.com/v1",
  "api_key": "<embedding-key>",
  "model_id": "embedding-model-id",
  "dimension": 1024
}
~~~

锁定规则：

1. 空 KnowledgeBase 可以更换 Embedding 模型和维度；服务会按 `model_id + dimension` 在租户 Collection 中创建或复用动态 Named Vector。
2. 首次 Ingest 接受后 `locked=true`。此后禁止直接修改模型 ID 或维度，当前不提供全量重新向量化。
3. 锁定后仍可在保持模型 ID 与维度不变的前提下轮换 URL 和 Key。
4. 不同 NewAPI 实例中的相同模型 ID 必须表达同一种 Embedding；这是调用方的部署契约。

### 3.3 修改或关闭 Rerank

**PUT /knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config**

权限：Admin Token。启用时必须完整提交独立的 URL、Key 和模型 ID：

~~~json
{
  "enabled": true,
  "base_url": "https://model.example.com/v1",
  "api_key": "<rerank-key>",
  "model_id": "rerank-model-id"
}
~~~

关闭时只能提交：

~~~json
{"enabled": false}
~~~

Rerank 不决定持久索引，因此可随时启用、修改或关闭。它只作用于 `mode=hybrid`；单个 KnowledgeBase 的远程 Rerank 失败时，该库退回 Qdrant 内层 RRF 排名。`dense` 和 `bm25` 不执行 Rerank。

## 4. KnowledgeBase API

### 4.1 创建技术 KnowledgeBase

**POST /knowledge/v1/knowledge-bases**

权限：Runtime 或 Admin Token。要求 Idempotency-Key。

`tenant_id` 决定 Collection 路由，`embedding` 为必填完整配置，`rerank` 整段可省略。`embedding.dimension` 可省略并由服务探测。Embedding 与 Rerank 可以使用相同或不同的 URL/Key。

名称、描述、公司/部门/产品归属等业务字段由产品数据库维护，不复制到技术 KnowledgeBase。

Stella 示例：在部署初始化时创建唯一的隐藏 KnowledgeBase。`Idempotency-Key` 必须在同一 Stella 部署内保持稳定；返回的 `knowledge_base_id` 存入 Stella 配置或数据库，不向普通用户展示。

~~~bash
curl -X POST "http://localhost:8321/knowledge/v1/knowledge-bases" \
  -H "Authorization: Bearer <runtime-token>" \
  -H "Idempotency-Key: create-stella-hidden-kb-v1" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "stella",
    "embedding": {
      "base_url": "https://newapi.example.com/v1",
      "api_key": "<embedding-key>",
      "model_id": "embedding-model-id",
      "dimension": 1024
    }
  }'
~~~

该示例没有配置 Rerank；需要时在创建请求中增加完整 `rerank` 对象。由于 Stella 只有一个隐藏 KnowledgeBase，这份模型配置就是当前部署的全局知识库模型配置。

企业版示例：企业版后端先创建公司、部门或产品等业务 KnowledgeBase，再为该业务对象创建一一对应的技术 KnowledgeBase。每个业务 KnowledgeBase 都可以提交自己的 Embedding 与 Rerank 配置。

~~~bash
curl -X POST "http://localhost:8321/knowledge/v1/knowledge-bases" \
  -H "Authorization: Bearer <runtime-token>" \
  -H "Idempotency-Key: create-kb-business-kb-123" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "enterprise-customer-42",
    "embedding": {
      "base_url": "https://newapi.example.com/v1",
      "api_key": "<embedding-key>",
      "model_id": "embedding-model-id",
      "dimension": 1536
    },
    "rerank": {
      "base_url": "https://newapi.example.com/v1",
      "api_key": "<rerank-key>",
      "model_id": "rerank-model-id"
    }
  }'
~~~

企业版把返回的 `knowledge_base_id` 与自己的 `business_kb_id` 一一保存。Embedding 与 Rerank 使用同一 NewAPI 连接时，两组 URL/Key 仍分别完整提交；服务不增加共享凭证实体。名称、描述、所属公司/部门/产品和挂载关系继续只保存在企业版数据库。

成功：

- 首次创建：201 Created。
- 幂等重放：200 OK。

~~~json
{
  "knowledge_base_id": "vs_7e62f5a0-8f4d-4fa0-ae2f-6ff81fc41068",
  "tenant_id": "tenant-a",
  "embedding": {
    "model_id": "embedding-model-id",
    "dimension": 1024,
    "locked": false
  },
  "rerank": {
    "enabled": true,
    "base_url": "https://model.example.com/v1",
    "model_id": "rerank-model-id",
    "credential_configured": true,
    "updated_at": "2026-08-25T10:00:00Z"
  },
  "file_counts": {
    "total": 0,
    "processing": 0,
    "completed": 0,
    "failed": 0
  },
  "created_at": "2026-08-25T10:00:00Z"
}
~~~

创建时会先探测上游模型；探针失败则不留下半成品 KnowledgeBase，调用方修正配置后可用同一个 Idempotency-Key 重试。请求幂等指纹包含凭证摘要，但任何响应都不会返回明文 Key。

### 4.2 查询技术 KnowledgeBase

**GET /knowledge/v1/knowledge-bases/{knowledge_base_id}**

权限：Runtime 或 Admin Token。

返回创建响应同样的技术信息和实时文件状态计数。该接口用于产品对账和部署自检，不是业务知识库列表接口。

### 4.3 删除技术 KnowledgeBase

**DELETE /knowledge/v1/knowledge-bases/{knowledge_base_id}**

权限：Runtime 或 Admin Token。

成功返回 204 No Content；重复删除也视为成功。它会清理该技术 KnowledgeBase 的文件、原文件引用和 Qdrant Chunk，不删除整个租户 Collection，也不影响同租户其他 KnowledgeBase。

如果仍有 processing Operation，返回 409 knowledge_base_busy，不提供强制删除。

## 5. Ingest 与 Operation API

### 5.1 异步提交文件

**POST /knowledge/v1/ingest**

权限：Runtime 或 Admin Token。要求 Idempotency-Key。Content-Type 为 multipart/form-data。

表单字段：

| 字段 | 必填 | 约束 | 含义 |
| --- | --- | --- | --- |
| file | 是 | 默认最大 100 MiB | 原始文件二进制；文件名同时参与幂等指纹 |
| knowledge_base_id | 是 | 已存在的技术 ID | 文件归属的逻辑 KnowledgeBase，不是 Qdrant Collection ID |
| attributes | 否 | JSON 对象字符串 | 产品生成的过滤属性 |

两端的差异只在 `attributes`：Stella 用它表达四级范围；企业版的公司/部门/产品归属已由业务 KnowledgeBase 表达，没有额外文件分类需求时不用传 `attributes`。

Stella 示例：

~~~bash
# curl 会自动生成 multipart boundary，不要手工设置 Content-Type。
curl -X POST "http://localhost:8321/knowledge/v1/ingest" \
  -H "Authorization: Bearer <runtime-token>" \
  -H "Idempotency-Key: stella-upload-123-v1" \
  -F "knowledge_base_id=vs_stella_hidden" \
  -F 'attributes={"scope":"user_agent","user_id":"user-1","agent_id":"agent-1"}' \
  -F "file=@./example.pdf"
~~~

企业版示例：

~~~bash
curl -X POST "http://localhost:8321/knowledge/v1/ingest" \
  -H "Authorization: Bearer <runtime-token>" \
  -H "Idempotency-Key: enterprise-upload-123-v1" \
  -F "knowledge_base_id=vs_product_a" \
  -F "file=@./example.pdf"
~~~

企业版只在同一业务知识库内还需要按文件来源、分类等字段检索时，才把少量可信字段写入 `attributes`。用户、组织、挂载和权限仍只由企业版数据库管理。

成功：

- 首次接受：202 Accepted。
- 幂等重放：200 OK。

~~~json
{
  "operation_id": "batch_0123456789",
  "file_id": "file_0123456789",
  "knowledge_base_id": "vs_7e62f5a0-8f4d-4fa0-ae2f-6ff81fc41068",
  "status": "processing"
}
~~~

202 的含义是原文件和任务状态已经可靠保存，不代表解析、Embedding 或索引完成。产品必须保存 operation_id 和 file_id，并查询 Operation。

attributes 限制：

- 最多 16 个字段。
- 字段名 1～64 字符。
- 值可以是字符串、整数、浮点数、布尔值，或这些标量的一维数组。
- 数组最多 64 项；字符串值最长 512 字符。
- 不能写入 file_id、tenant_id、vector_store_id、filename、chunk_id、content_text、attributes 等服务保留字段。

### 5.2 查询导入状态

**GET /knowledge/v1/operations/{operation_id}**

权限：Runtime 或 Admin Token。

`operation_id` 在服务内全局唯一。统一知识库通过 OperationRecord 解析对应的 `knowledge_base_id` 和 `file_id`，调用方不需要在路径中重复提供 KnowledgeBase。

~~~json
{
  "operation_id": "batch_0123456789",
  "knowledge_base_id": "vs_7e62f5a0-8f4d-4fa0-ae2f-6ff81fc41068",
  "file_id": "file_0123456789",
  "status": "completed",
  "created_at": "2026-08-25T10:00:00Z",
  "last_error": null,
  "retryable": false,
  "retried_from_operation_id": null,
  "retried_by_operation_id": null
}
~~~

status 取值：

| 状态 | 产品行为 |
| --- | --- |
| processing | 继续轮询；不要允许检索结果依赖该文件 |
| completed | 文件已经进入在线索引 |
| failed | 展示 last_error；retryable=true 时可调用 Retry |
| cancelled | 当前代码保留该状态兼容 OGX，但 V1 不提供取消接口 |

建议轮询使用退避，例如 1 秒、2 秒、5 秒，之后每 5～10 秒一次；不要高频固定轮询。

### 5.3 重试失败导入

**POST /knowledge/v1/operations/{operation_id}/retry**

权限：Runtime 或 Admin Token。无请求正文。

只允许重试最终 failed 且 retryable=true 的 Operation。服务复用已经保存的原文件，清理失败挂载并创建新的 Operation，因此不要求用户重新上传。

成功：

- 新建重试：202 Accepted。
- 同一失败 Operation 已经创建过重试：200 OK，并返回同一个新 Operation。

~~~json
{
  "operation_id": "batch_retry_0123456789",
  "knowledge_base_id": "vs_7e62f5a0-8f4d-4fa0-ae2f-6ff81fc41068",
  "file_id": "file_0123456789",
  "status": "processing",
  "retried_from_operation_id": "batch_0123456789"
}
~~~

## 6. File API

### 6.1 查询文件列表

**POST /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query**

权限：Runtime 或 Admin Token。

路径中的 `knowledge_base_id` 已把查询限制在一个技术 KnowledgeBase 内。`filters`、`statuses`、`cursor` 和 `limit` 都不是权限参数；产品必须先自行完成用户权限检查。

Stella 示例：只查询 User 范围中已完成或失败的文件。

~~~json
{
  "filters": {
    "type": "eq",
    "key": "scope",
    "value": "user"
  },
  "statuses": ["completed", "failed"],
  "cursor": null,
  "limit": 20
}
~~~

企业版示例：用户通过权限检查后，查询当前业务知识库的第一页文件。

~~~json
{
  "limit": 20
}
~~~

企业版如果只展示失败文件，可以传 `"statuses":["failed"]`；只有 Ingest 时确实写入了文件级 `attributes`，才需要在这里生成 `filters`。

字段：

| 字段 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- |
| filters | 否 | null | 按 file_id 或 attributes 过滤 |
| statuses | 否 | 全部 | processing、completed、failed、cancelled |
| cursor | 否 | null | 上一页返回的 next_cursor |
| limit | 否 | 20 | 1～100 |

响应结构两端相同；以下仍以 Stella 的 `scope=user` 文件为例：

~~~json
{
  "items": [
    {
      "file_id": "file_0123456789",
      "filename": "example.pdf",
      "size_bytes": 123456,
      "status": "completed",
      "latest_operation_id": "batch_0123456789",
      "attributes": {
        "scope": "user"
      },
      "last_error": null,
      "created_at": "2026-08-25T10:00:00Z"
    }
  ],
  "next_cursor": null,
  "has_more": false
}
~~~

### 6.2 查询文件详情

**GET /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}**

权限：Runtime 或 Admin Token。

响应与文件列表项相同，并额外包含 knowledge_base_id。

### 6.3 删除文件

**DELETE /knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}**

权限：Runtime 或 Admin Token。

成功返回 204 No Content；重复删除视为成功。删除范围仅限指定技术 KnowledgeBase。

文件仍在 processing 时返回 409 file_busy，并在 details.active_operation_id 中给出活动任务。V1 不提供替换文件、修改 attributes 或下载原文件接口。

## 7. Search API

### 7.1 请求

**POST /knowledge/v1/search**

权限：Runtime 或 Admin Token。

Stella 示例：只传一个隐藏技术 KnowledgeBase，由 Stella 后端根据当前用户和 Agent 生成四级累加 Filter。

~~~json
{
  "query": "如何执行发布流程？",
  "knowledge_base_ids": ["vs_stella_hidden"],
  "filters": {
    "type": "or",
    "filters": [
      {"type": "eq", "key": "scope", "value": "system"},
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "system_agent"},
          {"type": "eq", "key": "agent_id", "value": "agent-1"}
        ]
      },
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "user"},
          {"type": "eq", "key": "user_id", "value": "user-1"}
        ]
      },
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "user_agent"},
          {"type": "eq", "key": "user_id", "value": "user-1"},
          {"type": "eq", "key": "agent_id", "value": "agent-1"}
        ]
      }
    ]
  },
  "mode": "hybrid",
  "limit": 10
}
~~~

企业版示例：企业版先计算当前用户有权且已挂载的业务知识库，再把它们映射成技术 ID。当前的知识库级挂载模型不需要再传权限 Filter。

~~~json
{
  "query": "如何申请产品 A 的发布权限？",
  "knowledge_base_ids": ["vs_company", "vs_product_a"],
  "mode": "hybrid",
  "limit": 10
}
~~~

只有当企业版未来在同一业务 KnowledgeBase 内增加文件级可见范围或状态约束时，才需要在 Ingest `attributes` 与 Search `filters` 之间建立对应规则。

| 字段 | 必填 | 默认值 | 约束与含义 |
| --- | --- | --- | --- |
| query | 是 | 无 | 去除首尾空白后 1～4096 字符 |
| knowledge_base_ids | 是 | 无 | 1～100 个技术 ID；服务稳定去重 |
| filters | 否 | null | 产品完成权限计算后生成的 Filter AST |
| mode | 否 | hybrid | hybrid、dense 或 bm25 |
| limit | 否 | 10 | 最终返回 1～50 条 |

一次 Search 的所有 knowledge_base_ids 必须属于同一租户。跨租户请求返回 422 cross_tenant_search，服务不会查询任何一个 Collection。

### 7.2 检索模式

| mode | 链路 |
| --- | --- |
| dense | 每个 KnowledgeBase 使用自己的 Query Embedding → 对应 Qdrant Named Vector HNSW；多库时外层 RRF |
| bm25 | 每个 KnowledgeBase 执行 Qdrant BM25 Sparse Query；多库时外层 RRF |
| hybrid | 每个 KnowledgeBase：Dense + BM25 → Qdrant 内层 RRF → 该库可选 Rerank；多库结果再做外层 RRF |

Rerank 是 KnowledgeBase 配置，不是每次 Search 的请求参数。`dense` 和 `bm25` 不执行 Rerank。单库 Search 直接返回该库最终排名；多库 Search 对所有库等权，按请求中的 `knowledge_base_ids` 顺序稳定打破同分。某个库 Rerank 失败时只让该库回退到内层 RRF，不中断整个 Search；Embedding、Qdrant 等必要分支失败则整次请求失败。

### 7.3 Filter AST

比较条件：

~~~json
{"type": "eq", "key": "scope", "value": "system"}
~~~

组合条件：

~~~json
{
  "type": "and",
  "filters": [
    {"type": "eq", "key": "department_id", "value": "department-a"},
    {"type": "in", "key": "status", "value": ["published", "approved"]}
  ]
}
~~~

支持的操作符：

| 类型 | 操作符 | 值 |
| --- | --- | --- |
| 逻辑 | and、or | 非空 filters 数组 |
| 相等 | eq、ne | 字符串、整数或布尔值 |
| 集合 | in、nin | 同类型字符串数组或整数数组 |
| 范围 | gt、gte、lt、lte | 声明过索引类型的 integer、float 或 datetime 字段 |

限制：

- 最大 8 层。
- 最多 64 个叶子条件。
- 普通 key 自动映射到 attributes.<key>。
- file_id 和 chunk_id 可直接过滤。
- 调用方不能访问 vector_store_id 或直接指定 attributes. 前缀。
- ne/nin 会同时要求字段存在，避免缺失权限字段被否定条件误放行。
- 需要高性能或范围过滤的业务字段，由部署方在 payload_indexes 中按 Stella/企业版实际字段声明类型。

服务分别把每个 knowledge_base_id 翻译成精确 `vector_store_id = ...` 条件，再与 filters 做 AND。调用方无法通过 Filter 越过本次允许的 KnowledgeBase 集合。

### 7.4 响应

~~~json
{
  "hits": [
    {
      "knowledge_base_id": "vs_product_a",
      "file_id": "file_0123456789",
      "filename": "release-guide.pdf",
      "chunk_id": "chunk_0123456789",
      "content": "产品 A 发布前需要……",
      "locator": {
        "source": "release-guide.pdf",
        "headings": ["发布流程", "权限申请"]
      },
      "score": 0.82,
      "attributes": {
        "department_id": "department-a",
        "status": "published"
      }
    }
  ]
}
~~~

score 只用于本次响应内排序。产品不应跨不同请求、不同 mode 或不同 Rerank 配置比较其绝对值。

## 8. 端点速查

| 方法 | 路径 | Token | 主要用途 |
| --- | --- | --- | --- |
| POST | /knowledge-bases | Runtime | 创建技术 KnowledgeBase |
| GET | /knowledge-bases/{id} | Runtime | 查询技术状态和文件计数 |
| GET | /knowledge-bases/{id}/inference-config | Admin | 查询不含 Key 的完整模型配置 |
| PUT | /knowledge-bases/{id}/embedding-config | Admin | 修改空库模型或轮换 URL/Key |
| PUT | /knowledge-bases/{id}/rerank-config | Admin | 启用、修改或关闭 Rerank |
| DELETE | /knowledge-bases/{id} | Runtime | 删除技术 KnowledgeBase |
| POST | /ingest | Runtime | 异步上传并导入一个文件 |
| GET | /operations/{operation_id} | Runtime | 查询导入状态 |
| POST | /operations/{operation_id}/retry | Runtime | 重试最终失败的导入 |
| POST | /knowledge-bases/{id}/files/query | Runtime | 分页查询文件 |
| GET | /knowledge-bases/{id}/files/{file_id} | Runtime | 查询文件详情 |
| DELETE | /knowledge-bases/{id}/files/{file_id} | Runtime | 删除文件 |
| POST | /search | Runtime | 检索一个或多个同租户 KnowledgeBase |

表中路径均省略 /knowledge/v1 前缀。Admin Token 是 Runtime Token 的超集。

## 9. Stella 如何使用

### 9.1 初始化

Stella 在一套部署中视为一个技术租户：

1. Stella 用稳定 Idempotency-Key 创建一个隐藏技术 KnowledgeBase，并在创建请求中提交部署级 Embedding 与可选 Rerank 配置。
2. 把 tenant_id 和 knowledge_base_id 保存在 Stella 配置或数据库中。
3. 部署 payload_indexes：scope、user_id、agent_id 使用 keyword。

Stella 不需要为四级范围分别创建 KnowledgeBase，也不需要向用户展示 KnowledgeBase 管理 UI。

### 9.2 上传

Stella 根据可信调用上下文写入：

| 范围 | attributes |
| --- | --- |
| System | {"scope":"system"} |
| System-Agent | {"scope":"system_agent","agent_id":"A"} |
| User | {"scope":"user","user_id":"U"} |
| User-Agent | {"scope":"user_agent","user_id":"U","agent_id":"A"} |

上传流程：

~~~text
产品上传动作
  → POST /ingest
  → 保存 file_id 与 operation_id
  → 轮询 GET /operations/{operation_id}
  → completed 后可检索
  → failed 且 retryable 时由产品触发 Retry
~~~

scope、user_id、agent_id 必须由 Stella 后端生成，不能直接信任终端用户提交的 attributes。

### 9.3 四级范围检索

用户 U 使用 Agent A 时，Stella 只传那个隐藏 knowledge_base_id，并构造：

~~~json
{
  "query": "用户问题",
  "knowledge_base_ids": ["vs_stella_hidden"],
  "mode": "hybrid",
  "limit": 10,
  "filters": {
    "type": "or",
    "filters": [
      {"type": "eq", "key": "scope", "value": "system"},
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "system_agent"},
          {"type": "eq", "key": "agent_id", "value": "A"}
        ]
      },
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "user"},
          {"type": "eq", "key": "user_id", "value": "U"}
        ]
      },
      {
        "type": "and",
        "filters": [
          {"type": "eq", "key": "scope", "value": "user_agent"},
          {"type": "eq", "key": "user_id", "value": "U"},
          {"type": "eq", "key": "agent_id", "value": "A"}
        ]
      }
    ]
  }
}
~~~

统一知识库负责完整执行该 Filter，但不理解“四级权限”的业务含义。Stella 仍是权限规则的唯一权威来源。

### 9.4 Stella 需要调用哪些接口

| 场景 | 接口 |
| --- | --- |
| 部署初始化 | 创建/查询隐藏 KnowledgeBase；必要时查询或轮换模型配置 |
| 文件上传 | Ingest、Operation 查询、按需 Retry |
| 文件管理 | File Query、File Detail、File Delete |
| Agent 检索 | Search |
| Stella 数据彻底清理 | 删除隐藏技术 KnowledgeBase；不是普通用户操作 |

Stella 不需要业务 KnowledgeBase 列表、改名、挂载、用户管理或权限分配接口。

## 10. Cherry Studio 企业版如何使用

### 10.1 多租户初始化

企业版的每个业务租户映射为统一知识库的一个 tenant_id：

1. 企业版或部署系统生成稳定 tenant_id。
2. 同一服务为不同 tenant_id 使用相互隔离的 Qdrant Collection。
3. 每次创建业务 KnowledgeBase 时，企业版提交该库选择的 Embedding 与可选 Rerank 配置。

KnowledgeBase 凭证只保存在统一知识库的加密配置中。企业版数据库可保存模型选择或最后更新时间，但不应要求 Knowledge API 回传明文 Key。

### 10.2 创建显式业务知识库

公司、部门、产品等知识库是企业版显式业务对象，需要创建、展示、修改、挂载和删除。推荐顺序：

~~~text
企业版数据库创建业务 KnowledgeBase
  → 读取当前账号的 NewAPI URL/Key 与页面选择的模型
  → POST /knowledge-bases 创建并配置技术 KnowledgeBase
  → 保存 business_kb_id ↔ knowledge_base_id
  → 业务对象进入可用状态
~~~

名称、描述、图标、公司/部门/产品归属、创建人和展示状态只存企业版数据库。统一知识库只保存 tenant_id 和技术状态。

若创建技术 KnowledgeBase 失败，企业版可以用同一个 Idempotency-Key 重试；不要重复创建业务对象。

### 10.3 上传与状态展示

企业版把业务知识库 ID 映射为技术 knowledge_base_id 后调用 Ingest，并在自己的业务文件记录中保存 file_id、operation_id。

企业版 UI 的“上传中、已完成、失败”来自 Operation；文件列表和删除由 File API 完成。业务文件显示名称、上传人、业务标签等可以保存在企业版数据库，也可以把需要检索过滤的少量字段写入 attributes。

### 10.4 挂载与检索

“用户/Assistant 挂载了哪些知识库”只保存在企业版数据库：

~~~text
用户发起检索
  → 企业版计算当前有权访问且已挂载的 business_kb_id
  → 映射成 knowledge_base_ids
  → 一次 POST /search
  → 每个 KnowledgeBase 使用自己的模型独立检索/可选重排
  → 统一知识库对各库结果做等权外层 RRF
~~~

例如用户同时挂载公司库和产品 A 库：

~~~json
{
  "query": "产品 A 的发布流程",
  "knowledge_base_ids": ["vs_company", "vs_product_a"],
  "mode": "hybrid",
  "limit": 10
}
~~~

企业版不需要调用 Mount API，因为统一知识库不维护用户和挂载关系。没有任何可访问且已挂载的知识库时，企业版应直接跳过 Search，而不是发送空数组。

### 10.5 删除业务知识库

企业版先按自己的权限和依赖规则确认允许删除，再调用技术 KnowledgeBase DELETE。成功后清理 business_kb_id 与 knowledge_base_id 的映射。

若返回 knowledge_base_busy，企业版应展示仍有处理任务并稍后重试，不应在产品数据库中提前永久删除映射。

### 10.6 企业版需要调用哪些接口

| 场景 | 接口 |
| --- | --- |
| 租户开通 | 生成稳定 tenant_id；无需预先创建租户模型配置 |
| 创建业务知识库 | 携带该库模型配置创建技术 KnowledgeBase，并保存 ID 映射 |
| 模型配置维护 | 查询配置；空库可换 Embedding，非空库可轮换 URL/Key；Rerank 可随时修改 |
| 对账和展示技术状态 | 查询技术 KnowledgeBase |
| 上传与任务状态 | Ingest、Operation 查询、Retry |
| 文件管理 | File Query、File Detail、File Delete |
| 多库挂载检索 | Search，传同租户多个 knowledge_base_ids |
| 删除业务知识库 | 删除技术 KnowledgeBase |

企业版自行提供业务 KnowledgeBase 列表、改名、挂载、用户、组织、权限和 UI。

## 11. 两侧与统一知识库的维护边界

| Stella / 企业版维护 | 统一知识库维护 |
| --- | --- |
| 终端用户认证和权限判断 | Runtime/Admin 服务鉴权 |
| 用户、组织、角色、Agent/Assistant | 租户存储路由和 KnowledgeBase 模型配置 |
| 业务知识库对象、名称、归属和展示 | 技术 KnowledgeBase、File 和 Operation |
| 企业版挂载关系；Stella 四级范围规则 | 强制 KnowledgeBase 范围和通用 Filter 执行 |
| 可信 attributes 和 Search filters 的生成 | 保留字段保护、同租户校验和 Payload Index |
| 业务文件记录和 UI 状态 | 原文件、本地/S3、Docling、切块和异步恢复 |
| 决定何时创建、重试和删除 | Named Vector、Dense、BM25、内外层 RRF、可选 Rerank 和 Qdrant |
| 配额、业务审计和错误文案 | 凭证加密、稳定错误码、无效原文件自动清理 |

## 12. 当前未提供的接口

V1 明确不提供：

- 获取可用模型列表。
- 用户、组织、角色或权限管理。
- 挂载/取消挂载 KnowledgeBase。
- 业务 KnowledgeBase 列表、名称或描述修改。
- 原文件下载。
- 替换文件或 Revision 管理。
- 修改已上传文件 attributes。
- 取消 Operation。
- 跨租户 Search。
- 已有向量的 Embedding 模型迁移。

这些能力属于产品职责、当前未确认需求或后续实现细节，不应由调用方假设存在。

## 13. 常见状态码

| HTTP | 常见 error.code | 含义 |
| --- | --- | --- |
| 401 | unauthorized | 缺少或使用了错误服务 Token |
| 403 | admin_token_required | Runtime Token 调用了 Admin 接口 |
| 404 | knowledge_base_not_found、file_not_found、operation_not_found | 技术对象不存在 |
| 409 | embedding_config_locked | 非空 KnowledgeBase 尝试修改 Embedding 模型或维度 |
| 409 | idempotency_conflict | 相同 Idempotency-Key 对应不同请求 |
| 409 | file_busy、knowledge_base_busy | 删除目标仍有处理中的任务 |
| 409 | operation_not_retryable、retry_source_missing | Operation 不可重试或原文件已不存在 |
| 413 | payload_too_large | 文件超过部署上传限制 |
| 422 | invalid_request、invalid_attributes、invalid_filter、invalid_search | 请求字段或 Filter 不合法 |
| 422 | cross_tenant_search | 一次 Search 包含不同租户的 KnowledgeBase |
| 502 | invalid_embedding_response、invalid_rerank_response | 模型服务响应与配置不一致 |
| 503 | inference_unavailable、storage_unavailable | 模型或存储依赖暂时不可用 |
| 500 | internal_error | 未预期的服务端错误；用 request_id 查日志 |

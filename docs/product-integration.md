# Stella 与 Cherry Studio 企业版产品接入契约

本文只说明两侧需要维护的映射和调用方式。用户、组织、角色、权限规则和 UI 均由产品维护；统一知识库只执行产品传入的逻辑 KnowledgeBase 范围和 Filter。

## 1. 共同对象映射

| 产品概念 | 统一知识库概念 | Qdrant 表达 |
| --- | --- | --- |
| 租户 | KnowledgeBase 中的可信 `tenant_id`，只用于存储路由 | 一个独立 Collection；物理名称使用租户 ID 的哈希 |
| 逻辑知识库 | 带独立模型配置的技术 `KnowledgeBase`，内部复用 OGX VectorStore | `vector_store_id` Payload + 该模型/维度的 Named Vector |
| 上传文件 | OGX File + 单文件 FileBatch + FileRecord | 原文件在本地卷/S3，Chunk 在租户 Collection |
| 产品过滤字段 | Ingest `attributes` | `attributes.<field>` Payload |
| 本次可检索范围 | Search `knowledge_base_ids + filters` | 每库分支强制生成精确 `vector_store_id = ... AND filter` |

一个租户只有一个 Qdrant Collection；同一服务可以承载多个相互隔离的租户 Collection。产品只在创建技术 KnowledgeBase 时传入可信 `tenant_id`，后续 Ingest/Search 传 `knowledge_base_id(s)`，服务根据持久化映射路由。

创建每个技术 KnowledgeBase 时，由产品后端提交：

- Embedding `base_url / api_key / model_id / dimension`。
- 可选 Rerank `base_url / api_key / model_id`。

统一知识库不获取模型列表。`dimension` 可省略并由服务探测。空 KnowledgeBase 可以换 Embedding 模型和维度；首次 Ingest 后模型与维度锁定，URL/Key 可以轮换。Rerank 不参与持久化索引，可以按库修改或关闭。

## 2. Stella

Stella 把自己视为一个租户，并在部署初始化时创建一个隐藏 KnowledgeBase，将返回 ID 保存在 Stella 配置或数据库中。它通常不向用户展示 KnowledgeBase 管理 UI。

上传时，Stella 从可信运行时写入四级范围 attributes：

| scope | `user_id` | `agent_id` |
| --- | --- | --- |
| `system` | 不写 | 不写 |
| `system_agent` | 不写 | 当前 Agent |
| `user` | 当前用户 | 不写 |
| `user_agent` | 当前用户 | 当前 Agent |

检索当前用户 `U`、Agent `A` 时，Stella 传固定隐藏 `knowledge_base_id`，并生成：

```json
{
  "type": "or",
  "filters": [
    {"type": "eq", "key": "scope", "value": "system"},
    {"type": "and", "filters": [
      {"type": "eq", "key": "scope", "value": "system_agent"},
      {"type": "eq", "key": "agent_id", "value": "A"}
    ]},
    {"type": "and", "filters": [
      {"type": "eq", "key": "scope", "value": "user"},
      {"type": "eq", "key": "user_id", "value": "U"}
    ]},
    {"type": "and", "filters": [
      {"type": "eq", "key": "scope", "value": "user_agent"},
      {"type": "eq", "key": "user_id", "value": "U"},
      {"type": "eq", "key": "agent_id", "value": "A"}
    ]}
  ]
}
```

Stella 负责确保 `scope / user_id / agent_id` 来自可信身份。统一知识库只验证通用 Filter 结构并在 Qdrant 检索阶段完整执行，不理解“四级权限”的业务含义。

Stella 主要使用：

- 部署时：携带部署级 Embedding/Rerank 配置创建并检查隐藏 KnowledgeBase。
- 运行时：单文件或批量 Ingest、Operation 查询/重试、File 查询/下载/删除、Search。
- 不需要：业务知识库列表、重命名、用户挂载或权限管理接口。

Stella 使用批量 Ingest 时，同一请求中的所有文件必须属于相同四级范围并共用 attributes；不同用户、Agent 或 scope 的文件必须拆批。下载前由 Stella 先按四级范围判断当前用户能否访问对应文件，再代理统一知识库返回的原文件流。

## 3. Cherry Studio 企业版

公司、部门、产品等知识库是企业版显式业务对象。企业版先在自己的数据库中创建业务对象，再调用 `POST /knowledge/v1/knowledge-bases`，把自己的业务 ID 与返回的 `knowledge_base_id` 一对一保存。

企业版负责：

- 业务知识库名称、归属、展示字段与 CRUD UI。
- 用户、组织、角色到业务知识库的授权和挂载关系。
- 每次 Search 前把允许访问的业务知识库映射成 `knowledge_base_ids`。
- 为每个业务知识库选择 Embedding/Rerank 模型，并在创建技术 KnowledgeBase 时提交对应连接和凭证。

例如当前用户可访问公司库与产品 A 库，企业版传入两个技术 ID；服务确认两者属于同一租户后，让每个库使用自己的模型独立执行 Dense/BM25/Hybrid 和可选 Rerank，再做等权外层 RRF。若 ID 属于不同租户，服务返回 `422 cross_tenant_search`，不会读取任意一个 Collection。

“挂载知识库”只存在于企业版产品数据库中，不调用统一知识库的 Mount API。创建、展示和修改业务对象也不等于修改 Qdrant Payload；只有删除业务知识库时，企业版才调用技术 KnowledgeBase DELETE 清理其文件和索引。

企业版可以用批量 Ingest 一次向当前业务知识库提交多个文件；整批共用 attributes，每个文件仍有独立 Operation。文件列表或详情页需要下载时，企业版先验证用户对业务知识库的权限，再用映射后的 `knowledge_base_id` 和返回的 `file_id` 调用下载接口并代理响应。

## 4. 维护边界

| Stella / 企业版维护 | 统一知识库维护 |
| --- | --- |
| 用户认证、权限规则、可信身份和组织关系 | Runtime/Admin 服务认证与稳定错误协议 |
| 业务知识库对象、名称、归属、挂载关系及技术 ID 映射 | 技术 KnowledgeBase、File、FileBatch、Operation 和幂等状态 |
| 上传 attributes 与 Search filters 的业务生成 | 保留字段保护、同租户检查和 Qdrant 范围强制 |
| 每个 KnowledgeBase 选择的模型 URL、Key、模型 ID 和 Embedding 维度 | 凭证加密、配置锁定、动态 Named Vector、精确模型探针和实际模型调用 |
| 产品 UI、配额、业务审计和错误提示 | 本地/S3 原文件、Docling、HybridChunker、Dense、BM25、内外层 RRF、可选 Rerank |
| 决定何时创建、重试、删除 | 异步执行、崩溃恢复、技术删除和无效原文件自动清理 |

统一知识库不提供用户创建、权限分配、业务 KnowledgeBase 列表/改名、挂载、任务取消或模型列表接口。原文件下载只负责技术 KB/File 归属校验和字节返回，不替代产品权限判断。

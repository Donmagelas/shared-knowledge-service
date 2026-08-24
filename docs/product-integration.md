# Stella 与 Cherry Studio 企业版产品接入契约

本文只说明两侧需要维护的映射和调用方式。用户、组织、角色、权限规则和 UI 均由产品维护；统一知识库只执行产品传入的逻辑知识库范围和 Filter。

## 1. 共同对象映射

| 产品概念 | 统一知识库概念 | Qdrant 表达 |
| --- | --- | --- |
| 逻辑知识库 | OGX VectorStore，ID 对外称 `knowledge_base_id` | Point 的 `vector_store_id` Payload；不是 Collection |
| 上传文件 | OGX File + VectorStoreFile | 原文件在 File Store，Chunk 在共享 Collection |
| 产品过滤字段 | Ingest `attributes` | `attributes.<field>` Payload |
| 本次可检索范围 | Search `knowledge_base_ids + filters` | 服务强制生成 `vector_store_id IN [...] AND filter` |

一个客户环境只有一个共享 Qdrant Collection；Collection 固定一套 Embedding 模型和维度。新增业务知识库只是新增 VectorStore ID 和 Payload 值，不会新建 Dense/BM25 索引结构。

## 2. Stella

Stella 在部署初始化时创建一个隐藏 VectorStore，并把返回 ID 保存在产品配置或数据库中。上传时，Stella 从可信运行时写入四级范围 attributes：

| scope | `user_id` | `agent_id` |
| --- | --- | --- |
| `system` | 不写 | 不写 |
| `system_agent` | 不写 | 当前 Agent |
| `user` | 当前用户 | 不写 |
| `user_agent` | 当前用户 | 当前 Agent |

检索当前用户 `U`、Agent `A` 时，Stella 传固定隐藏 `knowledge_base_id`，并生成以下等价 Filter：

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

Stella 负责确保 `scope / user_id / agent_id` 来自可信身份，不接受终端用户直接提交 owner 字段。

## 3. Cherry Studio 企业版

公司、部门、产品等知识库是企业版显式业务对象。企业版创建业务对象时调用 `POST /v1/vector_stores`，把自己的业务 ID 与返回的 `vector_store_id` 一对一保存。

企业版负责：

- 知识库名称、业务归属、展示字段与 CRUD UI。
- 用户、组织、角色到业务知识库的授权和挂载关系。
- 每次 Search 前把允许访问的业务知识库映射成 `knowledge_base_ids`。

例如当前用户可访问公司库与产品 A 库，Search 传入两个 ID；统一服务在同一个 Collection 中执行一条 `vector_store_id IN [company, product-a]` 的 Dense/BM25/Hybrid 查询，不进行两次检索后的二次融合。

## 4. 统一知识库维护范围

| Stella / 企业版维护 | 统一知识库维护 |
| --- | --- |
| 权限规则、可信身份、组织关系 | Files、VectorStore、VectorStoreFile 状态 |
| 业务知识库对象及 ID 映射 | 原文件存储、Docling、HybridChunker |
| 上传 attributes 和 Search filters 的业务生成 | Embedding、Dense、BM25、Qdrant RRF |
| 产品 UI、挂载选择、配额 | Filter 校验与强制知识库范围 |
| 调用失败后的产品提示和显式重试 | 稳定 Ingest/Search 响应、删除和恢复协议 |

知识库服务不提供用户创建或权限分配接口。企业版需要的“新建知识库”由企业版创建业务对象后调用 VectorStore 辅助接口完成；Stella 通常不把该能力暴露给用户。

# 部署问题与解决方法

本文只记录 OGX MVP 实际搭建中遇到、且对后续构建或部署有复用价值的问题。版本基线为 OGX `1.3.0`、Qdrant Server `1.18.2`、qdrant-client `1.18.0` 和 Docling `2.121.0`。

## 1. Docker 构建在加载 Dockerfile 前端时超时

**现象**

构建停在 `resolve image config for docker-image://docker.io/docker/dockerfile:1`，访问 Docker Hub 鉴权地址超时。即使业务基础镜像已经缓存，这个语法前端仍可能单独联网。

**原因**

Dockerfile 顶部的 `# syntax=docker/dockerfile:1` 会让 BuildKit 解析并拉取指定前端。当前 Docker 自带的 BuildKit 已支持本项目使用的 cache mount，不需要额外前端。

**解决**

- 当前 Dockerfile 不声明远程 syntax frontend。
- 保留基础镜像 digest 固定，避免把“移除 syntax 行”误解成放弃版本固定。
- 如果未来构建器过旧而必须使用外部前端，应在部署环境提前缓存或配置可达的镜像代理。

## 2. Docling 在断网环境首次处理 PDF 失败

**现象**

只缓存 HybridChunker tokenizer 后，Markdown 可以处理，但数字 PDF 首次导入仍会尝试获取布局分析或表格模型；断网时初始化失败。

**原因**

HybridChunker tokenizer 只负责 token 计数。Docling 的 PDF Standard Pipeline 还需要 Transformers layout 模型；启用表格结构分析的默认路径还需要 Accurate TableFormer。

**解决**

- 构建镜像时分别固定并下载：HybridChunker tokenizer、Transformers layout、Accurate TableFormer。
- 使用精确 commit，不使用漂移的 `main`。
- 构建阶段设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 初始化 HybridChunker 和 PDF Pipeline；缺模型时让镜像构建直接失败。
- 运行容器保持离线变量，避免生产首次导入临时联网。

## 3. Docling 通用模型下载使镜像膨胀

**现象**

使用 Docling CLI 一次下载 layout 与 tableformer 后，镜像约 `4.55 GB`。

**原因**

通用下载同时包含当前运行路径不使用的 ONNX layout、Transformers layout、Accurate TableFormer 和 Fast TableFormer 等变体。

**解决**

- 使用 Hugging Face `snapshot_download` 精确下载 Transformers layout 仓库。
- TableFormer 只允许 `model_artifacts/tableformer/accurate/*`。
- 当前 amd64 镜像约 `1.08 GB`；模型缓存约 `367 MB`，其中 layout 约 `164 MB`、Accurate TableFormer 约 `203 MB`。
- Docling 或 Pipeline 配置变化后必须重新执行离线初始化测试，不能假定原模型清单仍然完整。

## 4. 本机代理污染 localhost 或内网 Qdrant 请求

**现象**

本地 Qdrant 探针或集成测试可能走开发机通用 HTTP/SOCKS 代理，表现为连接错误、超时或请求被代理改写，而容器本身保持 healthy。

**原因**

HTTP 客户端默认读取 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量；版本兼容检查还可能引入不必要的额外请求。

**解决**

- 本地和部署内网使用的 qdrant-client 显式设置 `trust_env=False`。
- 本项目已固定并主动探测 Qdrant 能力，因此设置 `check_compatibility=False`，避免用初始化时的额外网络行为代替真实特性探针。
- 生产环境仍需通过网络策略限制 Qdrant 端口，不能把关闭代理当成安全隔离。

## 5. Qdrant 原生 BM25 的配置必须在写入和查询时一致

**现象**

Qdrant 1.18.2 已能在服务端根据 `qdrant/bm25` 文档生成 Sparse Vector，但默认语言处理偏向英文；只在查询或写入一侧改 tokenizer 会导致词项空间或处理规则不一致。

**解决**

- 写入和查询统一使用 `tokenizer=multilingual`、`language=none`。
- Collection 的 BM25 Sparse Vector 必须启用 `Modifier.IDF`。
- `knowledge-qdrant-preflight` 同时验证服务端 BM25、Payload Filter 和原生 RRF；升级 Qdrant 前必须重新运行。
- 当前选择只对固定的 Qdrant 1.18.2 生效；未来版本若调整语言选项，先迁移协议并重建索引，不原地混用两种编码。

## 6. OpenAI-compatible Embedding 服务未实现模型列表接口

**现象**

OGX 启动时可能记录 `list_provider_model_ids() failed` 或 `/models` 404，但显式注册的 Embedding 模型仍能正常完成 `/embeddings` 请求。

**原因**

部分 OpenAI-compatible 服务只实现 Embedding/Chat 接口，没有实现完整的模型枚举接口。OGX 的周期模型发现与实际已注册模型调用是两条路径。

**解决**

- 在 `registered_resources.models` 中显式配置 `model_id`、provider 和 `embedding_dimension`。
- 以 Embedding preflight 和真实导入结果判断可用性，不把模型刷新告警单独当成服务不可用。
- 如果实际 `/embeddings` 也失败，则按真实调用失败处理，不能忽略。

## 7. Docling Worker 的内存和租约回收不是瞬时行为

**现象**

数字 PDF 首次处理后 OGX 容器内存明显上升；终止 Docling 子进程后，任务不会立即被另一 Worker 接管。

**原因**

OGX 1.3.0 固定启动两个文件处理 Worker，模型会驻留内存；Worker 崩溃后需要等待默认约 60 秒任务租约到期。

**解决**

- 本机基线：OGX 冷启动稳定后约 `1.03 GiB`，处理单页数字 PDF 后约 `1.54 GiB`。这只是 MVP 量级，不是生产上限。
- 健康检查和故障测试必须给租约回收预留时间，不能在 Worker 退出后立即判定任务丢失。
- 当前已验证 Worker 被终止后任务可以重新领取并完成；大文件和并发容量仍需独立压测。

## 8. Docker 展示的卷大小可能包含稀疏文件表观容量

**现象**

`docker system df` 显示的 Qdrant 卷容量明显大于少量测试数据的实际占用。

**原因**

Qdrant 的 WAL 或存储文件可能是稀疏文件；Docker 的汇总数字不一定等于实际磁盘块占用。

**解决**

- 容量排查时同时查看容器内 `du -sh /qdrant/storage` 和宿主文件系统实际块占用。
- 不使用 `docker compose down -v` 做普通重启；该命令会删除 PostgreSQL、Qdrant 和原文件卷。
- 生产容量与备份策略应基于持续写入后的真实数据，而不是空库或稀疏文件表观值。

## 9. 官方 Debian/PyPI/Hugging Face 源在部分网络中构建极慢

**现象**

同一份 Dockerfile 可以正常解析，但 `apt-get update` 下载不到 10 MB 索引耗时数分钟，后续系统包或模型下载可能继续长时间停顿。代码和 Docker BuildKit 均无报错。

**原因**

部署网络到官方 Debian、PyPI 或 Hugging Face 源的链路带宽或稳定性不足。更换代码、反复重试或放宽版本固定都不能解决该网络问题。

**解决**

- Dockerfile 已把 Debian、PyPI 和 Hugging Face 下载地址做成构建参数；版本、commit 和 `uv.lock` 哈希校验不随镜像地址改变。
- 当前默认使用阿里云 Debian 与 Debian Security 镜像，避免当前部署网络反复等待官方源；其他环境仍可显式覆盖。
- 使用 `scripts/build-production-image.sh` 构建生产镜像。脚本先探测 `HF_ENDPOINT` 上固定 revision 的 tokenizer、layout 和 TableFormer 配置文件；任一不可达时明确提示并尝试 `HF_FALLBACK_ENDPOINT`，不会只根据站点首页判断。
- 在对应网络中通过 `.env` 或构建环境设置已验证可达的 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR`、`PYPI_SIMPLE_URL`、`PYPI_FILES_URL`、`HF_ENDPOINT` 和 `HF_FALLBACK_ENDPOINT`。
- 切换镜像只改变传输来源，不得去掉基础镜像 digest、Python 锁文件或 Docling 模型 revision。
- 如果官方源只是慢而未失败，应先确认实际下载进程；不要把网络等待误判为新接口启动故障。

## 10. 修改业务代码导致依赖与 Docling 模型层重复构建

**现象**

只修改 Provider 或 FastAPI 代码，Docker 仍重新执行 `uv sync`、模型预取和离线 Pipeline 初始化，重建时间与首次构建接近。

**原因**

源码曾在大体积依赖层之前 `COPY`，因此任何代码变化都会使 Docker 缓存键失效。

**解决**

- 第一层只复制 `pyproject.toml` 和 `uv.lock`，以 `uv sync --no-install-project` 固定第三方依赖并预置模型。
- 随后再复制 README 和 `src`，单独执行一次只安装本项目的 `uv sync --no-editable`；文档与业务代码变化都不会让模型层失效。
- 配置文件保持最后复制；普通 Provider/API 改动不再触发系统包、第三方依赖或 Docling 模型下载。
- 依赖版本、模型 revision 或构建参数变化仍应主动使大层失效，这是正确行为。

## 11. 开发机代理或客户端特征导致 Embedding 请求在模型路由前被拒绝

**现象**

- WSL 继承宿主机 `ALL_PROXY=socks5://...` 时，Embedding 探针可能在发出请求前报缺少 `socksio`。
- 某些网关会对 Python 标准库 `urllib` 的默认请求特征返回 `403`；相同 Token、Endpoint 和模型通过常规 HTTP 客户端可以成功。
- 多个完全不同的模型 ID 均返回相同 `403` 时，不能立即判断为“所有模型都不可用”。

**解决**

- `knowledge-embedding-preflight` 显式使用 `trust_env=False`，不继承开发机通用代理。
- 使用与生产 OGX OpenAI-compatible Provider 接近的 HTTP 客户端验证 `/embeddings`，不要用单个临时客户端的结果替代真实链路。
- 依次区分网络/网关、鉴权、模型路由和响应协议；只有请求已进入模型路由后，模型不存在的 `404` 才能用于淘汰模型 ID。
- 当前实测 `qwen/qwen3-embedding-8b` 可返回 4096 维向量；该结论仍需以部署环境的实时探针为准。

## 12. 在同一 OGX 逻辑数据库中直接切换 Embedding 模型可能启动失败

**现象**

一个模型已运行并完成 OGX 模型目录刷新后，只修改 `EMBEDDING_MODEL` 和维度再重启，可能出现 `already exists with conflicting field values`。本次实测中，自动发现记录把另一个 Qwen Embedding 模型登记为 `llm`，与随后显式注册的 `embedding` 类型冲突。

**影响与解决**

- 维度在部署初始化后保持不变；新模型必须返回配置的相同维度，否则前置探针直接拒绝。
- 已有数据不能通过只改环境变量安全切换模型；当前范围不提供模型切换或迁移工具。若未来提出该需求，再单独设计数据重建与回滚流程。
- 本次多模型评测为每个候选创建独立 PostgreSQL 评测数据库和 Qdrant Collection，没有清理或篡改原有注册表。
- 正式设计迁移工具前，不自动删除 OGX Registry 记录；删除错误记录可能影响仍在使用的模型和 VectorStore。

## 13. OGX vLLM Rerank 路径与版本化网关不一致

**现象**

模型网关的 `POST /v1/rerank` 能返回标准 `results[index, relevance_score]`，但直接配置 OGX `remote::vllm` 后，请求落到根路径 `POST /rerank`；某些网关在该路径返回 HTML 首页和 `200`，随后 OGX 在解析 JSON 时失败。

**原因与解决**

- OGX `1.3.0` 的 vLLM Provider 会主动去掉 Base URL 末尾的 `/v1`，这是针对原生 vLLM 路径的行为，不适用于当前版本化网关。
- 本项目的 `remote::shared-rerank` Provider 复用 OGX vLLM 请求、鉴权和响应转换，只保留 Base URL 中的 `/v1`，最终调用 `/v1/rerank`。
- 不使用把 Base URL 伪造成 `/v1/v1` 的配置技巧；该做法会破坏健康检查和其他模型接口语义。
- OGX `1.3.0` 的 `--dry-run` 校验没有把当前配置传给外置 Provider Registry，可能误报 `remote::shared-rerank` 不可用。应以配置感知的 Registry 测试和真实 Compose 启动为准，不能据此判断 Provider 注册失败。
## 14. 租户 Collection 路由

- `QDRANT_COLLECTION_NAME` 是单租户默认 Collection 名，也是多租户 Collection 的前缀；它不是企业版业务知识库 ID。
- 企业版多租户创建 VectorStore 时必须从可信产品上下文写入 `metadata.tenant_id`。后续上传和检索不重复传该字段。
- `tenant_id` 创建后不可修改。需要改变租户归属时应新建目标租户 VectorStore 并重新导入，当前 MVP 不提供在线迁移工具。
- 升级前已经存在且没有 `tenant_id` 的 VectorStore 会继续使用默认 Collection，不会自动迁移。部署前应确认这符合单租户语义。
- 删除最后一个 VectorStore 只会删除其 Point，不会自动删除空的租户 Collection；租户销毁属于显式运维动作，避免误删同租户其他逻辑知识库。

## 15. 代码迭代不应被模型站点网络阻塞

**现象**

只修改 Provider 代码后重建完整镜像，Debian 系统包可以通过镜像源完成，但构建环境无法连接 Hugging Face，固定 revision 的 Docling tokenizer/layout/TableFormer 无法重新下载。

**解决**

- 生产镜像仍必须在能够访问已批准模型源或内部制品库的环境中完整重建，并执行离线 Pipeline 初始化，不能跳过模型资产验收。
- `scripts/build-production-image.sh` 可以在官方 Hugging Face 端点不可达时选择兼容镜像，并在日志中打印本次实际使用的 `HF_ENDPOINT`；正式发布流水线可以把两个端点都指向经过批准的内部制品库。
- 测试专用 `compose.e2e.yaml` 会复用上一版已包含固定模型资产的镜像，并把当前 `src/shared_knowledge_service` 与 `config` 只读挂载进去，只用于验证代码变更。
- 测试挂载不能作为生产镜像构建成功的证据；交付前需要在模型源可达时补跑完整 `docker compose build`。

## 16. OGX FileBatch 在正常服务重启时会被标记为 cancelled

**现象**

单文件 FileBatch 提交后立即执行 `docker restart knowledge-ogx`，重启后的 Batch 状态是 `cancelled`，启动恢复逻辑不会继续处理。

**原因**

OGX `1.3.0` 在优雅关闭时取消进程内 Batch Task；后台协程捕获 `CancelledError` 后会把 Batch 状态持久化为 `cancelled`。OGX 启动时只恢复 `in_progress` Batch，因此“服务停机”和“用户主动取消”被表达成了同一种状态。

**解决**

- 自定义 Qdrant Provider 在 shutdown 前记录仍为 `in_progress` 的 Batch。
- OGX 完成后台 Task 清理后，只把这批由服务停机造成的 `cancelled` 状态恢复为 `in_progress`。
- 用户通过 Cancel API 主动取消且在 shutdown 前已是 `cancelled` 的 Batch 不会被恢复。
- 当前已通过真实 Compose 测试验证：提交包含 1000 个段落的 HTML 后立即重启 OGX，Batch 会在启动后恢复、完成并可检索。
- 该修正依赖当前已接受的单实例前提；未来多副本运行时必须改用带租约的全局任务领取机制，不能继续依赖进程内 `asyncio.Task`。

测试环境的 `embedding-stub` 只定义在 `compose.e2e.yaml`。故障注入和服务控制命令必须同时传入 `compose.yaml` 与 `compose.e2e.yaml`，否则 Docker 会报告 `no such service: embedding-stub`。

## 17. 超大文档产生过多 Chunk 时单次 Embedding 请求会被拒绝

**现象**

使用包含 5000 个独立标题和段落的 HTML 做异步重启测试时，Docling 可以完成解析，但 OGX 会把全部 Chunk 一次性放入 Embedding 请求，随后被请求模型的列表长度校验拒绝并把文件标记为 `failed`。

**影响与处理**

- 这是 OGX 当前 Embedding 批处理方式的容量边界，不是 FileBatch 重启恢复失败。
- 异步状态接口会返回明确的 `failed` 和文件级错误，不会把该文件误报为完成。
- 当前重启恢复测试使用 1000 个段落，既能稳定覆盖停机窗口，也不会越过当前测试模型的单请求上限。
- 正式支持超大文档前，需要把 Chunk Embedding 按配置上限分批，并验证任一批失败时的 Qdrant 清理或幂等重试；不能只放宽 Pydantic 校验。

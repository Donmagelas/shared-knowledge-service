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
- 在对应网络中通过 `.env` 或构建环境设置已验证可达的 `DEBIAN_MIRROR`、`DEBIAN_SECURITY_MIRROR`、`PYPI_SIMPLE_URL`、`PYPI_FILES_URL` 和 `HF_ENDPOINT`。
- 切换镜像只改变传输来源，不得去掉基础镜像 digest、Python 锁文件或 Docling 模型 revision。
- 如果官方源只是慢而未失败，应先确认实际下载进程；不要把网络等待误判为新接口启动故障。

## 10. 修改业务代码导致依赖与 Docling 模型层重复构建

**现象**

只修改 Provider 或 FastAPI 代码，Docker 仍重新执行 `uv sync`、模型预取和离线 Pipeline 初始化，重建时间与首次构建接近。

**原因**

源码曾在大体积依赖层之前 `COPY`，因此任何代码变化都会使 Docker 缓存键失效。

**解决**

- 第一层只复制 `pyproject.toml`、`uv.lock` 和 README，以 `uv sync --no-install-project` 固定第三方依赖并预置模型。
- 随后再复制 `src`，单独执行一次只安装本项目的 `uv sync --no-editable`。
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

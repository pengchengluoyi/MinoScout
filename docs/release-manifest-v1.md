# Release manifest schema 1（A 基线）

三仓分别发布到自身 GitHub Releases，不建立共享产品仓库。所有运行包需保留固定 build_id、源码仓库真实 source_commit、平台及包摘要。dirty 本地包只用于隔离验证，不进入公共可安装版本列表。

| 字段 | 合同 |
| --- | --- |
| manifest_schema_version | 整数 1 |
| component | nexus / scout / workbench |
| version | 组件版本；固定发布 tag 与源码 VERSION/package.json 必须一致 |
| build_id | 版本与源码提交组成的不可变身份；本地未提交包必须带 local 标识 |
| source_commit | 源码仓库的完整 40 位提交 SHA，不使用打包仓库提交代替 |
| compatibility.runtime | 是否自带运行时、Python 主次版本；不要求用户电脑另装 Python |
| compatibility.protocol | 当前节点协议 2、MCP 2025-03-26；字段存在不代表所有工作流兼容 |
| compatibility.api | 明确 tested_build_pairs；为空时禁止以版本相近推断兼容 |
| compatibility.schema | Nexus 初始化含 migration 和 bootstrap；旧版数据兼容未经证明时必须拒绝自动回退 |
| compatibility.automatic_rollback | 本期基线 false；D 阶段完成实际兼容演练后才可新增明确组合 |
| items[].os / arch | 实际构建主机平台；本轮实际验证 darwin/arm64，其他平台不推断通过 |
| items[].filename / url / sha256 / bytes | 固定 tag 的 HTTPS 文件 URL、真实文件名、摘要和大小；公开 manifest 生成前核验实际文件 |
| items[].payload | binary；源码包不能作为通过自检的安装包 |
| items[].layers | Scout 保留旧 version/items/layers 格式及 runtime/app/browser 的 key/依赖合同 |

公共 write_manifest.py 拒绝混合提交、脏工作树、未自检源码包、重复平台（Nexus/Workbench）、文件越界或大小/摘要不符。CI 在原生构建成功后才上传 item 元数据并生成 manifest。Scout 的原有提前创建 Release 行为只创建发布容器，构建失败不会上传可安装 item/manifest。

本地交付另给 `hi3d-local-manifest.json`，distribution=local-verification、publishable=false、无远端 URL，只引用当前目录实际文件；它不是公开更新源。GitHub 发布必须使用干净的已提交源码，并重新由 CI 构建；不得把本地 dirty 改成 false 冒充发布验收。

应用运行身份：Nexus `/sys/server_info` 返回固定进程 build 与持久 instance_id；Scout 注册/心跳能力声明及 runtime sidecar 携带进程加载时的 build，status 的 running_build 不从磁盘新包推测；Workbench `/health` 和运行配置返回打包 build。

# macOS 本地部署目录

新正式安装使用 `~/Library/Application Support/Mino/`，下设独立的 `nexus/`、`scout/`、`workbench/`。Nexus 的业务数据放 `nexus/data/`；Workbench 自己管理 releases/config/logs/updates；Scout 自己管理配置、插件、进程锁和 logs。禁止组件共用配置文件、数据库或进程状态。开发源码实例继续使用 `~/.mino-dev/`，不读取正式目录或正式变量。

Nexus 和 Workbench 都是本地后台 Web 服务，Scout 是执行进程；不会生成 `.app`。Workbench 在浏览器访问。GitHub Actions 构建发布包，本地安装器负责部署，两者不是相互替代的路线。

旧安装兼容：已有 `~/Mino/nexus`、`~/Mino/workbench`、`Application Support/MinoScout` 或显式指定目录继续原地运行升级。不会在旁边自动创建第二套服务，也不自动搬动运行中的数据库。新旧目录同时存在时必须明确选择，不猜测、不合并。新默认目录不表示本机已完成迁移。

既有安装迁移必须停写、备份、复制到不存在的新目录、同步服务定义/命令入口/配置内的绝对路径，再启动并验收；原目录保留供回滚。严禁直接移动目录或对运行中的 SQLite 主文件做普通复制。

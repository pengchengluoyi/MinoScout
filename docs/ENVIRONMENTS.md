# 开发与正式部署

GitHub Releases 安装包仅用于正式部署；源码 / PyCharm 启动自动进入开发环境。安装脚本只选择 Nexus、Workbench 或两者，不选择环境。

| 组件 | 源码开发 | 正式安装包 |
| --- | --- | --- |
| Nexus | localhost:50101，关闭 mDNS | 0.0.0.0:50104，局域网可访问 |
| Workbench | localhost:60101 | 0.0.0.0:60104，局域网可访问 |
| Scout | 只连接本机 Nexus :50101 | 连接配置的正式 Nexus，默认 mino.local:50104 |

Scout 无独立业务监听端口。开发与正式使用不同的配置、凭证、节点身份和运行状态。开发 Scout 无参数或 `run` 前台运行，不启动或更新正式后台服务；开发凭证须由开发 Nexus 签发。不要让两套 Scout 同时控制同一设备。

开发 Nexus 默认数据目录 `~/.mino-dev/nexus`；开发 Scout 默认目录 `~/.mino-dev/scout`。开发 Nexus 可使用显式独立数据目录；不能指向标准正式目录。不要把正式数据目录或凭证复制进开发环境。Workbench 的浏览器存储由页面端口和 Nexus 地址隔离。

启动源码：Nexus `python -m mino_nexus.cli`，Workbench `npm run dev`，Scout `python -m mino_scout.cli run`。Workbench 开发地址覆盖也必须指向 localhost:50101，连接失败不会回退到正式。

正式同机部署时，Workbench 配置的 loopback Nexus 地址在浏览器里使用页面主机名，端口仍是50104；显式配置的远程 Nexus 地址保持不变。通过 `http://部署机器IP:60104` 访问，Nexus WebSocket 只接受对应本机接口IP的60104来源；跨机器 Workbench 或反向代理须显式设置 `MINO_WORKBENCH_ORIGINS`，认证仍然必需。

旧标准安装绑定 localhost 的实例，升级到本版并重启服务后使用局域网监听；保留原配置文件和全部业务数据。非标准端口或显式远程地址不自动更改。本次发布不会自动修改开发者旧 checkout 的未提交文件，更新源码前请保留本地改动。

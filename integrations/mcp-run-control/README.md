# MCP Run Control 的 PyCharm 261 兼容适配

上游：Lewik/mcp-run-control，Apache-2.0，原版发布 v0.1.2：
https://github.com/Lewik/mcp-run-control/releases/tag/v0.1.2

本地版本 `0.1.2-local261.1`，实测 PyCharm 2026.1 / 261.22158.340。
插件 ID 保持 `dev.lewik.mcp-run-control`，安装位置是 PyCharm 插件目录。

原版引用 `com.intellij.mcpserver.util.Execution_utilKt.buildSessionId`，当前 SDK 没有该方法；空会话列表不触发错误，但出现第一个 Run 实例后实际调用失败。兼容包用 `CompatibilitySessionIds` 生成稳定会话 ID，保留上游 `list_running_run_configurations` 和 `stop_run_configuration` 工具逻辑。构建脚本仅替换该静态调用的类引用并加入辅助类，同时明确标记本地版本、适用构建族、许可证与 NOTICE。

## 构建

先从上游下载原版 ZIP，再使用当前 IDE 自带 JBR/SDK 编译兼容类，无需账号或 Gradle 网络依赖：

```sh
python3 integrations/mcp-run-control/build_compat.py \
  --upstream /path/to/mcp-run-control-0.1.2.zip \
  --ide-home /Applications/PyCharm.app \
  --output /tmp/mcp-run-control-0.1.2-local261.1.zip
```

输入包保持只读，输出路径已存在会拒绝覆盖；已签名的上游 JAR 会拒绝改写。
Workbench 的本地下载入口为 `public/ide/mcp-run-control-0.1.2-local261.1.zip`。更新该资源时同步保留本目录构建源文件与来源说明。

## 运行契约

原生 MCP 仍从 PyCharm 设置复制实际地址；Scout 可自动复用已保存且属于同一 PyCharm 进程的原生连接。配置通过项目路径、配置名和会话 ID 精确匹配。停止使用 IDE ProcessHandler，等待旧进程退出后才通过原生 MCP 启动同一配置。无需把已有普通 Run 改为 Debug。

安装后必须用真实的活动 Run 实例调用列表和停止/启动，不能仅凭 tools/list 或空列表判断可用。当前实测 nexus dev 旧进程退出、新进程与新会话建立，Scout Development 和 workbench dev 会话不变，服务健康检查通过。副作用无回执时只查询原状态，不重发。

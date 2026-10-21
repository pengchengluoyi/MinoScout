# 历史方案（已停用）

开发联动已切换到社区开源 Debugger MCP Server：https://github.com/hechtcarmel/jetbrains-debugger-mcp-plugin 。下文保留旧扩展的构建说明供历史参考；当前 Workbench 不再下载或调用此扩展。

# Mino IDE Runtime Control

为 JetBrains 2026.1 自带 MCP Server 添加配置级运行查询和自动重启。安装一次后，在 Workbench 点击重启即可由 IDE 停止当前配置、等待旧实例退出、启动同一配置并验证新实例，无需每次切换 IDE。

- `mino_get_run_configuration_state(projectPath, configurationName, operationId?)`：读取精确配置及重启回执。
- `mino_restart_run_configuration(projectPath, configurationName, operationId)`：提交一次重启；同 ID 返回原回执，不重复执行。
- 保留 IDE 的 MCP 授权设置。配置名及项目均来自应用保存的绑定，不依赖窗口焦点，也不执行 shell 停止命令。
- 同一配置互斥；只停止该配置的 IDE ProcessHandler。旧实例未退出时不启动重复实例。启动失败或超时有独立回执。

## 构建

```sh
python3 ide-bridge/build.py --ide-home /Applications/PyCharm.app --output /tmp/mino-ide-runtime.zip
```

编译针对本机 IDE 自带 SDK，不下载构建依赖；兼容范围固定为 261.*。输出不能覆盖已有文件。

## 一次性安装

在 PyCharm 插件设置中选择“从磁盘安装插件”，选择构建的 ZIP。IDE 支持动态插件时直接加载；以 IDE 返回的安装结果为准。安装后在 Workbench 重新检查 IDE，Scout 从真实工具清单自动识别重启能力。没有加载扩展前，不把“刷新状态”当作重启，也不声称重启可用。

重启动作可能遵循当前 IDE 的 MCP 执行授权弹窗；需要无人值守时由用户在 IDE 的 MCP 设置中允许可信客户端执行，扩展不绕过 IDE 的授权策略。

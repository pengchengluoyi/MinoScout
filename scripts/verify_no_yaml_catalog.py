#!/usr/bin/env python3
"""硬约束 3：Scout 不读能力目录。目录唯一真源在 Nexus。

Scout 只按 EXECUTE 载荷里给定的 executor_order / low_level / selected_impl 执行。
在两边各存一份能力声明必然漂移。
"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_yaml_catalog",
    "不解析 plugins/**.yaml、不含 abstract_caps 加载逻辑",
    {
        r"abstract_caps": "引用了 abstract_caps（词表真源在 Nexus）",
        r"plugins/(capabilities|executors|recovery|resources)": "读了能力目录路径",
        r"\byaml\.safe_load\b|\byaml\.load\b|\bruamel\b": "解析 YAML（Scout 不该有配置目录）",
        r"\bplugin_registry\b|\bget_loader\s*\(": "引用了能力目录 registry",
    },
))

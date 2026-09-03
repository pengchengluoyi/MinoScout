#!/usr/bin/env python3
"""硬约束 4：依赖必须单向。Scout 不 import Nexus，也不 import 上游 server 包。"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_nexus_import",
    "不 import mino_nexus，也不 import 上游 MiniOrangeServer 的 server.* / driver.*",
    {
        r"\bfrom\s+mino_nexus\b|\bimport\s+mino_nexus\b": "import 了 Nexus",
        r"\bfrom\s+server\.|\bimport\s+server\.": "import 了上游 server 包（搬迁时要改成本仓路径）",
        r"\bfrom\s+driver\.|\bimport\s+driver\.": "import 了上游 driver 包（engine 层应落在 mino_scout/engines/）",
    },
))

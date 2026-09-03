#!/usr/bin/env python3
"""硬约束 1：Scout 不碰数据库。数据归属方是 Nexus。

Scout 一旦有持久状态，就没法随时被 kill 并重启、没法多节点部署。
需要的数据让 Nexus 塞进 EXECUTE 载荷（device_hint / run_context）。
"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_orm",
    "不 import sqlalchemy、不定义 ORM 模型、不写迁移",
    {
        r"\bimport\s+sqlalchemy\b|\bfrom\s+sqlalchemy\b": "引用了 sqlalchemy",
        r"\bdeclarative_base\b|\bSessionLocal\b|\bsessionmaker\b": "出现 ORM session/base",
        r"\bcreate_engine\s*\(": "创建了数据库引擎",
        r"\bimport\s+sqlite3\b": "直接用 sqlite3",
        r"\balembic\b": "引用了迁移工具",
    },
))

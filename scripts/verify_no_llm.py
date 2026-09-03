#!/usr/bin/env python3
"""硬约束 2：Scout 不调大模型。决策权在 Nexus。

Scout 一旦能"想"，两边就会各想一半，行为无法复现、无法归因。
需要判断的地方一律回报给 Nexus，由它的循环决定下一步。
"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_llm",
    "不 import LLM SDK、不打 LLM endpoint、不含 prompt 常量",
    {
        r"\bimport\s+openai\b|\bfrom\s+openai\b|\banthropic\b": "引用了 LLM SDK",
        r"chat/completions|/v1/messages\b|responses\.create": "直接打 LLM endpoint",
        r"\b(PROMPT|SYSTEM_PROMPT|USER_PROMPT)\b\s*=": "定义了 prompt 常量",
        r"\bdecide_next_action\b|\blocate_element\b|\bassert_visual\b": "调用了 Nexus 侧的决策函数",
        r"\bplanner\b\s*\.|\bllm_client\b": "引用了 planner / llm_client",
    },
))

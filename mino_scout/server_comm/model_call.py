"""用例脚本的文本模型。密钥、地址和模型名都在本节点，不向 Nexus 读取。"""
from __future__ import annotations

import json
from typing import Any
from urllib import error, request


def complete(case, prompt: str, text: str = "", schema: dict | None = None) -> Any:
    from mino_scout.plugins.state import plain_value, secret_value
    from mino_scout.runtime.script_exec import ScriptAbort

    node_id = str(getattr(case.core, "node_id", "") or "")
    base = plain_value("model", "llm", "base_url").rstrip("/")
    model = plain_value("model", "llm", "model")
    key = secret_value(node_id, "model", "llm", "api_key")
    if not base or not model or not key:
        raise ScriptAbort("这台节点还没有配置模型地址、模型名或密钥", reason_code="model_unconfigured")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    user = str(prompt or "")
    extra = str(text or "")
    if extra:
        user = f"{user}\n\n{extra}"
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "返回 JSON。没有单独 schema 时只含 text 字段，回答放进 text。"},
            {"role": "user", "content": user[:8000]},
        ],
        "response_format": {"type": "json_object"},
    }
    if schema:
        body["messages"][0]["content"] = "按调用方给出的 JSON 结构返回，不要添加解释。"
        body["messages"].append({"role": "user", "content": json.dumps(schema, ensure_ascii=False)[:4000]})
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise ScriptAbort(f"模型请求失败：HTTP {exc.code}", reason_code="model_empty") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ScriptAbort("模型没有返回结果", reason_code="model_empty") from exc
    content = _content(raw)
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError as exc:
        raise ScriptAbort("模型没有返回结果", reason_code="model_empty") from exc
    if not isinstance(parsed, dict):
        raise ScriptAbort("模型没有返回结果", reason_code="model_empty")
    if schema:
        return parsed
    return str(parsed.get("text") or "")


def see(case, prompt: str, *, verdict: str = "", index: int | None = None) -> str:
    from mino_scout.plugins.state import plain_value, secret_value
    from mino_scout.runtime.script_exec import ScriptAbort

    shot = getattr(case, "_shot", None)
    if shot is None or not getattr(shot, "has_image", lambda: False)():
        raise ScriptAbort("没有可用截图，不能做视觉判断", reason_code="observation_missing")
    node_id = str(getattr(case.core, "node_id", "") or "")
    base = plain_value("model", "llm", "base_url").rstrip("/")
    model = plain_value("model", "llm", "model")
    key = secret_value(node_id, "model", "llm", "api_key")
    if not base or not model or not key:
        raise ScriptAbort("这台节点还没有配置模型地址、模型名或密钥", reason_code="model_unconfigured")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    ask = str(prompt or "")[:4000]
    if index is not None:
        ask = f"第 {index} 帧。{ask}"
    if verdict == "pass":
        ask += '\n只返回 JSON：{"passed": true 或 false, "text": "看到了什么"}'
    else:
        ask += '\n只返回 JSON：{"text": "看到了什么"}'
    mime = str(getattr(shot, "image_mime", "") or "image/png")
    encoded = str(getattr(shot, "image_base64", "") or "")
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }],
        "response_format": {"type": "json_object"},
    }
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise ScriptAbort(f"模型请求失败：HTTP {exc.code}", reason_code="model_empty") from exc
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ScriptAbort("视觉模型没有返回结果", reason_code="model_empty") from exc
    content = _content(raw)
    try:
        parsed = json.loads(content) if content else {}
    except json.JSONDecodeError as exc:
        raise ScriptAbort("视觉模型没有返回结果", reason_code="model_empty") from exc
    if not isinstance(parsed, dict):
        raise ScriptAbort("视觉模型没有返回结果", reason_code="model_empty")
    summary = str(parsed.get("text") or "")
    if verdict == "pass" and parsed.get("passed") is not True:
        raise ScriptAbort(summary or "视觉断言未通过", reason_code="vlm_failed")
    return summary


def _content(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")

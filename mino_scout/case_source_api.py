from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value, *, field=""):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,18}", str(value)):
        raise ValueError(f"source_api_{field}_id_invalid" if field else "source_api_id_invalid")
    return str(value)


def origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError
        parsed.port
    except (ValueError, TypeError):
        raise ValueError("source_api_url_invalid") from None
    return value.rstrip("/")


def _items(payload, key):
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        candidates = [payload[field] for field in (key, "items", "data") if field in payload]
        if len(candidates) != 1 or not isinstance(candidates[0], list):
            raise ValueError("source_api_payload_invalid")
        items = candidates[0]
        if any(payload.get(field) not in (None, False, "", 0) for field in ("has_more", "next", "next_page", "next_cursor")):
            raise ValueError("source_api_incomplete")
        for field in ("total", "total_count", "count"):
            if field in payload and (type(payload[field]) is not int or payload[field] != len(items)):
                raise ValueError("source_api_incomplete")
        if payload.get("pagination") or any(payload.get(field) not in (None, False, "", 0) for field in ("has_next", "next_page_url")):
            raise ValueError("source_api_incomplete")
        if any(type(payload[field]) is not int or payload[field] > 1 for field in ("pages", "total_pages", "page_count") if field in payload):
            raise ValueError("source_api_incomplete")
    else:
        raise ValueError("source_api_payload_invalid")
    if len(items) > MAX_ROWS or any(not isinstance(item, dict) for item in items):
        raise ValueError("source_api_payload_invalid")
    return items


def read_api(node_id, params):
    from mino_scout.plugins.state import plain_value, secret_value

    base = origin(params.get("service_url"))
    operation = params.get("operation", "read")
    email = plain_value("cli", "case_api", "email")
    password = secret_value(node_id, "cli", "case_api", "password")
    if not email or not password:
        raise ValueError("source_api_credentials_missing")
    if operation not in {"read", "projects", "sessions"}:
        raise ValueError("source_api_operation_invalid")
    project = identifier(params.get("remote_project_id")) if operation != "projects" else ""
    scope = params.get("scope", "project")
    if scope not in {"project", "session"}:
        raise ValueError("source_api_scope_invalid")
    session = identifier(params.get("session_id")) if operation == "read" and scope == "session" else ""
    deadline = time.monotonic() + 90
    opener = build_opener(_NoRedirect())

    def fetch(path, *, token="", login=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("source_api_timeout")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
            if project:
                headers["X-Project-Id"] = project
        body = None
        if login:
            headers["Content-Type"] = "application/json"
            body = json.dumps({"email": email, "password": password}).encode("utf-8")
        request = Request(base + path, data=body, headers=headers, method="POST" if login else "GET")
        try:
            with opener.open(request, timeout=min(20, remaining)) as response:
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ValueError("source_api_timeout")
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("source_api_response_too_large")
                content = b"".join(chunks)
        except HTTPError as error:
            error.close()
            raise ValueError("source_api_auth_denied" if error.code in {401, 403} else
                "source_api_redirect_rejected" if 300 <= error.code < 400 else "source_api_http_failed") from None
        except (OSError, URLError):
            raise ValueError("source_api_unavailable") from None
        if time.monotonic() > deadline:
            raise ValueError("source_api_timeout")
        if len(content) > MAX_BYTES:
            raise ValueError("source_api_response_too_large")
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            raise ValueError("source_api_json_invalid") from None

    login = fetch("/api/auth/login", login=True)
    token = login.get("token") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("source_api_login_invalid")
    if operation in {"projects", "sessions"}:
        items = _items(fetch("/api/" + operation, token=token), operation)
        options, seen = [], set()
        for item in items:
            item_id = identifier(item.get("id"))
            if item_id in seen:
                raise ValueError("source_api_duplicate_id")
            seen.add(item_id)
            label = item.get("title") or item.get("name") or item.get("session_title") or item_id
            if not isinstance(label, str):
                raise ValueError("source_api_payload_invalid")
            options.append({"id": item_id, "label": label[:500]})
        return {"complete": True, "options": options, "service_url": base}
    path = f"/api/sessions/{session}/cases" if session else "/api/cases"
    snapshots = [_items(fetch(path, token=token), "cases") for _attempt in range(2)]
    inferred_sessions = {"missing": 0, "null": 0}
    for snapshot_index, records in enumerate(snapshots):
        seen = set()
        for record in records:
            case_id = identifier(record.get("id"), field="case")
            if case_id in seen:
                raise ValueError("source_api_duplicate_id")
            seen.add(case_id)
            if record.get("project_id") is not None and identifier(record["project_id"], field="record_project") != project:
                raise ValueError("source_api_project_mismatch")
            if session:
                if record.get("session_id") is None:
                    # The scoped endpoint establishes membership when metadata is omitted.
                    if snapshot_index == 1:
                        inferred_sessions["null" if "session_id" in record else "missing"] += 1
                    record["session_id"] = session
                elif identifier(record["session_id"], field="record_session") != session:
                    raise ValueError("source_api_session_mismatch")
    canonical = lambda records: sorted((json.dumps(record, sort_keys=True, ensure_ascii=False) for record in records))
    if canonical(snapshots[0]) != canonical(snapshots[1]):
        raise ValueError("source_api_changed_during_read")
    if any(inferred_sessions.values()):
        from mino_scout.tools.keep_alive.log import SLog
        SLog.i("CaseSourceApi", f"会话范围补齐 session_id: missing={inferred_sessions['missing']} null={inferred_sessions['null']}")
    return {"complete": True, "records": snapshots[1], "consistency": "conservative_double_read"}

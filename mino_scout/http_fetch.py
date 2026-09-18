"""HTTPS 下载（冻结包内 urllib 常缺 CA，统一走 httpx + certifi）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

_USER_AGENT = "MinoScout"


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )


def fetch_json(url: str, *, timeout: float = 120.0) -> dict[str, Any]:
    with _client(timeout) as client:
        resp = client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else {}


def download_file(url: str, dest: Path, *, timeout: float = 600.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _client(timeout) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as out:
                for chunk in resp.iter_bytes(1024 * 1024):
                    out.write(chunk)

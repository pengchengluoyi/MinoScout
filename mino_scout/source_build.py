"""Process-lifetime source identity; source edits never relabel a running process."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


class ProcessBuild:
    def __init__(self, package: Path):
        self.package = package.resolve()
        manifest = self.package / "BUILD.json"
        self.signatures = None
        if manifest.is_file():
            data = json.loads(manifest.read_text())
            self.build = {key: data[key] for key in ("build_id", "source_commit", "dirty")}
            self.build["identity_kind"] = "release"
        else:
            self.signatures = self._signatures()
            digest = hashlib.sha256()
            for name in sorted(self.signatures):
                content = (self.package / name).read_bytes()
                digest.update(name.encode() + b"\0" + content + b"\0")
            if self.signatures != self._signatures():
                raise RuntimeError("Source changed during build identity capture; restart required")
            source_digest = digest.hexdigest()
            self.build = {"build_id": "dev-" + source_digest[:24], "source_digest": source_digest,
                          "source_commit": self._git("rev-parse", "HEAD"),
                          "dirty": bool(self._git("status", "--porcelain", "--untracked-files=normal")),
                          "identity_kind": "source"}

    def _git(self, *args):
        try:
            result = subprocess.run(["git", "-C", str(self.package), *args],
                                    capture_output=True, text=True, timeout=3, check=True)
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _signatures(self):
        signatures = {}
        for path in self.package.rglob("*"):
            relative = path.relative_to(self.package)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if relative.as_posix() != "VERSION" and path.suffix not in {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".js", ".css", ".html"}:
                continue
            if path.is_symlink():
                raise ValueError("Source identity requires regular package files")
            if path.is_file():
                info = path.stat()
                signatures[relative.as_posix()] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return signatures

    def identity(self):
        unchanged = True
        if self.signatures is not None:
            try:
                unchanged = self.signatures == self._signatures()
            except (OSError, ValueError):
                unchanged = False
        return {**self.build, "source_unchanged": unchanged}

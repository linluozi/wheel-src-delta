"""Minimal PyPI JSON API client (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request

from .core import ToolError

USER_AGENT = "wheelsrcdelta/1.0 (+https://github.com/linluozi/wheel-src-delta)"


def release_metadata(project: str, version: str, timeout: int = 30) -> dict:
    url = f"https://pypi.org/pypi/{project}/{version}/json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:  # network/HTTP/JSON
        raise ToolError(f"cannot read PyPI metadata for {project}=={version}: {exc}") from exc


def pick_file(metadata: dict, packagetype: str) -> dict:
    for item in metadata.get("urls", []):
        if item.get("packagetype") == packagetype:
            return item
    raise ToolError(f"release has no {packagetype} file")


def download(item: dict, dest_dir: str, timeout: int = 60) -> str:
    filename = item["filename"]
    dest = os.path.join(dest_dir, filename)
    expected = item.get("digests", {}).get("sha256")
    req = urllib.request.Request(item["url"], headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
    except Exception as exc:
        raise ToolError(f"download failed for {filename}: {exc}") from exc
    actual = h.hexdigest()
    if expected and actual != expected:
        raise ToolError(f"downloaded {filename} sha256 {actual} != PyPI {expected}")
    return dest

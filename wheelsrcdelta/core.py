"""Core comparison engine: byte-exact wheel-vs-source certificates.

Design rules:
  * Only file *bytes* decide equality; no decoding, no newline normalisation.
  * Every compared file carries both sha256 digests, so a third party can
    recompute the verdict from the certificate alone.
  * Verdict is three-valued: SAME_SOURCE / DIVERGENT / INCONCLUSIVE.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = "wheelsrcdelta/1"
PREFIXES = ("", "src/", "lib/")
WHEEL_SKIP_PARTS = (".dist-info/", ".data/", "__pycache__/")


class ToolError(RuntimeError):
    """A precondition failed; the caller should report INCONCLUSIVE/error."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# wheel
# --------------------------------------------------------------------------

def read_wheel_py(path: str) -> Tuple[Dict[str, bytes], int]:
    """Return ({relative .py path: bytes}, number of skipped non-.py entries)."""
    files: Dict[str, bytes] = {}
    skipped = 0
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if name.endswith(".py") and not any(p in name for p in WHEEL_SKIP_PARTS):
                files[name] = zf.read(info)
            else:
                skipped += 1
    return files, skipped


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

class Source:
    kind = "abstract"

    def list_py(self) -> List[str]:
        raise NotImplementedError

    def read(self, relpath: str) -> bytes:
        raise NotImplementedError

    def describe(self) -> dict:
        raise NotImplementedError


class GitSource(Source):
    """A git ref, either a local repository path or a clonable URL.

    Remote clones use ``git init`` + ``fetch --depth=1 <ref>`` so both tags
    and commit ids work.  GitHub's HTTPS endpoint is not required; SSH URLs
    (git@github-<account>:<owner>/<repo>.git) work on restricted hosts.
    """

    kind = "git"

    def __init__(self, repo: str, ref: str, workdir: Optional[str] = None):
        self.repo = repo
        self.ref = ref
        self._tmp: Optional[str] = None
        if os.path.isdir(repo):
            self.path = os.path.abspath(repo)
        else:
            self._tmp = tempfile.mkdtemp(prefix="wsd-git-", dir=workdir)
            self.path = os.path.join(self._tmp, "repo")
            os.makedirs(self.path, exist_ok=True)
            self._git("init", "-q")
            self._git("remote", "add", "origin", repo)
            try:
                self._git("fetch", "-q", "--depth=1", "origin", ref)
            except ToolError as exc:
                raise ToolError(f"cannot fetch {ref!r} from {repo!r}: {exc}") from exc
            self._git("checkout", "-q", "FETCH_HEAD")
        rev = "HEAD" if self._tmp else f"{ref}^{{commit}}"
        self.commit = self._git("rev-parse", rev).decode().strip()

    def _git(self, *args: str) -> bytes:
        cmd = ["git", "-C", self.path, *args]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise ToolError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
            )
        return proc.stdout

    def list_py(self) -> List[str]:
        out = self._git("ls-tree", "-r", "--name-only", self.commit).decode("utf-8")
        return [line for line in out.splitlines() if line.endswith(".py")]

    def read(self, relpath: str) -> bytes:
        return self._git("show", f"{self.commit}:{relpath}")

    def describe(self) -> dict:
        return {"kind": "git", "repo": self.repo, "ref": self.ref, "commit": self.commit}

    def cleanup(self) -> None:
        if self._tmp:
            import shutil

            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


class SdistSource(Source):
    """A source distribution tarball; the top-level ``name-version/`` is stripped."""

    kind = "sdist"

    def __init__(self, path: str):
        self.path = path
        self.sha256 = sha256_file(path)
        self._files: Dict[str, bytes] = {}
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(".py"):
                    continue
                name = member.name.lstrip("./")
                head, sep, tail = name.partition("/")
                rel = tail if sep else head
                if not rel:
                    continue
                fh = tf.extractfile(member)
                if fh is not None:
                    self._files[rel] = fh.read()

    def list_py(self) -> List[str]:
        return sorted(self._files)

    def read(self, relpath: str) -> bytes:
        return self._files[relpath]

    def describe(self) -> dict:
        return {
            "kind": "sdist",
            "filename": os.path.basename(self.path),
            "sha256": self.sha256,
            "bytes": os.path.getsize(self.path),
        }


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _match_source(wheel_path: str, source_paths: set) -> Tuple[Optional[str], List[str]]:
    """Map a wheel path onto the source tree; return (path, candidates_found)."""
    found = [prefix + wheel_path for prefix in PREFIXES if prefix + wheel_path in source_paths]
    if not found:
        return None, []
    return min(found, key=len), found


def compare(wheel_files: Dict[str, bytes], source: Source, allow: Iterable[str] = ()) -> dict:
    allow_patterns = list(allow)
    source_paths = set(source.list_py())
    used = set()
    entries: List[dict] = []
    matched_via = {"exact": 0, "src/": 0, "lib/": 0, "ambiguous": 0}

    for wpath in sorted(wheel_files):
        wbytes = wheel_files[wpath]
        spath, candidates = _match_source(wpath, source_paths)
        if len(candidates) > 1:
            matched_via["ambiguous"] += 1
        if spath is None:
            entries.append(
                {
                    "path": wpath,
                    "status": "missing_in_source",
                    "bytes_wheel": len(wbytes),
                    "bytes_source": None,
                    "sha256_wheel": sha256_bytes(wbytes),
                    "sha256_source": None,
                    "matched_via": None,
                    "allowed": _allowed(wpath, allow_patterns),
                }
            )
            continue
        used.add(spath)
        prefix = spath[: len(spath) - len(wpath)]
        matched_via["exact" if prefix == "" else prefix] += 1
        sbytes = source.read(spath)
        status = "identical" if wbytes == sbytes else "differing"
        entries.append(
            {
                "path": wpath,
                "source_path": spath,
                "status": status,
                "bytes_wheel": len(wbytes),
                "bytes_source": len(sbytes),
                "sha256_wheel": sha256_bytes(wbytes),
                "sha256_source": sha256_bytes(sbytes),
                "matched_via": prefix or "exact",
                "allowed": _allowed(wpath, allow_patterns),
            }
        )

    # Files that exist in the source package tree but never reached the wheel.
    tops = {p.split("/")[0] for p in wheel_files if "/" in p}
    # A file may exist at several legitimate layouts (e.g. ``demo/a.py`` and
    # ``src/demo/a.py``); the copy we did not pick is a shadow, not an omission.
    shadowed = {prefix + wpath for wpath in wheel_files for prefix in PREFIXES}
    source_only: List[dict] = []
    if tops:
        for spath in sorted(source_paths - used):
            if spath in shadowed:
                continue
            if not any(spath.startswith(prefix + top + "/") for top in tops for prefix in PREFIXES):
                continue
            data = source.read(spath)
            source_only.append(
                {
                    "path": spath,
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                    "allowed": _allowed(spath, allow_patterns),
                }
            )

    unexplained = sum(
        1 for e in entries if e["status"] != "identical" and not e["allowed"]
    ) + sum(1 for e in source_only if not e["allowed"])
    allowed_count = sum(
        1 for e in entries if e["status"] != "identical" and e["allowed"]
    ) + sum(1 for e in source_only if e["allowed"])

    if not entries:
        verdict = "INCONCLUSIVE"
        reason = "wheel contains no comparable .py files"
    elif unexplained:
        verdict = "DIVERGENT"
        reason = f"{unexplained} unexplained byte-level divergence(s)"
    else:
        verdict = "SAME_SOURCE"
        reason = "all compared .py files are byte-identical"

    return {
        "files": entries,
        "source_only": source_only,
        "comparison": {
            "wheel_py_files": len(wheel_files),
            "source_py_files": len(source_paths),
            "compared": sum(1 for e in entries if e["status"] != "missing_in_source"),
            "identical": sum(1 for e in entries if e["status"] == "identical"),
            "differing": sum(1 for e in entries if e["status"] == "differing"),
            "missing_in_source": sum(1 for e in entries if e["status"] == "missing_in_source"),
            "source_only": len(source_only),
            "allowed_divergences": allowed_count,
            "unexplained_divergences": unexplained,
            "matched_via": matched_via,
            "allow": allow_patterns,
        },
        "verdict": verdict,
        "verdict_reason": reason,
    }


def _allowed(path: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def manifest_sha256(entries: Iterable[dict], key: str) -> str:
    h = hashlib.sha256()
    for e in entries:
        digest = e.get(key)
        if digest:
            h.update(f"{e['path']}\0{digest}\n".encode("utf-8"))
    return h.hexdigest()


def certificate_digest(cert: dict) -> str:
    """sha256 over the deterministic part of a certificate (no timestamps)."""
    payload = {k: cert[k] for k in ("schema", "subject", "preconditions", "comparison", "files", "source_only", "verdict", "verdict_reason")}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(blob.encode("utf-8"))


def build_certificate(
    *,
    project: Optional[str],
    version: Optional[str],
    wheel_path: str,
    wheel_expected_sha256: Optional[str],
    wheel_source_url: Optional[str],
    source: Source,
    allow: Iterable[str] = (),
    generated_at: str,
) -> dict:
    wheel_files, skipped = read_wheel_py(wheel_path)
    wheel_sha = sha256_file(wheel_path)
    if wheel_expected_sha256 and wheel_sha != wheel_expected_sha256:
        raise ToolError(
            f"wheel digest mismatch: expected {wheel_expected_sha256}, got {wheel_sha}"
        )
    result = compare(wheel_files, source, allow)
    cert = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "subject": {
            "project": project,
            "version": version,
            "wheel": {
                "filename": os.path.basename(wheel_path),
                "sha256": wheel_sha,
                "bytes": os.path.getsize(wheel_path),
                "url": wheel_source_url,
            },
            "source": source.describe(),
        },
        "preconditions": {
            "wheel_digest_declared": wheel_expected_sha256,
            "wheel_digest_matches": (
                None if wheel_expected_sha256 is None else wheel_sha == wheel_expected_sha256
            ),
            "git_worktree": None,
            "non_py_wheel_entries_skipped": skipped,
        },
        "comparison": result["comparison"],
        "files": result["files"],
        "source_only": result["source_only"],
        "verdict": result["verdict"],
        "verdict_reason": result["verdict_reason"],
        "manifest_sha256": {
            "wheel_py": manifest_sha256(result["files"], "sha256_wheel"),
            "source_py": manifest_sha256(result["files"], "sha256_source"),
        },
    }
    cert["certificate_sha256"] = certificate_digest(cert)
    return cert

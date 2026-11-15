"""Command line interface: ``wheelsrcdelta check`` / ``wheelsrcdelta verify``."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile

from . import __version__
from .core import (
    GitSource,
    SdistSource,
    ToolError,
    build_certificate,
    certificate_digest,
)
from .pypi import download, pick_file, release_metadata

EXIT_SAME = 0
EXIT_ERROR = 1
EXIT_DIVERGENT = 2
EXIT_INCONCLUSIVE = 3


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(cert: dict, json_out: str | None, quiet: bool) -> None:
    blob = json.dumps(cert, indent=2, sort_keys=True, ensure_ascii=False)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
    if not quiet:
        c = cert["comparison"]
        print(
            f"{cert['verdict']}: {c['compared']} compared, {c['identical']} identical, "
            f"{c['differing']} differing, {c['missing_in_source']} missing-in-source, "
            f"{c['source_only']} source-only",
            file=sys.stderr,
        )
        if not json_out:
            print(blob)


def _resolve_wheel(args, workdir: str):
    if args.wheel:
        if not os.path.isfile(args.wheel):
            raise ToolError(f"wheel not found: {args.wheel}")
        return args.wheel, None, None, None
    if not (args.project and args.version):
        raise ToolError("either --wheel or --project/--version is required")
    meta = release_metadata(args.project, args.version)
    item = pick_file(meta, "bdist_wheel")
    path = download(item, workdir)
    return path, item.get("digests", {}).get("sha256"), item["url"], meta


def cmd_check(args) -> int:
    workdir = tempfile.mkdtemp(prefix="wsd-")
    git_source = None
    try:
        wheel_path, expected, wheel_url, meta = _resolve_wheel(args, workdir)
        if args.against == "git":
            if not (args.repo and args.ref):
                raise ToolError("--against git requires --repo and --ref")
            source = GitSource(args.repo, args.ref, workdir=workdir)
            git_source = source
        else:
            if args.sdist:
                sdist_path = args.sdist
            elif args.project and args.version:
                sdist_path = download(pick_file(meta, "sdist"), workdir)
            else:
                raise ToolError("--against sdist requires --sdist or --project/--version")
            source = SdistSource(sdist_path)
        cert = build_certificate(
            project=args.project,
            version=args.version,
            wheel_path=wheel_path,
            wheel_expected_sha256=expected,
            wheel_source_url=wheel_url,
            source=source,
            allow=args.allow or [],
            generated_at=_now(),
        )
        _emit(cert, args.json_out, args.quiet)
        if cert["verdict"] == "SAME_SOURCE":
            return EXIT_SAME
        if cert["verdict"] == "DIVERGENT":
            return EXIT_DIVERGENT
        return EXIT_INCONCLUSIVE
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if git_source is not None:
            git_source.cleanup()
        shutil.rmtree(workdir, ignore_errors=True)


def cmd_verify(args) -> int:
    with open(args.certificate, "r", encoding="utf-8") as fh:
        cert = json.load(fh)
    declared = cert.get("certificate_sha256")
    recomputed = certificate_digest(cert)
    ok = declared == recomputed
    result = {
        "certificate": args.certificate,
        "declared_sha256": declared,
        "recomputed_sha256": recomputed,
        "valid": ok,
        "verdict": cert.get("verdict"),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else EXIT_DIVERGENT


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wheelsrcdelta",
        description="Byte-exact wheel-vs-source divergence certificate",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="compare a wheel against a git ref or sdist")
    c.add_argument("--project", help="PyPI project name")
    c.add_argument("--version", dest="version", help="PyPI version")
    c.add_argument("--wheel", help="local wheel file (skips PyPI download)")
    c.add_argument("--against", choices=["git", "sdist"], default="git")
    c.add_argument("--repo", help="git repository path or SSH URL")
    c.add_argument("--ref", help="tag, branch or commit in --repo")
    c.add_argument("--sdist", help="local sdist file (sdist mode)")
    c.add_argument("--allow", action="append", default=[], metavar="GLOB",
                   help="known-generated path to allowlist (repeatable)")
    c.add_argument("--json-out", help="write the certificate to this path")
    c.add_argument("--quiet", action="store_true", help="no summary on stderr")
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("verify", help="recompute a certificate's sha256")
    v.add_argument("certificate")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

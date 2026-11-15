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
        c = cert.get("comparison")
        if c is not None:
            print(
                f"{cert['verdict']}: {c['compared']} compared, {c['identical']} identical, "
                f"{c['differing']} differing, {c['missing_in_source']} missing-in-source, "
                f"{c['source_only']} source-only",
                file=sys.stderr,
            )
        else:
            print(f"{cert['verdict']}: {cert['totals']}", file=sys.stderr)
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


def _perform(args, workdir: str) -> dict:
    """Run one comparison described by an argparse-like namespace."""
    wheel_path, expected, wheel_url, meta = _resolve_wheel(args, workdir)
    source = None
    try:
        if args.against == "git":
            if not (args.repo and args.ref):
                raise ToolError("--against git requires --repo and --ref")
            source = GitSource(args.repo, args.ref, workdir=workdir)
        else:
            if args.sdist:
                sdist_path = args.sdist
            elif args.project and args.version:
                sdist_path = download(pick_file(meta, "sdist"), workdir)
            else:
                raise ToolError("--against sdist requires --sdist or --project/--version")
            source = SdistSource(sdist_path)
        return build_certificate(
            project=args.project,
            version=args.version,
            wheel_path=wheel_path,
            wheel_expected_sha256=expected,
            wheel_source_url=wheel_url,
            source=source,
            allow=args.allow or [],
            generated_at=_now(),
        )
    finally:
        if isinstance(source, GitSource):
            source.cleanup()


def cmd_check(args) -> int:
    workdir = tempfile.mkdtemp(prefix="wsd-")
    try:
        cert = _perform(args, workdir)
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
        shutil.rmtree(workdir, ignore_errors=True)


def cmd_batch(args) -> int:
    """Check a manifest of releases and gate on the worst verdict."""
    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        entries = manifest["entries"]
        if not isinstance(entries, list) or not entries:
            raise ToolError("manifest must contain a non-empty 'entries' list")
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: cannot read manifest: {exc}", file=sys.stderr)
        return EXIT_ERROR

    workdir = tempfile.mkdtemp(prefix="wsd-batch-")
    results = []
    worst = EXIT_SAME
    try:
        for entry in entries:
            ns = argparse.Namespace(
                project=entry.get("project"),
                version=entry.get("version"),
                wheel=entry.get("wheel"),
                against=entry.get("against", "git"),
                repo=entry.get("repo"),
                ref=entry.get("ref"),
                sdist=entry.get("sdist"),
                allow=entry.get("allow") or [],
            )
            try:
                cert = _perform(ns, workdir)
            except ToolError as exc:
                results.append({"project": ns.project, "version": ns.version,
                                "verdict": "INCONCLUSIVE", "error": str(exc)})
                worst = max(worst, EXIT_INCONCLUSIVE)
                continue
            if args.json_dir:
                os.makedirs(args.json_dir, exist_ok=True)
                name = f"{ns.project}-{ns.version}.json".replace("/", "_")
                with open(os.path.join(args.json_dir, name), "w", encoding="utf-8") as fh:
                    json.dump(cert, fh, indent=2, sort_keys=True)
            results.append({
                "project": ns.project,
                "version": ns.version,
                "verdict": cert["verdict"],
                "compared": cert["comparison"]["compared"],
                "unexplained_divergences": cert["comparison"]["unexplained_divergences"],
                "certificate_sha256": cert["certificate_sha256"],
            })
            if cert["verdict"] == "DIVERGENT":
                worst = max(worst, EXIT_DIVERGENT)
            elif cert["verdict"] == "INCONCLUSIVE":
                worst = max(worst, EXIT_INCONCLUSIVE)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    summary = {
        "schema": "wheelsrcdelta-batch/1",
        "entries": results,
        "totals": {
            "releases": len(results),
            "same_source": sum(1 for r in results if r["verdict"] == "SAME_SOURCE"),
            "divergent": sum(1 for r in results if r["verdict"] == "DIVERGENT"),
            "inconclusive": sum(1 for r in results if r.get("verdict") == "INCONCLUSIVE"),
        },
        "verdict": {EXIT_SAME: "ALL_SAME", EXIT_DIVERGENT: "ANY_DIVERGENT",
                    EXIT_INCONCLUSIVE: "ANY_INCONCLUSIVE"}[worst],
    }
    summary["certificate_sha256"] = certificate_digest(summary)
    _emit(summary, args.json_out, args.quiet)
    return worst


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

    b = sub.add_parser("batch", help="check a manifest of releases, gate on the worst verdict")
    b.add_argument("manifest", help="JSON manifest with an 'entries' list")
    b.add_argument("--json-dir", help="write each certificate here")
    b.add_argument("--json-out", help="write the batch summary here")
    b.add_argument("--quiet", action="store_true")
    b.set_defaults(func=cmd_batch)

    v = sub.add_parser("verify", help="recompute a certificate's sha256")
    v.add_argument("certificate")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

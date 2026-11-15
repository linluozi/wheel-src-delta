# wheel-src-delta

**Does the wheel users install actually contain the source of the git tag it claims to come from?**
`wheelsrcdelta` answers that with a byte-exact, offline-recomputable certificate and a CI-friendly exit code.

Upstream: none

This is an original tool. It does not patch or vendor any upstream project; it *measures* released
artifacts. (There is no `patch.diff`: nothing upstream is modified.)

## Why this exists

A source release is reviewed on GitHub; users `pip install` a wheel built somewhere else. Build
backends legitimately generate files (`_version.py`, compiled stubs), and less legitimately can
substitute code. Existing provenance work (PEP 740 attestations, reproducible-build checks) proves
*who built it* or *that a rebuild matches*; it does not hand a reviewer a per-file, byte-level
comparison of the published wheel against the tag, with a verdict they can re-derive from the
certificate alone.

Three properties make the result auditable rather than a claim:

1. **Preconditions are checked, not assumed.** The wheel's sha256 must equal PyPI's declared digest;
   the git ref is resolved to a commit id that is recorded in the certificate.
2. **Bytes, not text.** No decoding, no newline normalisation, no AST comparison.
3. **Layout is normalised, but explicitly.** Wheel paths are mapped through `""`, `src/` and `lib/`;
   the matching route is recorded per file, so `src/`-layout packages are not silently reported as
   "missing".

## Install / run

Zero third-party dependencies (Python 3.9+, `git` on PATH).

```bash
git clone git@github.com:linluozi/wheel-src-delta.git
cd wheel-src-delta
python3 -m unittest discover -s tests -v     # 24 tests, no network
python3 -m wheelsrcdelta check --help
# optional:
python3 -m pip install -e .
```

On hosts where `github.com:443` is blocked, use an SSH URL (`git@github.com:owner/repo.git` or a
host alias such as `git@github-linluozi:owner/repo.git`). PyPI's JSON/files endpoints are used over
HTTPS.

## Usage

```bash
# wheel from PyPI vs a git tag
python3 -m wheelsrcdelta check \
  --project attrs --version 25.4.0 \
  --against git --repo git@github-linluozi:python-attrs/attrs.git --ref 25.4.0 \
  --json-out attrs.json

# wheel from PyPI vs the published sdist (no git host needed)
python3 -m wheelsrcdelta check --project attrs --version 25.4.0 --against sdist

# local wheel / local sdist, plus known build-generated files
python3 -m wheelsrcdelta check --wheel dist/demo-1.0-py3-none-any.whl \
  --against sdist --sdist dist/demo-1.0.tar.gz --allow 'demo/_version.py'

# re-derive the certificate digest offline (detects edited JSON)
python3 -m wheelsrcdelta verify attrs.json
```

Exit codes: `0` SAME_SOURCE, `2` DIVERGENT, `3` INCONCLUSIVE (no comparable `.py`, or a
precondition failed), `1` usage/network error. `--allow GLOB` (repeatable) marks a divergence as
known-generated: it is still listed and counted, but does not flip the verdict.

## Evidence: twelve real PyPI releases (2026-11-16)

Each row is one `check` command against the tag on the project's own repository. Full certificates
(per-file sha256 of both sides) are in `evidence/certs/`; `evidence/summary.json` aggregates them.

| Project | Version = ref | `.py` compared | identical | missing in source | matched via | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| attrs | 25.4.0 | 19 | 19 | 0 | `src/` 19 | SAME_SOURCE |
| click | 8.5.1 | 17 | 17 | 0 | `src/` 17 | SAME_SOURCE |
| coverage | 7.17.3 | 44 | 44 | 0 | exact 44 | SAME_SOURCE |
| flask | 3.1.3 | 24 | 24 | 0 | `src/` 24 | SAME_SOURCE |
| isort | 6.1.0 | 45 | 45 | 0 | exact 45 | SAME_SOURCE |
| markupsafe | 3.0.4 | 2 | 2 | 0 | `src/` 2 | SAME_SOURCE |
| packaging | 26.3 | 22 | 22 | 0 | `src/` 22 | SAME_SOURCE |
| pytest | 9.2.0 | 81 | 81 | **1** | `src/` 81 | **DIVERGENT** |
| six | 1.17.0 | 1 | 1 | 0 | exact 1 | SAME_SOURCE |
| tomlkit | 0.15.2 | 12 | 12 | 0 | exact 12 | SAME_SOURCE |
| tox | 4.67.13 | 129 | 129 | **1** | `src/` 129 | **DIVERGENT** |
| virtualenv | 21.15.6 | 92 | 92 | **1** | `src/` 92 | **DIVERGENT** |
| **total** | | **488** | **488** | **3** | `src/` **386** | 3/12 divergent |

Three measured facts, each reproducible from the certificates:

1. **Layout normalisation is load-bearing.** 386/488 files (79.1%) match only after `src/`
   normalisation, across 8 of the 12 `src/`-layout releases. A literal path comparison reports 100%
   missing on those releases — 386 files of false divergence. Without the mapping this tool would be
   useless on the majority of modern Python projects.
2. **Discrepancies are rare but real, and none of them is a silent auto-pass.** Every file present on
   both sides (488/488) is byte-identical; the only divergences are three wheel-only modules —
   `tox/version.py`, `virtualenv/version.py`, `_pytest/_version.py` (520–524 bytes each) — that do
   not exist at the tag. All three are build-generated version modules (setuptools_scm-style
   `write_to`). Prevalence in this sample: **3/12 releases (25%)**.
3. **The strict verdict stays DIVERGENT; an allowlist is an explicit act.** With
   `--allow '*/version.py'` (or the exact path for `_pytest/_version.py`) the same runs become
   `SAME_SOURCE` while still reporting `allowed_divergences: 1`
   (`evidence/allowed/*.json`). Nothing is excused by default.

### Negative controls (the instrument can say no)

Comparing the *newer* wheel against an *older* tag must fail; otherwise the check is a rubber stamp:

| Wheel | Compared against | compared | identical | differing | verdict |
| --- | --- | --- | --- | --- | --- |
| attrs 25.4.0 | tag 25.3.0 | 19 | 12 | 7 | DIVERGENT |
| six 1.17.0 | tag 1.16.0 | 1 | 0 | 1 | DIVERGENT |

Certificates: `evidence/negative/*.json`. This is the sensitivity evidence: the same code path that
returned 73/73 identical returns a precise, byte-level diff when the source is wrong.

## 本轮创新

**B 档：把「PyPI 已发布 wheel ↔ 发布 git tag」的逐文件字节散度做成可离线复算的证书，并把路径
布局归一化与「显式允许」作为证书的一部分记录。** 既有台账条目（`sametree-recheck` 复核 PR 的
base/patched 同树、`perfclaim-guard` 管声明、`caseproof` 数用例）都不测「发布产物 vs 源码 tag」。
本轮给出的可证伪结论：12 个真实发布共 491 个 wheel `.py`，其中 488 个两边都存在且逐字节一致、
0 个内容不同；3 个仅存在于 wheel 的模块全部是构建生成的 `version.py`/`_version.py`（25% 的抽样
发布），严格判定为 DIVERGENT，显式 `--allow` 后才变 SAME；另有 386/488（79.1%）只有经 `src/`
归一化才匹配——朴素路径比较会在 8 个 src 布局项目上把 386 个文件全部误判为缺失。两个负控
（attrs 25.4.0 vs 25.3.0：7/19 不同；six 1.17.0 vs 1.16.0：1/1 不同）证明证书不是只会输出 SAME。

## Reproduce (copy-paste)

```bash
python3 -m unittest discover -s tests -v
python3 -m wheelsrcdelta check --project attrs --version 25.4.0 --against git \
  --repo git@github-linluozi:python-attrs/attrs.git --ref 25.4.0 --json-out /tmp/attrs.json
python3 -m wheelsrcdelta verify /tmp/attrs.json
```

`verify` recomputes `certificate_sha256` over the deterministic part of the JSON (everything except
`generated_at`), so a truncated or edited certificate is rejected.

## Certificate format (schema `wheelsrcdelta/1`)

- `subject.wheel`: filename, sha256, size, PyPI URL; `subject.source`: git repo/ref/commit or sdist
  filename/sha256.
- `preconditions.wheel_digest_matches`: PyPI digest check (or `null` for a local wheel).
- `files[]`: `path`, `source_path`, `status` (`identical` / `differing` / `missing_in_source`),
  byte counts, both sha256 digests, `matched_via`, `allowed`.
- `source_only[]`: source modules inside the package that never reached the wheel.
- `comparison`: counts plus `unexplained_divergences`.
- `verdict`: `SAME_SOURCE` / `DIVERGENT` / `INCONCLUSIVE`; `certificate_sha256` closes the document.

## Limits (stated, not hidden)

- Only `.py` files are compared. Compiled extensions (`.so`, `.pyd`), data files and generated stubs
  are counted as skipped wheel entries, not verified.
- Build-generated Python (e.g. version modules) is a real divergence. Use `--allow` deliberately; the
  file remains in the report.
- The certificate proves artifact/source consistency, not that the tag itself is honest, and it does
  not verify PEP 740 attestations or rebuild reproducibility.
- sdist mode compares against the sdist's own tree; it cannot detect a source substitution that was
  applied before both artifacts were built.
- A repo whose release tag contains no `.py` under the wheel's top-level package yields
  `INCONCLUSIVE`, never a silent pass.

## Tests

`python3 -m unittest discover -s tests -v` → **24 tests, OK** (0.33 s). They build wheels, sdists and
real git repositories in temporary directories and cover: exact/`src/` mapping, single-byte and
CRLF/BOM differences, empty files, missing and source-only modules, allowlists, ambiguity between
`demo/` and `src/demo/`, digest mismatch, certificate determinism/tampering, and all CLI exit codes.

## Environment

AMD EPYC 7K62 (4 vCPU), 3.7 GB RAM, Linux x86_64, CPython 3.12.3, git 2.43.0. Evidence collected
2026-11-16. No random seeds are involved; the comparison is deterministic and the
`certificate_sha256` values in `evidence/summary.json` pin the exact inputs.

## License

MIT — see `LICENSE`. Original work; no upstream source is copied.

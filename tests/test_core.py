"""Tests for the wheel-vs-source comparison engine (stdlib only)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheelsrcdelta import cli
from wheelsrcdelta.core import (
    GitSource,
    SdistSource,
    ToolError,
    build_certificate,
    certificate_digest,
    compare,
    read_wheel_py,
)


def make_wheel(path: str, files: dict, dist_info: bool = True) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if dist_info:
            zf.writestr("demo-1.0.dist-info/METADATA", "Name: demo\n")
            zf.writestr("demo-1.0.dist-info/RECORD", "demo/__init__.py,,\n")
    return path


def make_sdist(path: str, top: str, files: dict) -> str:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def make_repo(root: str, files: dict, tag: str = "v1") -> str:
    env = dict(os.environ)
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, env=env)
    for name, data in files.items():
        full = os.path.join(root, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "fixture"],
        check=True, env=env,
    )
    subprocess.run(["git", "-C", root, "tag", tag], check=True, env=env)
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def wheel(self, files, name="demo-1.0-py3-none-any.whl", dist_info=True):
        return make_wheel(os.path.join(self.root, name), files, dist_info)

    def sdist(self, files, top="demo-1.0", name="demo-1.0.tar.gz"):
        return make_sdist(os.path.join(self.root, name), top, files)

    def repo(self, files, tag="v1", name="repo"):
        return make_repo(os.path.join(self.root, name), files, tag)

    def cert(self, wheel, source, allow=()):
        return build_certificate(
            project="demo",
            version="1.0",
            wheel_path=wheel,
            wheel_expected_sha256=None,
            wheel_source_url=None,
            source=source,
            allow=allow,
            generated_at="2026-01-01T00:00:00Z",
        )


class TestWheelReading(Base):
    def test_reads_only_top_level_python(self):
        whl = self.wheel({
            "demo/__init__.py": b"x = 1\n",
            "demo/sub/mod.py": b"y = 2\n",
            "demo/data.txt": b"not python",
            "demo/__pycache__/mod.cpython-312.pyc": b"\x00",
            "demo-1.0.data/scripts/tool.py": b"print(1)\n",
        })
        files, skipped = read_wheel_py(whl)
        self.assertEqual(sorted(files), ["demo/__init__.py", "demo/sub/mod.py"])
        self.assertGreaterEqual(skipped, 3)


class TestGitMode(Base):
    def test_identical_src_layout(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = GitSource(self.repo({"src/demo/__init__.py": b"x = 1\n"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["comparison"]["matched_via"]["src/"], 1)
        self.assertEqual(cert["comparison"]["matched_via"]["exact"], 0)
        self.assertEqual(cert["files"][0]["sha256_wheel"], cert["files"][0]["sha256_source"])

    def test_single_byte_difference_is_divergent(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = GitSource(self.repo({"demo/__init__.py": b"x = 2\n"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "DIVERGENT")
        self.assertEqual(cert["comparison"]["differing"], 1)
        self.assertNotEqual(cert["files"][0]["sha256_wheel"], cert["files"][0]["sha256_source"])

    def test_crlf_is_not_normalised(self):
        whl = self.wheel({"demo/a.py": b"x = 1\r\n"})
        src = GitSource(self.repo({"demo/a.py": b"x = 1\n"}), "v1")
        self.assertEqual(self.cert(whl, src)["verdict"], "DIVERGENT")

    def test_empty_file_matches(self):
        whl = self.wheel({"demo/empty.py": b""})
        src = GitSource(self.repo({"demo/empty.py": b""}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["files"][0]["sha256_wheel"],
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_bom_difference_detected(self):
        whl = self.wheel({"demo/a.py": b"\xef\xbb\xbfx = 1\n"})
        src = GitSource(self.repo({"demo/a.py": b"x = 1\n"}), "v1")
        self.assertEqual(self.cert(whl, src)["verdict"], "DIVERGENT")

    def test_missing_module_in_source(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n", "demo/ghost.py": b"y = 2\n"})
        src = GitSource(self.repo({"demo/__init__.py": b"x = 1\n"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "DIVERGENT")
        self.assertEqual(cert["comparison"]["missing_in_source"], 1)

    def test_source_only_module_inside_package(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = GitSource(self.repo({
            "demo/__init__.py": b"x = 1\n",
            "demo/extra.py": b"z = 3\n",
            "tests/test_demo.py": b"def test(): pass\n",
        }), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "DIVERGENT")
        self.assertEqual([e["path"] for e in cert["source_only"]], ["demo/extra.py"])

    def test_allow_glob_explains_generated_file(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n", "demo/_version.py": b"V = '1.0'\n"})
        src = GitSource(self.repo({"demo/__init__.py": b"x = 1\n"}), "v1")
        cert = self.cert(whl, src, allow=["demo/_version.py"])
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["comparison"]["allowed_divergences"], 1)
        self.assertEqual(cert["comparison"]["unexplained_divergences"], 0)

    def test_allow_glob_does_not_hide_other_files(self):
        whl = self.wheel({"demo/a.py": b"1\n", "demo/b.py": b"2\n"})
        src = GitSource(self.repo({"demo/a.py": b"9\n", "demo/b.py": b"8\n"}), "v1")
        cert = self.cert(whl, src, allow=["demo/a.py"])
        self.assertEqual(cert["verdict"], "DIVERGENT")
        self.assertEqual(cert["comparison"]["unexplained_divergences"], 1)

    def test_ambiguous_candidates_prefer_exact(self):
        whl = self.wheel({"demo/a.py": b"exact\n"})
        src = GitSource(self.repo({"demo/a.py": b"exact\n", "src/demo/a.py": b"other\n"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["comparison"]["matched_via"]["ambiguous"], 1)
        self.assertEqual(cert["files"][0]["matched_via"], "exact")


class TestSdistMode(Base):
    def test_sdist_identical_with_prefix_strip(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = SdistSource(self.sdist({"demo/__init__.py": b"x = 1\n"}))
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["subject"]["source"]["kind"], "sdist")
        self.assertEqual(len(cert["subject"]["source"]["sha256"]), 64)

    def test_sdist_divergence(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = SdistSource(self.sdist({"demo/__init__.py": b"x = 1 \n"}))
        self.assertEqual(self.cert(whl, src)["verdict"], "DIVERGENT")

    def test_sdist_nested_layout(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = SdistSource(self.sdist({"src/demo/__init__.py": b"x = 1\n"}, top="demo-1.0"))
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "SAME_SOURCE")
        self.assertEqual(cert["comparison"]["matched_via"]["src/"], 1)


class TestCertificate(Base):
    def test_digest_ignores_generated_at(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = GitSource(self.repo({"demo/__init__.py": b"x = 1\n"}), "v1")
        c1 = self.cert(whl, src)
        c2 = self.cert(whl, src)
        c2["generated_at"] = "2030-12-31T23:59:59Z"
        self.assertEqual(c1["certificate_sha256"], c2["certificate_sha256"])
        self.assertEqual(certificate_digest(c2), c1["certificate_sha256"])

    def test_digest_changes_when_verdict_changes(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        same = self.cert(whl, GitSource(self.repo({"demo/__init__.py": b"x = 1\n"}), "v1"))
        diff = self.cert(whl, GitSource(self.repo({"demo/__init__.py": b"x = 2\n"}, name="repo2"), "v1"))
        self.assertNotEqual(same["certificate_sha256"], diff["certificate_sha256"])

    def test_manifest_hashes_equal_for_same_bytes(self):
        whl = self.wheel({"demo/a.py": b"same\n", "demo/b.py": b"too\n"})
        src = GitSource(self.repo({"demo/a.py": b"same\n", "demo/b.py": b"too\n"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["manifest_sha256"]["wheel_py"], cert["manifest_sha256"]["source_py"])

    def test_no_comparable_files_is_inconclusive(self):
        whl = self.wheel({"demo/data.txt": b"nope"})
        src = GitSource(self.repo({"demo/data.txt": b"nope"}), "v1")
        cert = self.cert(whl, src)
        self.assertEqual(cert["verdict"], "INCONCLUSIVE")

    def test_pypi_digest_mismatch_raises(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        src = GitSource(self.repo({"demo/__init__.py": b"x = 1\n"}), "v1")
        with self.assertRaises(ToolError):
            build_certificate(
                project="demo", version="1.0", wheel_path=whl,
                wheel_expected_sha256="0" * 64, wheel_source_url=None,
                source=src, allow=(), generated_at="2026-01-01T00:00:00Z",
            )


class TestCli(Base):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.main(argv)
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def test_exit_code_same_and_json_out(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        repo = self.repo({"demo/__init__.py": b"x = 1\n"})
        out_json = os.path.join(self.root, "cert.json")
        code, _, err = self.run_cli([
            "check", "--wheel", whl, "--against", "git", "--repo", repo,
            "--ref", "v1", "--json-out", out_json, "--quiet",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        cert = json.load(open(out_json))
        self.assertEqual(cert["verdict"], "SAME_SOURCE")

    def test_exit_code_divergent(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        repo = self.repo({"demo/__init__.py": b"x = 2\n"})
        code, out, _ = self.run_cli([
            "check", "--wheel", whl, "--against", "git", "--repo", repo, "--ref", "v1",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["verdict"], "DIVERGENT")

    def test_exit_code_inconclusive(self):
        whl = self.wheel({"demo/a.txt": b"x"})
        repo = self.repo({"demo/a.txt": b"x"})
        code, _, _ = self.run_cli([
            "check", "--wheel", whl, "--against", "git", "--repo", repo,
            "--ref", "v1", "--json-out", os.path.join(self.root, "c.json"),
        ])
        self.assertEqual(code, 3)

    def test_verify_detects_tampering(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        repo = self.repo({"demo/__init__.py": b"x = 1\n"})
        path = os.path.join(self.root, "cert.json")
        self.run_cli(["check", "--wheel", whl, "--against", "git", "--repo", repo,
                      "--ref", "v1", "--json-out", path, "--quiet"])
        code, out, _ = self.run_cli(["verify", path])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])

        cert = json.load(open(path))
        cert["verdict"] = "DIVERGENT"
        with open(path, "w") as fh:
            json.dump(cert, fh)
        code, out, _ = self.run_cli(["verify", path])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["valid"])

    def test_sdist_mode_cli(self):
        whl = self.wheel({"demo/__init__.py": b"x = 1\n"})
        sdist = self.sdist({"demo/__init__.py": b"x = 1\n"})
        code, _, _ = self.run_cli([
            "check", "--wheel", whl, "--against", "sdist", "--sdist", sdist,
            "--json-out", os.path.join(self.root, "s.json"),
        ])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

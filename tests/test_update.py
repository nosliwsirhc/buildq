# SPDX-License-Identifier: MIT
"""Tests for buildq self-update (`buildq update`, install.json metadata, and safe extraction)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from buildq import client, update  # noqa: E402


def _make_tarball(entries: dict[str, bytes | str | tuple[str, str]]) -> bytes:
    """Build an in-memory .tar.gz archive.

    Value types:
      - bytes / str: regular file contents
      - ("symlink", target): symlink entry
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, val in entries.items():
            info = tarfile.TarInfo(name=name)
            if isinstance(val, tuple) and val[0] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = val[1]
                tf.addfile(info)
            else:
                raw = val.encode("utf-8") if isinstance(val, str) else val
                info.size = len(raw)
                info.mode = 0o755 if name.endswith(".sh") else 0o644
                tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


class UpdateHelpersTest(unittest.TestCase):
    def test_parse_github_repo(self):
        self.assertEqual(update.parse_github_repo("https://github.com/nosliwsirhc/buildq.git"), "nosliwsirhc/buildq")
        self.assertEqual(update.parse_github_repo("https://github.com/nosliwsirhc/buildq/"), "nosliwsirhc/buildq")
        self.assertEqual(update.parse_github_repo("git@github.com:nosliwsirhc/buildq.git"), "nosliwsirhc/buildq")
        self.assertEqual(update.parse_github_repo("ssh://git@github.com/nosliwsirhc/buildq.git"), "nosliwsirhc/buildq")
        self.assertIsNone(update.parse_github_repo("https://gitlab.com/foo/bar.git"))
        self.assertIsNone(update.parse_github_repo("/tmp/local-repo"))

    def test_safe_extract_tarball_valid(self):
        data = _make_tarball({
            "nosliwsirhc-buildq-abc1234/install.sh": "#!/bin/sh\necho ok\n",
            "nosliwsirhc-buildq-abc1234/src/buildq/__init__.py": "VERSION = '0.2.0'\n",
            "nosliwsirhc-buildq-abc1234/bin/buildqd": ("symlink", "buildq"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            extracted = update.safe_extract_tarball(data, tmp)
            self.assertTrue(os.path.isfile(os.path.join(extracted, "install.sh")))
            self.assertTrue(os.path.isdir(os.path.join(extracted, "src", "buildq")))

    def test_safe_extract_tarball_rejects_traversal(self):
        data = _make_tarball({
            "../escape.txt": "bad",
            "repo/install.sh": "#!/bin/sh\n",
            "repo/src/buildq/__init__.py": "",
        })
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(update.UpdateError):
                update.safe_extract_tarball(data, tmp)

    def test_safe_extract_tarball_rejects_escaping_symlink(self):
        data = _make_tarball({
            "repo/install.sh": "#!/bin/sh\n",
            "repo/src/buildq/__init__.py": "",
            "repo/evil": ("symlink", "../../../../etc/passwd"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(update.UpdateError):
                update.safe_extract_tarball(data, tmp)

    def test_safe_extract_tarball_rejects_missing_buildq(self):
        data = _make_tarball({"repo/README.md": "hello"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(update.UpdateError):
                update.safe_extract_tarball(data, tmp)


class GitAndInstallerUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="buildq-upd-test-")
        self.base = self.tmp.name
        self.home = os.path.join(self.base, "home")
        self.prefix = os.path.join(self.home, ".local")
        self.state = os.path.join(self.home, ".local", "state", "buildq")
        self.config = os.path.join(self.home, ".config")
        self.data = os.path.join(self.home, ".local", "share")
        os.makedirs(self.home, exist_ok=True)

        # Create a bare remote and a working checkout seeded with a minimal stub install.sh.
        self.remote = os.path.join(self.base, "remote.git")
        self.checkout = os.path.join(self.base, "checkout")
        self._git(self.base, "init", "--bare", "-b", "main", self.remote)
        self._git(self.base, "clone", self.remote, self.checkout)
        self._git(self.checkout, "config", "user.email", "test@example.com")
        self._git(self.checkout, "config", "user.name", "Test")

        self.install_log = os.path.join(self.base, "install.log")
        self._write_stub_repo(self.checkout, "v1")
        self._git(self.checkout, "add", ".")
        self._git(self.checkout, "commit", "-m", "initial")
        self._git(self.checkout, "push", "-u", "origin", "main")
        self.sha1 = self._git(self.checkout, "rev-parse", "HEAD").strip()

        os.makedirs(self.state, exist_ok=True)
        self._write_install_meta(commit=self.sha1, source_dir=self.checkout)

        self._env_patch = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
            "XDG_CONFIG_HOME": self.config,
            "XDG_DATA_HOME": self.data,
            "INSTALL_LOG": self.install_log,
        })
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.tmp.cleanup()

    def _git(self, cwd: str, *args: str) -> str:
        res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)
        return res.stdout

    def _write_stub_repo(self, repo_dir: str, marker: str) -> None:
        os.makedirs(os.path.join(repo_dir, "src", "buildq"), exist_ok=True)
        with open(os.path.join(repo_dir, "src", "buildq", "__init__.py"), "w", encoding="utf-8") as f:
            f.write(f"MARKER = {marker!r}\n")
        installer = os.path.join(repo_dir, "install.sh")
        with open(installer, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/sh\n"
                "set -eu\n"
                "echo \"installed:$BUILDQ_INSTALL_COMMIT:$*\" >> \"$INSTALL_LOG\"\n"
                "python3 - \"$XDG_STATE_HOME/buildq/install.json\" \"$BUILDQ_INSTALL_COMMIT\" <<'PY'\n"
                "import json, sys\n"
                "p, c = sys.argv[1], sys.argv[2]\n"
                "d = json.load(open(p))\n"
                "d['commit'] = c\n"
                "json.dump(d, open(p, 'w'))\n"
                "PY\n"
            )
        os.chmod(installer, 0o755)

    def _write_install_meta(self, *, commit: str | None, source_dir: str | None) -> None:
        with open(os.path.join(self.state, "install.json"), "w", encoding="utf-8") as f:
            json.dump({
                "prefix": self.prefix,
                "python": sys.executable,
                "source_dir": source_dir,
                "commit": commit,
                "repo": "nosliwsirhc/buildq",
                "branch": "main",
                "shims": False,
                "skill": False,
                "auto_update": False,
            }, f)

    def _push_upstream_commit(self, marker: str = "v2") -> str:
        pusher = os.path.join(self.base, f"pusher-{marker}")
        self._git(self.base, "clone", self.remote, pusher)
        self._git(pusher, "config", "user.email", "test@example.com")
        self._git(pusher, "config", "user.name", "Test")
        self._write_stub_repo(pusher, marker)
        self._git(pusher, "add", ".")
        self._git(pusher, "commit", "-m", f"bump to {marker}")
        self._git(pusher, "push", "origin", "main")
        return self._git(pusher, "rev-parse", "HEAD").strip()

    def _run_update(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = client.main(["update", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_git_up_to_date_and_fast_forward_update(self):
        rc, out, _ = self._run_update("--check", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "up_to_date")
        self.assertEqual(data["latest"], self.sha1)

        sha2 = self._push_upstream_commit("v2")

        rc, out, _ = self._run_update("--check", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "update_available")
        self.assertEqual(data["installed"], self.sha1)
        self.assertEqual(data["latest"], sha2)

        rc, out, _ = self._run_update("--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "updated")
        self.assertEqual(data["latest"], sha2)
        self.assertEqual(self._git(self.checkout, "rev-parse", "HEAD").strip(), sha2)
        self.assertEqual(update.load_install_meta()["commit"], sha2)
        self.assertTrue(os.path.isfile(self.install_log))

    def test_git_detached_head_fast_forwards(self):
        self._git(self.checkout, "checkout", "--detach", "HEAD")
        sha2 = self._push_upstream_commit("v2-detached")
        rc, out, _ = self._run_update("--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "updated")
        self.assertEqual(self._git(self.checkout, "rev-parse", "HEAD").strip(), sha2)

    def test_git_dirty_checkout_refuses_or_skips_in_auto(self):
        with open(os.path.join(self.checkout, "dirty.txt"), "w", encoding="utf-8") as f:
            f.write("uncommitted\n")
        rc, _, err = self._run_update()
        self.assertEqual(rc, 1)
        self.assertIn("uncommitted changes", err)

        rc, out, _ = self._run_update("--auto", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "skipped")

    def test_git_non_main_branch_refuses_or_skips_in_auto(self):
        self._git(self.checkout, "checkout", "-b", "feature/wip")
        rc, _, err = self._run_update()
        self.assertEqual(rc, 1)
        self.assertIn("feature/wip", err)

        rc, out, _ = self._run_update("--auto", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "skipped")

    def test_when_idle_skips_while_builds_running(self):
        self._push_upstream_commit("v2-idle")
        with mock.patch.object(update, "_check_queue_idle", return_value=2):
            rc, out, _ = self._run_update("--when-idle", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "skipped")
        self.assertIn("2 build(s)", data["reason"])

    def test_github_fallback_check_and_install(self):
        new_sha = "b" * 40
        with open(os.path.join(self.checkout, "install.sh"), encoding="utf-8") as f:
            stub_installer = f.read()
        tarball = _make_tarball({
            f"nosliwsirhc-buildq-{new_sha[:7]}/install.sh": stub_installer,
            f"nosliwsirhc-buildq-{new_sha[:7]}/src/buildq/__init__.py": "VERSION = '0.2.0'\n",
        })

        def fake_https_get(url: str, **_kwargs) -> bytes:
            if "/commits/" in url:
                return json.dumps({"sha": new_sha}).encode("utf-8")
            if "/tarball/" in url:
                return tarball
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch.object(update, "_https_get", side_effect=fake_https_get):
            rc, out, _ = self._run_update("--source", "github", "--check", "--json")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["status"], "update_available")

            rc, out, _ = self._run_update("--source", "github", "--json")
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(data["status"], "updated")
            self.assertEqual(data["source"], "github")
            self.assertEqual(data["latest"], new_sha)
            self.assertEqual(update.load_install_meta()["commit"], new_sha)

    def test_status_json(self):
        rc, out, _ = self._run_update("--status", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "status")
        self.assertEqual(data["installed"], self.sha1)
        self.assertEqual(data["source_dir"], self.checkout)


class RealInstallMetadataTest(unittest.TestCase):
    def test_install_sh_writes_and_uninstall_removes_install_json(self):
        with tempfile.TemporaryDirectory(prefix="buildq-inst-meta-") as tmp:
            home = os.path.join(tmp, "home")
            prefix = os.path.join(home, ".local")
            state = os.path.join(home, ".local", "state", "buildq")
            os.makedirs(home, exist_ok=True)
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "PREFIX": prefix,
                "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
                "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus",
            })
            res = subprocess.run(
                ["/bin/sh", os.path.join(ROOT, "install.sh"), "--no-start"],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)
            meta_path = os.path.join(state, "install.json")
            self.assertTrue(os.path.isfile(meta_path))
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["prefix"], os.path.realpath(prefix))
            self.assertEqual(meta["source_dir"], os.path.realpath(ROOT))
            self.assertTrue(meta["commit"])
            self.assertEqual(meta["branch"], "main")

            res2 = subprocess.run(
                ["/bin/sh", os.path.join(ROOT, "install.sh"), "--uninstall"],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stdout + res2.stderr)
            self.assertFalse(os.path.exists(meta_path))


if __name__ == "__main__":
    unittest.main()

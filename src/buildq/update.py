# SPDX-License-Identifier: MIT
"""Self-update for buildq: git checkout fast-forward with GitHub archive fallback."""

from __future__ import annotations

import errno
import fcntl
import http.client
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from pathlib import Path

from . import common
from .common import VERSION

DEFAULT_REPO = "nosliwsirhc/buildq"
DEFAULT_BRANCH = "main"
_GITHUB_API_HOST = "api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_MAX_REDIRECTS = 5
_HTTP_TIMEOUT = 20
_MAX_TARBALL_BYTES = 50 * 1024 * 1024  # 50 MiB safety cap


class UpdateError(Exception):
    """Recoverable error during update check or installation."""

    def __init__(self, message: str, *, skip_in_auto: bool = False):
        super().__init__(message)
        self.skip_in_auto = skip_in_auto


def install_meta_path() -> str:
    return os.path.join(common.default_state_dir(), "install.json")


def load_install_meta() -> dict:
    path = install_meta_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_github_repo(url: str) -> str | None:
    """Extract `owner/repo` from a GitHub HTTPS or SSH remote URL."""
    url = (url or "").strip()
    for pattern in (
        r"^https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
    ):
        m = re.match(pattern, url)
        if m and _REPO_RE.fullmatch(m.group(1)):
            return m.group(1)
    return None


def _short_sha(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


def _run_git(repo_dir: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise UpdateError("git is not installed", skip_in_auto=False) from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError(f"git {args[0]} timed out after {timeout:.0f}s", skip_in_auto=True) from e


def _https_get(url: str, *, headers: dict[str, str] | None = None,
               max_bytes: int = _MAX_TARBALL_BYTES, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Perform an HTTPS-only GET, following up to _MAX_REDIRECTS HTTPS redirects."""
    req_headers = {"User-Agent": f"buildq/{VERSION}"}
    if headers:
        req_headers.update(headers)

    current = url
    ctx = ssl.create_default_context()
    for _ in range(_MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlsplit(current)
        if parsed.scheme != "https" or not parsed.hostname:
            raise UpdateError(f"refusing non-HTTPS URL: {current}")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout, context=ctx)
        try:
            conn.request("GET", path, headers=req_headers)
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.read()
                if not location:
                    raise UpdateError(f"redirect {resp.status} without Location from {parsed.hostname}")
                current = urllib.parse.urljoin(current, location)
                continue
            if resp.status != 200:
                body = resp.read(512).decode("utf-8", errors="replace").strip()
                raise UpdateError(
                    f"HTTP {resp.status} from {parsed.hostname}: {body or resp.reason}",
                    skip_in_auto=(resp.status >= 500 or resp.status == 429),
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateError(f"response from {parsed.hostname} exceeded {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as e:
            raise UpdateError(f"network error reaching {parsed.hostname}: {e}", skip_in_auto=True) from e
        finally:
            conn.close()
    raise UpdateError(f"too many redirects fetching {url}", skip_in_auto=True)


def fetch_github_head_sha(repo: str, branch: str) -> str:
    """Return the latest commit SHA for `branch` in GitHub `repo`."""
    if not _REPO_RE.fullmatch(repo):
        raise UpdateError(f"invalid GitHub repository: {repo!r}")
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch:
        raise UpdateError(f"invalid branch name: {branch!r}")
    url = f"https://{_GITHUB_API_HOST}/repos/{repo}/commits/{urllib.parse.quote(branch, safe='')}"
    raw = _https_get(
        url,
        headers={"Accept": "application/vnd.github+json"},
        max_bytes=1024 * 1024,
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise UpdateError(f"invalid JSON from GitHub API: {e}", skip_in_auto=True) from e
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise UpdateError("GitHub API response did not include a valid commit SHA", skip_in_auto=True)
    return sha


def safe_extract_tarball(data: bytes, dest_dir: str) -> str:
    """Safely extract a GitHub source tarball into `dest_dir` and return the top-level repo dir."""
    dest_real = os.path.realpath(dest_dir)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        if not members:
            raise UpdateError("downloaded archive is empty")
        safe_members: list[tarfile.TarInfo] = []
        for m in members:
            # Skip PAX global headers and unsafe special files.
            if m.type == tarfile.XGLTYPE:
                continue
            if m.isdev() or m.isfifo():
                raise UpdateError(f"refusing special file in archive: {m.name}")
            if os.path.isabs(m.name):
                raise UpdateError(f"refusing absolute path in archive: {m.name}")
            parts = Path(m.name).parts
            if ".." in parts:
                raise UpdateError(f"refusing path traversal in archive: {m.name}")
            target = os.path.realpath(os.path.join(dest_real, m.name))
            if target != dest_real and not target.startswith(dest_real + os.sep):
                raise UpdateError(f"archive member escapes destination: {m.name}")
            if m.issym() or m.islnk():
                link_target = m.linkname
                if os.path.isabs(link_target):
                    raise UpdateError(f"refusing absolute link in archive: {m.name} -> {link_target}")
                base = os.path.dirname(target) if m.issym() else dest_real
                resolved = os.path.realpath(os.path.join(base, link_target))
                if resolved != dest_real and not resolved.startswith(dest_real + os.sep):
                    raise UpdateError(f"archive link escapes destination: {m.name} -> {link_target}")
            safe_members.append(m)

        # Use Python 3.12+'s data filter when available; fall back to validated extraction on 3.11.
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest_real, members=safe_members, filter="data")  # noqa: S202
        else:
            for m in safe_members:
                tf.extract(m, dest_real, set_attrs=True)  # noqa: S202

    entries = [os.path.join(dest_real, e) for e in os.listdir(dest_real)]
    dirs = [p for p in entries if os.path.isdir(p)]
    for d in dirs:
        if os.path.isfile(os.path.join(d, "install.sh")) and os.path.isdir(os.path.join(d, "src", "buildq")):
            return d
    raise UpdateError("downloaded archive does not contain buildq (missing install.sh or src/buildq)")


def download_github_source(repo: str, ref: str, dest_dir: str) -> str:
    """Download and extract a GitHub tarball for `repo` at `ref` into `dest_dir`."""
    url = f"https://{_GITHUB_API_HOST}/repos/{repo}/tarball/{urllib.parse.quote(ref, safe='')}"
    data = _https_get(url, headers={"Accept": "application/vnd.github+json"})
    return safe_extract_tarball(data, dest_dir)


def _detect_existing_shims(prefix: str) -> bool:
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    shimdir = os.path.join(xdg_data, "buildq", "shims")
    try:
        return os.path.isdir(shimdir) and any(os.scandir(shimdir))
    except OSError:
        return False


def _detect_existing_skill() -> bool:
    state = common.default_state_dir()
    skill_list = os.path.join(state, "skill-links")
    try:
        with open(skill_list, encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def _run_installer(source_dir: str, meta: dict, *, commit: str | None, repo: str, branch: str,
                   preserve_source_dir: bool = False) -> None:
    installer = os.path.join(source_dir, "install.sh")
    if not os.path.isfile(installer):
        raise UpdateError(f"installer not found at {installer}")

    prefix = meta.get("prefix") or os.path.join(os.path.expanduser("~"), ".local")
    python = meta.get("python") or sys.executable
    cmd = ["/bin/sh", installer]
    if meta.get("shims") or _detect_existing_shims(prefix):
        cmd.append("--shims")
    if meta.get("skill") or _detect_existing_skill():
        cmd.append("--skill")

    env = os.environ.copy()
    env["PREFIX"] = str(prefix)
    if python and os.path.isfile(str(python)):
        env["PYTHON"] = str(python)
    if commit:
        env["BUILDQ_INSTALL_COMMIT"] = commit
    if repo:
        env["BUILDQ_INSTALL_REPO"] = repo
    if branch:
        env["BUILDQ_INSTALL_BRANCH"] = branch
    if preserve_source_dir and meta.get("source_dir"):
        env["BUILDQ_PRESERVE_SOURCE_DIR"] = str(meta["source_dir"])

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(f"install.sh failed (exit {proc.returncode}):\n{detail}")


def _check_queue_idle() -> int | None:
    """Return count of currently running builds, or None if daemon is unreachable."""
    from . import client
    reply = client.request({"op": "status"}, timeout=5, connect_timeout=2)
    if not isinstance(reply, dict):
        return None
    running = reply.get("running")
    if isinstance(running, list):
        return len(running)
    return 0


def _have_systemd() -> bool:
    if not shutil.which("systemctl"):
        return False
    res = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, check=False)
    return res.returncode == 0


def _timer_status() -> dict:
    if not _have_systemd():
        return {"systemd": False, "enabled": False, "active": False, "next": None}
    en = subprocess.run(
        ["systemctl", "--user", "is-enabled", "buildq-update.timer"],
        capture_output=True, text=True, check=False,
    )
    ac = subprocess.run(
        ["systemctl", "--user", "is-active", "buildq-update.timer"],
        capture_output=True, text=True, check=False,
    )
    nxt = subprocess.run(
        ["systemctl", "--user", "show", "-p", "NextElapseUSecRealtime", "--value", "buildq-update.timer"],
        capture_output=True, text=True, check=False,
    )
    next_val = nxt.stdout.strip() if nxt.returncode == 0 else ""
    return {
        "systemd": True,
        "enabled": en.returncode == 0 and en.stdout.strip() == "enabled",
        "active": ac.returncode == 0 and ac.stdout.strip() == "active",
        "next": next_val or None,
    }


def _set_timer_interval(interval: str | None) -> None:
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    dropin_dir = os.path.join(xdg_cfg, "systemd", "user", "buildq-update.timer.d")
    dropin = os.path.join(dropin_dir, "interval.conf")
    if not interval:
        return
    # Validate interval looks like a systemd timespan (e.g. 30m, 1h, 6h, 1d).
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*(?:s|sec|m|min|h|hr|d|day|w|week)s?", interval.strip()):
        raise UpdateError(f"invalid interval {interval!r} (try 30m, 1h, 6h, or 1d)")
    os.makedirs(dropin_dir, exist_ok=True)
    with open(dropin, "w", encoding="utf-8") as f:
        f.write(f"[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec={interval.strip()}\n")


def _manage_timer(action: str, interval: str | None = None) -> int:
    if not _have_systemd():
        print("buildq: systemd user session is not available; run `buildq update --auto` from cron instead.",
              file=sys.stderr)
        return 1
    if action == "enable":
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        timer_unit = os.path.join(xdg_cfg, "systemd", "user", "buildq-update.timer")
        if not os.path.exists(timer_unit):
            print("buildq: buildq-update.timer is not installed; run `./install.sh --auto-update` first.",
                  file=sys.stderr)
            return 1
        if interval:
            _set_timer_interval(interval)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        res = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "buildq-update.timer"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            print(f"buildq: failed to enable buildq-update.timer: {res.stderr.strip()}", file=sys.stderr)
            return 1
        st = _timer_status()
        extra = f" (next run: {st['next']})" if st.get("next") else ""
        print(f"buildq: automatic updates enabled{extra}")
        return 0
    if action == "disable":
        res = subprocess.run(
            ["systemctl", "--user", "disable", "--now", "buildq-update.timer"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0 and "not loaded" not in res.stderr.lower():
            print(f"buildq: failed to disable buildq-update.timer: {res.stderr.strip()}", file=sys.stderr)
            return 1
        print("buildq: automatic updates disabled")
        return 0
    return 2


def _update_via_git(source_dir: str, meta: dict, branch: str, repo: str, *,
                    check_only: bool, force: bool) -> dict:
    # Verify working tree is clean.
    status = _run_git(source_dir, "status", "--porcelain")
    if status.returncode != 0:
        raise UpdateError(f"git status failed in {source_dir}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise UpdateError(
            f"checkout {source_dir} has uncommitted changes; skipping update",
            skip_in_auto=True,
        )

    # Allow the target branch or detached HEAD (e.g. CI boxes), but never hijack a feature branch.
    cur_branch = _run_git(source_dir, "symbolic-ref", "--short", "-q", "HEAD")
    branch_name = cur_branch.stdout.strip() if cur_branch.returncode == 0 else None
    if branch_name is not None and branch_name != branch:
        raise UpdateError(
            f"checkout {source_dir} is on branch {branch_name!r} (expected {branch!r}); skipping update",
            skip_in_auto=True,
        )

    head_proc = _run_git(source_dir, "rev-parse", "HEAD")
    if head_proc.returncode != 0:
        raise UpdateError(f"couldn't read HEAD in {source_dir}: {head_proc.stderr.strip()}")
    local_sha = head_proc.stdout.strip()

    fetch = _run_git(source_dir, "fetch", "--quiet", "origin", branch, timeout=30.0)
    if fetch.returncode != 0:
        raise UpdateError(
            f"git fetch origin {branch} failed: {fetch.stderr.strip()}",
            skip_in_auto=True,
        )

    remote_proc = _run_git(source_dir, "rev-parse", "FETCH_HEAD")
    if remote_proc.returncode != 0:
        raise UpdateError(f"couldn't resolve FETCH_HEAD in {source_dir}: {remote_proc.stderr.strip()}")
    remote_sha = remote_proc.stdout.strip()

    installed_sha = meta.get("commit") or local_sha
    needs_pull = local_sha != remote_sha
    needs_install = force or needs_pull or (installed_sha != remote_sha)

    if not needs_install:
        return {
            "status": "up_to_date",
            "source": "git",
            "source_dir": source_dir,
            "installed": installed_sha,
            "latest": remote_sha,
            "updated": False,
        }

    if check_only:
        return {
            "status": "update_available",
            "source": "git",
            "source_dir": source_dir,
            "installed": installed_sha,
            "latest": remote_sha,
            "updated": False,
        }

    if needs_pull:
        ff = _run_git(source_dir, "merge", "--ff-only", remote_sha)
        if ff.returncode != 0:
            raise UpdateError(
                f"cannot fast-forward {source_dir} to {_short_sha(remote_sha)} (diverged history?): {ff.stderr.strip()}",
                skip_in_auto=True,
            )

    _run_installer(source_dir, meta, commit=remote_sha, repo=repo, branch=branch)
    return {
        "status": "updated",
        "source": "git",
        "source_dir": source_dir,
        "installed": installed_sha,
        "latest": remote_sha,
        "updated": True,
    }


def _update_via_github(meta: dict, repo: str, branch: str, *, check_only: bool, force: bool) -> dict:
    installed_sha = meta.get("commit")
    remote_sha = fetch_github_head_sha(repo, branch)
    if not force and installed_sha and installed_sha == remote_sha:
        return {
            "status": "up_to_date",
            "source": "github",
            "repo": repo,
            "branch": branch,
            "installed": installed_sha,
            "latest": remote_sha,
            "updated": False,
        }
    if check_only:
        return {
            "status": "update_available",
            "source": "github",
            "repo": repo,
            "branch": branch,
            "installed": installed_sha,
            "latest": remote_sha,
            "updated": False,
        }
    with tempfile.TemporaryDirectory(prefix="buildq-update-") as tmp:
        extracted = download_github_source(repo, remote_sha, tmp)
        _run_installer(
            extracted,
            meta,
            commit=remote_sha,
            repo=repo,
            branch=branch,
            preserve_source_dir=True,
        )
    return {
        "status": "updated",
        "source": "github",
        "repo": repo,
        "branch": branch,
        "installed": installed_sha,
        "latest": remote_sha,
        "updated": True,
    }


def _emit(result: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return
    st = result.get("status")
    src = result.get("source", "")
    old_s = _short_sha(result.get("installed"))
    new_s = _short_sha(result.get("latest"))
    if st == "up_to_date":
        print(f"buildq is already up to date ({new_s}, via {src}).")
    elif st == "update_available":
        print(f"buildq update available: {old_s} -> {new_s} (via {src}). Run `buildq update` to install.")
    elif st == "updated":
        print(f"buildq updated: {old_s} -> {new_s} (via {src}).")
    elif st == "skipped":
        print(f"buildq update skipped: {result.get('reason')}")
    elif st == "status":
        print(f"installed commit: {_short_sha(result.get('installed'))}")
        print(f"source: {result.get('source_dir') or result.get('repo')} (branch: {result.get('branch')})")
        timer = result.get("timer") or {}
        if timer.get("systemd"):
            state = "enabled" if timer.get("enabled") else "disabled"
            if timer.get("active") and timer.get("next"):
                state += f" (next: {timer['next']})"
            print(f"auto-update timer: {state}")
        else:
            print("auto-update timer: systemd user session not available")


def cmd_update(args) -> int:
    if getattr(args, "enable", False):
        try:
            return _manage_timer("enable", getattr(args, "interval", None))
        except UpdateError as e:
            print(f"buildq: {e}", file=sys.stderr)
            return 1
    if getattr(args, "disable", False):
        return _manage_timer("disable")

    meta = load_install_meta()
    repo = getattr(args, "repo", None) or meta.get("repo") or DEFAULT_REPO
    branch = getattr(args, "branch", None) or meta.get("branch") or DEFAULT_BRANCH
    source_dir = meta.get("source_dir")
    has_git_dir = bool(source_dir and os.path.isdir(os.path.join(str(source_dir), ".git")))

    if getattr(args, "status", False):
        res = {
            "status": "status",
            "installed": meta.get("commit"),
            "installed_at": meta.get("installed_at"),
            "source_dir": source_dir if has_git_dir else None,
            "repo": repo,
            "branch": branch,
            "shims": bool(meta.get("shims")),
            "skill": bool(meta.get("skill")),
            "timer": _timer_status(),
        }
        _emit(res, as_json=bool(getattr(args, "json", False)))
        return 0

    as_json = bool(getattr(args, "json", False))
    auto = bool(getattr(args, "auto", False))
    check_only = bool(getattr(args, "check", False))
    force = bool(getattr(args, "force", False))
    when_idle = bool(getattr(args, "when_idle", False))
    source_pref = getattr(args, "source", "auto") or "auto"

    # Serialize concurrent updates.
    state = common.default_state_dir()
    os.makedirs(state, exist_ok=True)
    lock_path = os.path.join(state, "update.lock")
    lock_f = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if auto else 0)
            fcntl.flock(lock_f.fileno(), flags)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                _emit({"status": "skipped", "reason": "another update is already running", "updated": False},
                      as_json=as_json)
                return 0
            raise

        if when_idle and not check_only:
            running = _check_queue_idle()
            if running:
                _emit(
                    {"status": "skipped", "reason": f"{running} build(s) currently running", "updated": False},
                    as_json=as_json,
                )
                return 0

        try:
            use_git = (source_pref == "git") or (source_pref == "auto" and has_git_dir)
            if use_git:
                if not has_git_dir:
                    raise UpdateError("no recorded git checkout found; use `--source github` or re-run install.sh")
                result = _update_via_git(
                    str(source_dir), meta, branch, repo, check_only=check_only, force=force,
                )
            else:
                result = _update_via_github(meta, repo, branch, check_only=check_only, force=force)
        except UpdateError as e:
            if auto and e.skip_in_auto:
                _emit({"status": "skipped", "reason": str(e), "updated": False}, as_json=as_json)
                return 0
            if as_json:
                print(json.dumps({"status": "error", "error": str(e), "updated": False}, indent=2))
            else:
                print(f"buildq: update failed: {e}", file=sys.stderr)
            return 1

        _emit(result, as_json=as_json)
        return 0
    finally:
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_f.close()

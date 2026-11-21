# SPDX-License-Identifier: MIT
"""Build supervisor (`buildqd --supervise`): output capture, timeout, and resource stats."""

from __future__ import annotations

import collections
import os
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import time

from . import commands, common, machine
from .common import DEVNULL

class CappedOutput:
    """Writes a build's output to its log, up to `limit` bytes; past that it keeps only the last
    quarter of `limit` and appends it, with a note, when the build ends."""

    def __init__(self, fd: int, limit: int, note: bool = True):
        self.fd = fd
        self.limit = limit
        self.note = note  # not in stdout.log, which a caller may be parsing
        self.written = 0
        self.tail: collections.deque = collections.deque()
        self.tail_bytes = 0
        self.tail_limit = max(limit // 4, 1)
        self.dropped = 0

    def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except InterruptedError:
                continue
            except OSError:
                return  # a full disk mustn't stop the build
            view = view[n:]

    def write(self, data: bytes) -> None:
        if self.limit <= 0 or self.written < self.limit:
            room = len(data) if self.limit <= 0 else self.limit - self.written
            self._write_all(data[:room])
            self.written += min(room, len(data))
            data = data[room:]
        if data:
            self.tail.append(data)
            self.tail_bytes += len(data)
            self.dropped += len(data)
            while self.tail and self.tail_bytes - len(self.tail[0]) >= self.tail_limit:
                self.tail_bytes -= len(self.tail.popleft())

    def finish(self) -> None:
        if not self.dropped:
            return
        kept = b"".join(self.tail)[-self.tail_limit:]
        if self.note:
            self._write_all(f"\n[buildq: {self.dropped - len(kept)} bytes of output left out here; "
                            f"the last {len(kept)} follow]\n".encode())
        self._write_all(kept)


def oom_of(result: dict) -> bool:
    """Whether a build's result shows it was killed for memory at its own cap. A kill that came
    from outside (the slice's limit as other work grew, systemd-oomd) doesn't say the build needs
    more than it had."""
    return bool(result.get("oom_kills")) and bool(result.get("hit_own_cap", True))


def signal_group(pgid: int, sig: int) -> bool:
    """Signal a process group; False if it has no processes left."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def apply_rlimits(limits) -> None:
    """The submitter's soft limits (open files, stack, core size), within this process's hard ones."""
    import resource
    names = {"NOFILE": resource.RLIMIT_NOFILE, "STACK": resource.RLIMIT_STACK, "CORE": resource.RLIMIT_CORE}
    for name, value in (limits or {}).items():
        which = names.get(name)
        if which is None or not isinstance(value, int):
            continue
        try:
            _, hard = resource.getrlimit(which)
            soft = value if hard == resource.RLIM_INFINITY or (0 <= value <= hard) else hard
            if value == resource.RLIM_INFINITY and hard != resource.RLIM_INFINITY:
                soft = hard
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            continue


def _reap_other_children(main_pid: int) -> None:
    """Reap exited reparented children without touching `main_pid`. Uses POSIX waitid(P_ALL,
    WNOWAIT) so it works on all Linux kernels even without CONFIG_PROC_CHILDREN."""
    waitid = getattr(os, "waitid", None)
    if waitid is not None and hasattr(os, "WNOWAIT"):
        for _ in range(64):
            try:
                si = waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return
            except OSError:
                break
            if si is None or si.si_pid <= 0 or si.si_pid == main_pid:
                return
            try:
                os.waitpid(si.si_pid, os.WNOHANG)
            except OSError:
                return
        return
    for orphan in machine.proc_children(os.getpid()):
        if orphan != main_pid:
            try:
                os.waitpid(orphan, os.WNOHANG)
            except OSError:
                pass


def _reap_all_children() -> None:
    """Reap all currently exited child processes (used after the main child has already exited)."""
    while True:
        try:
            reaped, _ = os.waitpid(-1, os.WNOHANG)
            if reaped <= 0:
                break
        except OSError:
            break


def _effective_wrapper_cwd(argv: list[str], tool_at: int, base_cwd: str) -> str:
    """Resolve effective working directory if an `env -C DIR` / `--chdir=DIR` wrapper precedes the tool."""
    cwd = base_cwd
    i = 0
    while i < tool_at:
        a = argv[i]
        if a in ("-C", "--chdir") and i + 1 < tool_at:
            nxt = argv[i + 1]
            cwd = nxt if os.path.isabs(nxt) else os.path.normpath(os.path.join(cwd, nxt))
            i += 2
            continue
        if a.startswith("--chdir="):
            nxt = a.split("=", 1)[1]
            cwd = nxt if os.path.isabs(nxt) else os.path.normpath(os.path.join(cwd, nxt))
        i += 1
    return cwd


def _resolve_in_cwd(target: str, eff_cwd: str, eff_path: str | None) -> str | None:
    """Resolve executable `target` relative to `eff_cwd` and `eff_path` (never supervisor's cwd/PATH)."""
    if "/" in target:
        cand = target if os.path.isabs(target) else os.path.normpath(os.path.join(eff_cwd, target))
        return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None
    search_path = os.defpath if eff_path is None else eff_path
    for entry in search_path.split(os.pathsep):
        base = eff_cwd if not entry else (entry if os.path.isabs(entry) else os.path.normpath(os.path.join(eff_cwd, entry)))
        cand = os.path.join(base, target)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _downgrade_old_make_fifo(argv: list[str], cwd: str, env: dict[str, str], jobserver_fd: int | None,
                             granted: int | None = None, directory: str | None = None,
                             cancel_flag: list[int] | None = None) -> int | None:
    """Inside the confined build, detect GNU Make < 4.4 which aborts on `--jobserver-auth=fifo:`.
    Uses the build's effective cwd (`env -C`) and PATH, runs in its own process group honoring
    cancellation, replaces fifo flags with `-j<granted>` from the ticket (not caller-overridable
    BUILDQ_CORES), writes `make_fixed.json` so the daemon stops rebalancing it, and closes the fd."""
    if jobserver_fd is None or "--jobserver-auth=fifo:" not in env.get("MAKEFLAGS", ""):
        return jobserver_fd
    stripped = commands.strip_wrappers(argv)
    if not stripped:
        return jobserver_fd
    tool = os.path.basename(stripped[0])
    if tool not in ("make", "gmake"):
        return jobserver_fd
    tool_at = len(argv) - len(stripped)
    eff_cwd = _effective_wrapper_cwd(argv, tool_at, cwd)
    inline_idx, reset_at, unset_at = commands.wrapper_env_sites(argv, tool_at)
    if reset_at is not None:
        eff_path = argv[inline_idx["PATH"]].split("=", 1)[1] if "PATH" in inline_idx else None
        probe_env = {k: argv[idx].split("=", 1)[1] for k, idx in inline_idx.items()}
    else:
        probe_env = dict(env)
        for k in unset_at:
            probe_env.pop(k, None)
        for k, idx in inline_idx.items():
            probe_env[k] = argv[idx].split("=", 1)[1]
        eff_path = probe_env.get("PATH")
    exe = _resolve_in_cwd(stripped[0], eff_cwd, eff_path)
    if not exe:
        return jobserver_fd
    out_text = ""
    try:
        proc = subprocess.Popen([exe, "--version"], cwd=eff_cwd, env=probe_env, stdin=DEVNULL,
                                stdout=subprocess.PIPE, stderr=DEVNULL, text=True, process_group=0)
        deadline = time.monotonic() + 2.0
        while proc.poll() is None:
            if cancel_flag:
                signal_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)
                return jobserver_fd
            if time.monotonic() >= deadline:
                signal_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)
                return jobserver_fd
            time.sleep(0.02)
        out_text = proc.stdout.read() if proc.stdout else ""
        if proc.stdout:
            proc.stdout.close()
    except (OSError, subprocess.SubprocessError):
        return jobserver_fd
    m = re.search(r"GNU Make\s+(\d+)\.(\d+)", out_text or "")
    if not m or (int(m.group(1)), int(m.group(2))) >= (4, 4):
        return jobserver_fd
    fixed_j = str(max(1, int(granted))) if granted else (env.get("BUILDQ_CORES") or "1")
    for var in ("MAKEFLAGS", "MFLAGS"):
        val = env.get(var)
        if not val or "--jobserver-auth=fifo:" not in val:
            continue
        cleaned = re.sub(r"(?:^|\s)-j\d+\s+--jobserver-auth=fifo:\S+", f" -j{fixed_j}", val)
        cleaned = re.sub(r"(?:^|\s)--jobserver-auth=fifo:\S+", "", cleaned).strip()
        env[var] = cleaned
    if directory:
        try:
            common.write_json(os.path.join(directory, "make_fixed.json"), {"granted": int(fixed_j)})
        except OSError:
            pass
    try:
        os.close(jobserver_fd)
    except OSError:
        pass
    return None


def supervise(directory: str) -> int:
    """Run one build: its command, with its submitter's environment, directory and limits; its
    output into output.log (both streams) and stdout.log / stderr.log (each); and, whatever
    happens, its exit status into result.json."""
    os.umask(0o077)
    code, timed_out = 126, False
    outputs = []
    jobserver = None
    try:
        # Claim the environment atomically: if two supervisors ever start for one build (a launch
        # from before a daemon restart arriving late), only one gets it, and the other runs nothing.
        claimed = os.path.join(directory, f"env.{os.getpid()}.json")
        try:
            os.rename(os.path.join(directory, "env.json"), claimed)
        except FileNotFoundError:
            return 0  # another supervisor has this build; it writes the result
        me = machine.proc_identity(os.getpid())
        common.write_json(os.path.join(directory, "supervisor.json"), {"pid": os.getpid(), "start": me[0] if me else None})
        spec = common.read_json(os.path.join(directory, "ticket.json")) or {}
        subreaper_ok = False
        if spec.get("runner") in ("direct", "cgroup"):
            # Become a subreaper on non-systemd runners (under systemd, systemd --user already
            # owns cgroup teardown and subreaping). DirectRunner requires it for orphan cleanup.
            subreaper_ok = machine.set_child_subreaper()
        job = str(spec.get("id") or os.path.basename(directory))
        env = common.read_json(claimed)
        try:
            os.remove(claimed)  # the environment can hold secrets; it isn't kept once the build has it
        except OSError:
            pass
        parallel_path = os.path.join(directory, "parallel.json")
        parallel = common.read_json(parallel_path)
        try:
            os.remove(parallel_path)  # merged caller env vars can hold secrets; don't keep on disk
        except OSError:
            pass
        if isinstance(env, dict) and isinstance(parallel, dict):
            env.update({str(k): str(v) for k, v in parallel.items()})
        # Propagate the daemon's real state directory so buildq-rustc-guard and nested `buildq`
        # calls find `tickets/<job>/ticket.json` even when buildqd runs with custom `--state-dir`.
        if isinstance(env, dict):
            tickets_parent = os.path.dirname(os.path.abspath(directory))
            if os.path.basename(tickets_parent) == "tickets":
                env["BUILDQ_STATE_DIR"] = os.path.dirname(tickets_parent)
        limit = int(spec.get("max_log") or 0)

        def log_file(name: str, size: int, note: bool = True) -> CappedOutput:
            fd = os.open(os.path.join(directory, name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            output = CappedOutput(fd, size, note)
            outputs.append(output)
            return output

        combined = log_file("output.log", limit)
        streams = {1: log_file("stdout.log", limit // 2, note=False), 2: log_file("stderr.log", limit // 2)}

        def note(text: str) -> None:
            for output in (combined, streams[2]):
                output.write(text.encode())

        if spec.get("runner") == "direct" and not subreaper_ok:
            note("buildq: couldn't enable PR_SET_CHILD_SUBREAPER; refusing direct build without orphan cleanup\n")
            return code

        # Direct cgroup v2 runner: move this supervisor into the build's cgroup BEFORE spawning
        # the child so every process of the build is born under the cgroup's limits.
        if spec.get("runner") == "cgroup" and isinstance(spec.get("cg"), str) and spec["cg"]:
            cg = os.path.realpath(spec["cg"])
            if not cg.startswith("/sys/fs/cgroup/") or not re.fullmatch(
                rf"buildq-{re.escape(job)}(-r\d+)?", os.path.basename(cg)
            ):
                note(f"buildq: refusing invalid cgroup path {spec['cg']!r}\n")
                return code
            try:
                with open(os.path.join(cg, "cgroup.procs"), "w") as f:
                    f.write(f"{os.getpid()}\n")
                actual = machine.proc_cgroup(os.getpid())
                if not actual or os.path.realpath(actual) != cg:
                    raise OSError(f"process cgroup is {actual!r}, expected {cg!r}")
            except OSError as e:
                note(f"buildq: couldn't join build cgroup {cg}: {e}\n")
                return code
        nice_val = spec.get("nice")
        if isinstance(nice_val, int) and not isinstance(nice_val, bool) and nice_val != 0:
            try:
                os.nice(nice_val)
            except OSError:
                pass

        # Hold the build's jobserver open for as long as the build runs: a pipe's tokens last only
        # while someone has it open, and the daemon may restart in between.
        if spec.get("jobserver") and os.path.exists(spec["jobserver"]):
            try:
                jobserver = os.open(spec["jobserver"], os.O_RDWR | os.O_NONBLOCK)
            except OSError:
                jobserver = None
        apply_rlimits(spec.get("rlimits"))
        argv, cwd = spec.get("exec_argv") or spec.get("argv"), spec.get("cwd") or "/"
        timeout = spec.get("timeout")
        if not isinstance(env, dict) or not argv:
            note("buildq: this build's command or environment is missing; it can't run.\n")
            return code
        # Install cancellation signal handler BEFORE any pre-launch probe so a cancel arriving
        # during the Make version probe aborts cleanly without leaking the probe child.
        pre_cancel: list[int] = []

        def _pre_cancel_handler(signum, _frame):
            pre_cancel.append(signum)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
            signal.signal(sig, _pre_cancel_handler)
        # Check GNU Make < 4.4 inside build confinement (with build's exact cwd/env/limits) so old
        # make falls back cleanly to `-j<granted>` instead of aborting on `--jobserver-auth=fifo:`.
        jobserver = _downgrade_old_make_fifo(argv, cwd, env, jobserver, granted=spec.get("granted"),
                                             directory=directory, cancel_flag=pre_cancel)
        if pre_cancel:
            code = 128 + pre_cancel[-1]
            return code
        pipes = {1: os.pipe(), 2: os.pipe()}
        try:
            umask = spec.get("umask")
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=DEVNULL, stdout=pipes[1][1], stderr=pipes[2][1],
                                     process_group=0, umask=umask if isinstance(umask, int) else 0o022)
        except (OSError, ValueError) as e:
            for r, w in pipes.values():
                os.close(r)
                os.close(w)
            note(f"buildq: can't run {argv[0]} in {cwd}: {getattr(e, 'strerror', None) or e}\n")
            code = 127 if isinstance(e, FileNotFoundError) else 126
            return code
        for _, w in pipes.values():
            os.close(w)
        common.write_json(os.path.join(directory, "child.json"), {"pid": child.pid, "pgid": child.pid})

        cancel_signal: list[int] = []

        def forward(signum, _frame):
            signal_group(child.pid, signum)
            if signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
                cancel_signal.append(signum)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2):
            signal.signal(sig, forward)
        selector = selectors.DefaultSelector()
        for number_, (r, _) in pipes.items():
            os.set_blocking(r, False)
            selector.register(r, selectors.EVENT_READ, number_)
        open_pipes = len(pipes)
        deadline = time.monotonic() + float(timeout) if timeout else None
        term_sent = None

        def pump(wait: float) -> None:
            nonlocal open_pipes
            if not open_pipes:
                time.sleep(wait)
                return
            for key, _ in selector.select(wait):
                for _batch in range(64):  # at most 4 MiB at a time, so deadlines are still checked
                    try:
                        data = os.read(key.fd, 1 << 16)
                    except BlockingIOError:
                        break
                    except InterruptedError:
                        continue
                    if not data:
                        selector.unregister(key.fd)
                        open_pipes -= 1
                        break
                    combined.write(data)
                    streams[key.data].write(data)

        last = time.monotonic()
        my_pid = os.getpid()
        subreaper_active = spec.get("runner") in ("direct", "cgroup")
        forwarded_descendants = False
        while child.poll() is None:
            pump(0.5)
            if subreaper_active:
                _reap_other_children(child.pid)
                if cancel_signal and not forwarded_descendants:
                    forwarded_descendants = True
                    sig_to_send = cancel_signal[-1]
                    for dpid, dstart in machine.proc_tree_descendants(my_pid):
                        if dpid != child.pid:
                            machine.pidfd_signal(dpid, dstart, sig_to_send)
            tick = time.monotonic()
            if deadline and tick - last > 5:
                # A turn of this loop that took far longer than its half second: the build was paused
                # (frozen to free memory) or the machine stalled. That time doesn't count.
                deadline += tick - last - 0.5
            last = tick
            if deadline and time.monotonic() > deadline and term_sent is None:
                timed_out, term_sent = True, time.monotonic()
                note(f"\nbuildq: timed out after {float(timeout):.0f}s; stopping it\n")
                signal_group(child.pid, signal.SIGTERM)
                if subreaper_active:
                    for dpid, dstart in machine.proc_tree_descendants(my_pid):
                        machine.pidfd_signal(dpid, dstart, signal.SIGTERM)
            if term_sent and time.monotonic() - term_sent > 30:
                signal_group(child.pid, signal.SIGKILL)
                if subreaper_active:
                    for dpid, dstart in machine.proc_tree_descendants(my_pid):
                        machine.pidfd_signal(dpid, dstart, signal.SIGKILL)
        # Reap exited children right away so dead members don't hold killpg(pgid, 0) open as zombies.
        if subreaper_active:
            _reap_all_children()

        def _remaining_descendants() -> list[tuple[int, int]]:
            return machine.proc_tree_descendants(my_pid) if subreaper_active else []

        # Whatever the build left running in its process group, adopted subreaper tree (including
        # session-detached env-cleared grandchildren), or BUILDQ_JOB-tagged strays goes too.
        group_alive = signal_group(child.pid, signal.SIGTERM)
        desc = _remaining_descendants()
        for dpid, dstart in desc:
            machine.pidfd_signal(dpid, dstart, signal.SIGTERM)
        strays = machine.tagged_processes(job, exclude=my_pid)
        for pid in strays:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if group_alive or desc or strays:
            stop_by = time.monotonic() + 10
            while time.monotonic() < stop_by:
                if subreaper_active:
                    _reap_all_children()
                still_group = signal_group(child.pid, 0)
                still_desc = _remaining_descendants()
                still_strays = machine.tagged_processes(job, exclude=my_pid)
                if not still_group and not still_desc and not still_strays:
                    break
                pump(0.1)
            # Converging SIGKILL teardown until no adopted descendants or strays survive.
            for _ in range(5):
                if subreaper_active:
                    _reap_all_children()
                still_group = signal_group(child.pid, signal.SIGKILL)
                still_desc = _remaining_descendants()
                for dpid, dstart in still_desc:
                    machine.pidfd_signal(dpid, dstart, signal.SIGKILL)
                still_strays = machine.tagged_processes(job, exclude=my_pid)
                for pid in still_strays:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                if not still_group and not still_desc and not still_strays:
                    break
                time.sleep(0.05)
        if subreaper_active:
            _reap_all_children()
        drain_by = time.monotonic() + 2
        while open_pipes and time.monotonic() < drain_by:
            pump(0.1)
        for r, _ in pipes.values():
            try:
                os.close(r)
            except OSError:
                pass
        returncode = child.returncode
        code = 124 if timed_out else (returncode if returncode >= 0 else 128 - returncode)
        return code
    except Exception as e:  # the result must always be written
        try:
            for output in outputs[:1]:
                output.write(f"\nbuildq: the supervisor failed: {e!r}\n".encode())
        except Exception:
            pass
        return code
    finally:
        for output in outputs:
            try:
                output.finish()
                os.close(output.fd)
            except Exception:
                pass
        if jobserver is not None:
            os.close(jobserver)
        if os.path.exists(os.path.join(directory, "supervisor.json")) and \
                (common.read_json(os.path.join(directory, "supervisor.json")) or {}).get("pid") == os.getpid():
            own = machine.proc_cgroup(os.getpid())
            # Only a cgroup of the build's own says anything about the build: under the direct
            # runner the supervisor shares the daemon's (or a terminal's), with its whole history.
            ticket_id = os.path.basename(os.path.normpath(directory))
            own = own if own and re.fullmatch(rf"buildq-{re.escape(ticket_id)}(-r\d+)?(\.service)?",
                                              os.path.basename(own)) else None
            events = machine.cgroup_events(own) if own else {}
            some, full, high = (machine.cgroup_memory_waits(own) if own else None) or (None, None, None)
            ru_self = resource.getrusage(resource.RUSAGE_SELF)
            ru_ch = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_s = round(ru_self.ru_utime + ru_self.ru_stime + ru_ch.ru_utime + ru_ch.ru_stime, 4)
            try:
                common.write_json(os.path.join(directory, "result.json"),
                           {"exit": code, "ended": time.time(), "timed_out": timed_out,
                            "cpu_s": cpu_s,
                            "oom_kills": events.get("oom_kill", 0),
                            # "oom" counts the times an allocation was about to fail at the build's
                            # own MemoryMax ("max" only counts reclaim there); a kill without it came
                            # from outside (the slice's limit, systemd-oomd).
                            "hit_own_cap": events.get("oom", 0) > 0,
                            # None: not measured (no cgroup of its own)
                            "mem_wait_some": None if some is None else round(some, 1),
                            "mem_wait_full": None if full is None else round(full, 1), "mem_high": high})
            except OSError:
                pass



if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m buildq.supervisor <ticket_dir>")
    sys.exit(supervise(sys.argv[1]))

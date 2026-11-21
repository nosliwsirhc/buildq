# SPDX-License-Identifier: MIT
"""Build supervisor (`buildqd --supervise`): output capture, timeout, and resource stats."""

from __future__ import annotations

import collections
import os
import re
import selectors
import signal
import subprocess
import sys
import time

from . import common, machine
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
        job = str(spec.get("id") or os.path.basename(directory))
        env = common.read_json(claimed)
        try:
            os.remove(claimed)  # the environment can hold secrets; it isn't kept once the build has it
        except OSError:
            pass
        parallel = common.read_json(os.path.join(directory, "parallel.json"))
        if isinstance(env, dict) and isinstance(parallel, dict):
            env.update({str(k): str(v) for k, v in parallel.items()})
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

        def forward(signum, _frame):
            signal_group(child.pid, signum)

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
        while child.poll() is None:
            pump(0.5)
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
            if term_sent and time.monotonic() - term_sent > 30:
                signal_group(child.pid, signal.SIGKILL)
        # Whatever the build left running in its process group goes too, so its turn ends with it.
        if signal_group(child.pid, signal.SIGTERM):
            stop_by = time.monotonic() + 10
            while signal_group(child.pid, 0) and time.monotonic() < stop_by:
                pump(0.2)
            signal_group(child.pid, signal.SIGKILL)
        # And so does anything that left the group (setsid, a double fork): it still carries this
        # build's BUILDQ_JOB. Under the direct runner nothing else would find it.
        strays = machine.tagged_processes(job, exclude=os.getpid())
        if strays:
            for pid in strays:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            stop_by = time.monotonic() + 5
            while machine.tagged_processes(job, exclude=os.getpid()) and time.monotonic() < stop_by:
                pump(0.2)
            for pid in machine.tagged_processes(job, exclude=os.getpid()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
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
            own = own if own and re.fullmatch(rf"buildq-{re.escape(ticket_id)}(-r\d+)?\.service",
                                              os.path.basename(own)) else None
            events = machine.cgroup_events(own) if own else {}
            some, full, high = (machine.cgroup_memory_waits(own) if own else None) or (None, None, None)
            try:
                common.write_json(os.path.join(directory, "result.json"),
                           {"exit": code, "ended": time.time(), "timed_out": timed_out,
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

# SPDX-License-Identifier: MIT
"""GitHub commit status posting with exponential backoff and failure classification."""

from __future__ import annotations

import os
import re
import subprocess

from . import commands, common, machine
from .common import DEVNULL
from .ticket import Ticket

class GitHubMixin:
    """GitHub commit status posting with failure classification and backoff."""

    # ---- telling GitHub what passed

    def note_status(self, t: Ticket, now: float) -> None:
        """With github_status on, a check (see reusable_check) or a named build that passed on a
        commit with no changes on top, its source the same at its start and end, says so on GitHub,
        as a status of that commit, so its pull request shows it was verified here. A later failure of
        the same check on the same commit, the check's own verdict (an exit of 1 to 127; not a kill,
        a timeout or a stop to free memory), turns that status red; other failures aren't posted (a
        local failure on the way to a fix is noise on a pull request). Results are ordered by when
        their builds ended, not when their checks did: an older result never replaces a newer one.
        A commit that isn't on GitHub yet is tried again after the next `git push` run through buildq
        from that repository, and with exponential backoff (10 minutes up to 2 hours) for up to 6
        hours (24 hours for transient/network errors). A build from a GitHub Actions job never
        posts success: the job reports its own result, and on a pull request its commit is usually a
        merge commit the pull request doesn't show. Its failure can still turn a green buildq posted
        on the same commit red."""
        src = t.source or {}
        if t.from_ci and t.state == "succeeded":
            return
        if not self.cfg.github_status or src.get("dirty") or not src.get("remote") or not src.get("commit") \
                or t.source_changed or not t.source_verified or not (t.name or commands.reusable_check(t.argv)) \
                or t.state not in ("succeeded", "failed"):
            return
        if t.state == "failed" and not (isinstance(t.exit, int) and 1 <= t.exit <= 127 and not t.oom
                                        and not t.timed_out and not t.shed_error):
            return
        context = commands.status_context(t.argv, t.cwd, src.get("repo"), t.name, self.cfg.script_labels)
        where = f"{src['remote']}@{src['commit']}@{context}"
        order = [t.ended or now, t.submitted, t.id]  # ties go to the one submitted later
        pending = [p for p in self.status_posts if p.get("where") == where]
        latest = max([p.get("order") or [0, ""] for p in pending] + [self.status_posted.get(where, {}).get("order")
                                                                      or [0, ""]])
        latest = max(latest, self.status_seen.get(where) or [0, ""])
        if order <= latest:
            return  # a result of a build that ended later is already there
        self.status_seen.pop(where, None)
        self.status_seen[where] = order  # newer than anything seen: what came before can't win now
        shown = (max(pending, key=lambda p: p.get("order") or [0, ""])["state"] if pending
                 else self.status_posted.get(where, {}).get("state"))
        if t.state == "failed" and shown != "success":
            self.save_status_posts()
            return  # nothing green to correct, but nothing older may turn it green now either
        in_flight = {id(p) for _, p, _ in self.status_procs}
        for p in pending:  # the newest result replaces older ones not yet sent
            if id(p) not in in_flight:
                self.status_posts.remove(p)
        took = common.fmt_duration((t.ended or now) - (t.started or now))
        firsts = [p["first"] for p in pending if isinstance(p.get("first"), (int, float))]
        nexts = [p["next"] for p in pending if isinstance(p.get("next"), (int, float))]
        post = {
            "repo": src["remote"], "sha": src["commit"], "state": "success" if t.state == "succeeded" else "failure",
            "context": context, "where": where, "order": order,
            "description": (f"passed on the shared build machine in {took}" if t.state == "succeeded" else
                            f"failed (exit {t.exit}) on the shared build machine")[:140],
            "first": min([now, *firsts]), "next": max([now, *nexts]), "ticket": t.id}
        if pending:
            tries = max(int(common.sane_count(p.get("tries")) or 0) for p in pending)
            if tries:
                post["tries"] = tries
            if any(p.get("missing") for p in pending):
                post["missing"] = True
            if any(p.get("logged") for p in pending):
                post["logged"] = True
        self.status_posts.append(post)
        self.save_status_posts()

    def save_status_posts(self) -> None:
        if len(self.status_posted) > 2000:  # (insertion order: the oldest go)
            self.status_posted = dict(list(self.status_posted.items())[-1000:])
        if len(self.status_expired) > 100:
            self.status_expired = self.status_expired[-100:]
        try:
            if len(self.status_seen) > 2000:
                self.status_seen = dict(list(self.status_seen.items())[-1000:])
            common.write_json(os.path.join(self.state_dir, "github-statuses.json"),
                       {"queue": self.status_posts, "posted": self.status_posted, "seen": self.status_seen,
                        "expired": self.status_expired, "stats": self.status_stats})
        except OSError as e:
            common.log(f"can't save the GitHub statuses to send: {e}")

    STATUS_EXPIRED_KEEP = 86400.0  # keep expired missing-commit posts this long so a later push can revive them

    def retry_statuses(self, now: float, repo: str | None = None, *, include_expired: bool = True) -> dict:
        """Schedule pending GitHub statuses for immediate retry (optionally limited to `repo`),
        and revive recently expired missing-commit statuses whose verdicts are still current."""
        nudged = 0
        for post in self.status_posts:
            if repo is not None and post.get("repo") != repo:
                continue
            if repo is None or post.get("missing"):
                post["next"] = now
                post["tries"] = 0
                nudged += 1
        revived = 0
        if include_expired and self.status_expired:
            queued_wheres = {p.get("where") for p in self.status_posts}
            kept = []
            for post in self.status_expired:
                expired_at = post.get("expired_at") or post.get("first") or 0
                if now - expired_at > self.STATUS_EXPIRED_KEEP:
                    continue
                if repo is not None and post.get("repo") != repo:
                    kept.append(post)
                    continue
                where = post.get("where") or f"{post['repo']}@{post['sha']}@{post['context']}"
                order = post.get("order") or [0, ""]
                seen = self.status_seen.get(where) or [0, ""]
                if where in queued_wheres or order < seen:
                    continue
                revived_post = {k: v for k, v in post.items() if k not in ("expired_at", "tries", "logged")}
                revived_post["first"] = now
                revived_post["next"] = now
                self.status_posts.append(revived_post)
                queued_wheres.add(where)
                revived += 1
            self.status_expired = kept
        if nudged or revived:
            self.save_status_posts()
        return {"nudged": nudged, "revived": revived, "pending": len(self.status_posts)}

    def github_status_summary(self) -> dict:
        return {
            "pending": len(self.status_posts),
            "missing": sum(1 for p in self.status_posts if p.get("missing")),
            "revivable": len(self.status_expired),
            "delivered": int(self.status_stats.get("delivered") or 0),
            "expired": int(self.status_stats.get("expired") or 0),
            "failed": int(self.status_stats.get("failed") or 0),
            "last_error": self.status_stats.get("last_error"),
            "last_posted": self.status_stats.get("last_posted"),
        }

    STATUS_RETRY_BASE = 600.0        # first retry wait (10m); doubles each failure up to STATUS_RETRY_MAX

    STATUS_RETRY_MAX = 7200.0        # longest backoff between retries (2h)

    STATUS_MISSING_MAX_AGE = 21600.0  # missing commit (HTTP 422): stop trying after 6h (likely amended/unpushed)

    STATUS_MAX_AGE = 86400.0         # transient/network failures: stop trying after a day

    @staticmethod
    def _status_failure(error: str) -> str:
        """Classify a failed `gh api` status post: 'missing' (commit not on GitHub yet),
        'permanent' (4xx client error that retrying won't fix), or 'transient'."""
        lower = error.lower()
        if "no commit found" in lower:
            return "missing"
        if "rate limit" in lower or "abuse" in lower:
            return "transient"
        if re.search(r"\bHTTP 4(00|01|03|04|22)\b", error):
            return "permanent"
        return "transient"

    def send_status_posts(self, now: float) -> None:
        changed = False
        for entry in list(self.status_procs):
            proc, post, deadline = entry
            code = proc.poll()
            if code is None and now < deadline:
                continue
            if code is None:
                proc.kill()
                proc.wait()
            self.status_procs.remove(entry)
            error = (proc.stderr.read() or b"").decode(errors="replace").strip() if proc.stderr else ""
            if proc.stderr:
                proc.stderr.close()
            newer = any(p is not post and p.get("where") == post.get("where")
                        and (p.get("order") or [0, ""]) > (post.get("order") or [0, ""]) for p in self.status_posts)
            if proc.returncode == 0 or newer:
                if post in self.status_posts:
                    self.status_posts.remove(post)  # sent, or replaced by a newer result for the same check
            if proc.returncode == 0:
                where = post.get("where") or f"{post['repo']}@{post['sha']}@{post['context']}"
                if (post.get("order") or [0, ""]) >= (self.status_posted.get(where, {}).get("order") or [0, ""]):
                    self.status_posted.pop(where, None)
                    self.status_posted[where] = {"state": post["state"], "order": post.get("order") or [0, ""]}
                self.status_stats["delivered"] = int(self.status_stats.get("delivered") or 0) + 1
                self.status_stats["last_posted"] = round(now)
                if newer:
                    # The commit is now on GitHub: give any queued replacement a fresh delivery
                    # window instead of expiring or backing off under the old missing-commit state.
                    for p in self.status_posts:
                        if p.get("where") == post.get("where"):
                            p.pop("missing", None)
                            p.pop("tries", None)
                            p["first"] = now
                            p["next"] = min(p.get("next", now), now)
            else:
                kind = self._status_failure(error)
                err_msg = error[:200] or f"gh exited {proc.returncode}"
                if not newer and kind == "permanent":
                    if post in self.status_posts:
                        self.status_posts.remove(post)
                    self.status_stats["failed"] = int(self.status_stats.get("failed") or 0) + 1
                    self.status_stats["last_error"] = f"{post['repo']}@{post['sha'][:8]}: {err_msg}"
                    common.log(f"GitHub refused the status for {post['repo']}@{post['sha'][:8]} (not retrying): "
                        f"{error[:300] or f'gh exited {proc.returncode}'}", level=4)
                elif kind != "permanent":
                    # If a newer verdict arrived while this request was in flight, carry a missing-commit
                    # or transient/rate-limit failure onto the surviving replacement so the send loop
                    # below doesn't immediately retry the same commit or rate-limited API in this tick.
                    target = (max((p for p in self.status_posts if p.get("where") == post.get("where")),
                                  key=lambda p: p.get("order") or [0, ""], default=None)
                              if newer else post)
                    if target is not None:
                        tries = max(0, int(common.sane_count(target.get("tries")) or 0)) + 1
                        target["tries"] = tries
                        if kind == "missing":
                            target["missing"] = True
                        else:
                            target.pop("missing", None)
                            self.status_stats["last_error"] = f"{target['repo']}@{target['sha'][:8]}: {err_msg}"
                        delay = min(self.STATUS_RETRY_MAX, self.STATUS_RETRY_BASE * (2 ** min(tries - 1, 10)))
                        target["next"] = max(target.get("next", 0), now + delay)
                        if not target.get("logged"):
                            target["logged"] = True
                            span = "6 hours" if target.get("missing") else "a day"
                            common.log(f"GitHub didn't take the status for {target['repo']}@{target['sha'][:8]} "
                                f"(trying again with backoff, for {span}): "
                                f"{error[:300] or f'gh exited {proc.returncode}'}", level=4)
            changed = True
        busy = {id(p) for _, p, _ in self.status_procs}
        for post in list(self.status_posts):
            limit = self.STATUS_MISSING_MAX_AGE if post.get("missing") else self.STATUS_MAX_AGE
            if id(post) not in busy and now - post["first"] > limit:
                why = "it didn't take it" if post.get("logged") else "gh couldn't be run"  # (logged: GitHub said no)
                span = "6 hours" if post.get("missing") else "a day"
                common.log(f"gave up telling GitHub about {post['repo']}@{post['sha'][:8]}: {why} for {span}")
                self.status_posts.remove(post)
                self.status_stats["expired"] = int(self.status_stats.get("expired") or 0) + 1
                if post.get("missing"):
                    expired = dict(post, expired_at=now)
                    self.status_expired.append(expired)
                changed = True
        sending = {p.get("where") for _, p, _ in self.status_procs}
        for post in self.status_posts if self.cfg.github_status else ():
            if len(self.status_procs) >= 2:
                break
            if id(post) in busy or post["next"] > now or post.get("where") in sending:
                continue  # one at a time per commit and check, so they land in order
            if any(p.get("where") == post.get("where") and (p.get("order") or [0, ""]) > (post.get("order") or [0, ""])
                   for p in self.status_posts):
                continue  # a newer result for it is queued
            command = [*(self.gh or [self.cfg.gh or machine.find_gh()]), "api", "--method", "POST", f"repos/{post['repo']}/statuses/{post['sha']}",
                       "-f", f"state={post['state']}", "-f", f"context={post['context']}",
                       "-f", f"description={post['description']}"]
            try:
                proc = subprocess.Popen(command, stdin=DEVNULL, stdout=DEVNULL, stderr=subprocess.PIPE,
                                        start_new_session=True)
            except OSError as e:
                if not self.gh_unrunnable:  # once, not once per result every ten minutes
                    where = (" (it isn't on PATH, in ~/.local/bin or in ~/bin; the gh setting names it)"
                             if command[0] == "gh" else "")
                    common.log(f"can't run gh to tell GitHub a build's result{where}: {e}")
                    self.gh_unrunnable = True
                post["next"] = now + 600
                changed = True
                continue
            self.gh_unrunnable = False
            self.status_procs.append((proc, post, now + 60))
        if changed:
            self.save_status_posts()

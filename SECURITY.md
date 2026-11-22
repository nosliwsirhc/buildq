# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report it privately using GitHub's
[Private Vulnerability Reporting](https://github.com/nosliwsirhc/buildq/security/advisories/new)
(under the repository's **Security → Advisories → Report a vulnerability** tab).

Include as much detail as you can:
- a description of the issue and how to reproduce it;
- the Linux distribution, init system, runner mode (`systemd`, `cgroup`, or `direct`), and Python
  version;
- any suggested fix or mitigation if you have one.

As an open-source project maintained on a best-effort basis, we aim to acknowledge reports promptly
and coordinate a fix before public disclosure.

## Security model and scope

Before reporting, please review buildq's documented trust model in
[docs/design.md#security](docs/design.md#security):

- **The local OS user is the trust boundary.** The daemon runs unprivileged as your user account and
  accepts connections only from the same UID (`SO_PEERCRED`). Any process running as that same user
  can submit, inspect, or cancel builds and read queued environments. If you run untrusted CI
  workloads on the same machine, run them under a separate OS user with its own daemon.
- **Shims and `buildq-rustc-guard` are guardrails, not sandboxes.** They keep well-meaning tools
  and coding agents in the queue; they are not meant to confine adversarial local code.
- **In scope:** privilege escalation (e.g., in `init/cgroup-setup.sh`, init service templates, or
  `install.sh`), symlink/TOCTOU or socket/file-permission bugs that let *another* local user
  interfere with your daemon or steal secrets, command/shell injection in wrappers or templates, or
  secret leakage into history/logs/analytics.

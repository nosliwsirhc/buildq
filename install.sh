#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install buildq for the current user. No root needed.
#
#   ./install.sh                 install or upgrade, then enable and (re)start the daemon
#   ./install.sh --shims         also put cargo, pnpm, npm, npx, yarn, make, ninja and dotnet shims
#                                in ~/.local/share/buildq/shims, which send heavy commands through
#                                the queue; put that directory first on PATH (it says how)
#   ./install.sh --rustc-guard=DIR[,DIR...]
#                                also make buildq-rustc-guard cargo's rustc wrapper for everything
#                                under each DIR (DIR/.cargo/config.toml), so Rust compiles there
#                                outside the queue are refused. Pick the directories your builds run
#                                in: CI runners' work trees and containers then never see it.
#   ./install.sh --rustc-guard   the same for every cargo run by this user ($CARGO_HOME/config.toml),
#                                CI jobs on this machine and containers that mount ~/.cargo included
#   ./install.sh --uninstall     stop and remove it all (keeps ~/.config/buildq and ~/.local/state/buildq)
#
# PREFIX (default ~/.local) sets where the programs go.
set -euo pipefail

prefix=${PREFIX:-$HOME/.local}
bindir=$prefix/bin
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
unitdir=$config_home/systemd/user
confdir=$config_home/buildq
shimdir=${XDG_DATA_HOME:-$HOME/.local/share}/buildq/shims
cargo_config=${CARGO_HOME:-$HOME/.cargo}/config.toml
src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# docker isn't among them: an image build's work runs in dockerd, outside the build's cgroup, so the
# queue can't measure it (see the README).
shim_tools=(cargo pnpm npm npx yarn make gmake ninja dotnet)
guard_list=$confdir/guard-configs  # every cargo config the guard was added to, for --uninstall

say() { printf 'install.sh: %s\n' "$*"; }

# Copies a file into place by renaming a temporary copy over it, so a program running from the
# old file (a shell script reads its file as it goes) keeps the old one intact.
put() {
  local mode=$1 from=$2 to=$3 tmp
  tmp="$to.new.$$"
  install -m "$mode" "$from" "$tmp"
  mv -f "$tmp" "$to"
}

have_systemd() { command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }

# Sets or clears build.rustc-wrapper in a cargo configuration, leaving everything else alone.
rustc_wrapper() {
  python3 - "$cargo_config" "$1" <<'EOF'
import os, re, sys, tomllib
path, wrapper = sys.argv[1], sys.argv[2]
text = open(path).read() if os.path.exists(path) else ""
current = tomllib.loads(text).get("build", {}).get("rustc-wrapper") if text else None
header = re.compile(r"(?m)^\[build\][ \t]*(#[^\n]*)?\n")
if wrapper == "--remove":
    if not (current and current.endswith("buildq-rustc-guard")):
        sys.exit(0)
    new = re.sub(r'(?m)^rustc-wrapper\s*=\s*"[^"]*buildq-rustc-guard"[^\n]*\n', "", text, count=1)
    expected = None
else:
    if current == wrapper:
        sys.exit(0)
    if current:
        sys.exit(f"{path} already sets build.rustc-wrapper = {current!r}; not replacing it")
    line = f'rustc-wrapper = "{wrapper}"  # buildq: refuse Rust compiles outside the build queue\n'
    if header.search(text):
        new = header.sub(lambda m: m.group(0) + line, text, count=1)
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + "[build]\n" + line
    expected = wrapper
# Write only a file that is valid TOML and says exactly what was meant; cargo (and CI) read it.
try:
    if tomllib.loads(new).get("build", {}).get("rustc-wrapper") != expected:
        raise ValueError("the edit didn't come out as meant")
except (tomllib.TOMLDecodeError, ValueError) as e:
    sys.exit(f"can't edit {path} safely ({e}); set build.rustc-wrapper = \"{wrapper}\" there by hand")
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
tmp = f"{path}.buildq.{os.getpid()}"
with open(tmp, "w") as f:
    f.write(new)
if os.path.exists(path):
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)
EOF
}

if [[ "${1:-}" == "--uninstall" ]]; then
  if have_systemd; then
    systemctl --user disable --now buildq.service buildq.socket 2>/dev/null || true
  fi
  keep_guard=no
  configs=("${CARGO_HOME:-$HOME/.cargo}/config.toml")
  [[ -r "$guard_list" ]] && mapfile -t -O 1 configs < "$guard_list"
  for cargo_config in "${configs[@]}"; do
    [[ -e "$cargo_config" ]] || continue
    if ! rustc_wrapper --remove; then
      say "couldn't take the guard out of $cargo_config; keeping $bindir/buildq-rustc-guard so cargo still works"
      keep_guard=yes
    fi
  done
  rm -f "$guard_list"
  rm -rf "$shimdir"
  rm -f "$bindir/buildq" "$bindir/buildqd" "$bindir/buildq-shim" \
        "$unitdir/buildq.service" "$unitdir/buildq.socket" "$unitdir/buildq.slice"
  [[ $keep_guard == no ]] && rm -f "$bindir/buildq-rustc-guard"
  rm -f "$unitdir/buildq.slice.d/50-buildq-memory.conf"
  have_systemd && systemctl --user daemon-reload
  say "removed. Kept $confdir and ${XDG_STATE_HOME:-$HOME/.local/state}/buildq; delete them by hand if you like."
  exit 0
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
  { say "buildq needs Python 3.11 or newer."; exit 1; }
[[ -r /proc/stat && -r /proc/meminfo ]] || { say "buildq needs Linux (/proc/stat, /proc/meminfo)."; exit 1; }

mkdir -p "$bindir" "$unitdir" "$confdir"
for program in buildqd buildq buildq-shim buildq-rustc-guard; do
  put 755 "$src/bin/$program" "$bindir/$program"
done
say "installed buildq, buildqd, buildq-shim and buildq-rustc-guard in $bindir"

if [[ ! -e "$confdir/config.toml" ]]; then
  put 644 "$src/config.example.toml" "$confdir/config.toml"
  say "wrote $confdir/config.toml (all defaults; edit it for your machine)"
fi
"$bindir/buildqd" --check-config --config "$confdir/config.toml" >/dev/null ||
  { say "$confdir/config.toml doesn't load; fix it and run install.sh again."; exit 1; }


for option in "$@"; do
  case $option in --shims | --rustc-guard | --rustc-guard=*) ;; *) say "unknown option $option"; exit 2 ;; esac
done
if ! have_systemd; then
  say "no systemd user session here. Start the daemon yourself ($bindir/buildqd &), then run install.sh again with any options."
  exit 0
fi

sed "s|@BINDIR@|$bindir|g" "$src/systemd/buildq.service" >"$unitdir/buildq.service.new.$$"
mv -f "$unitdir/buildq.service.new.$$" "$unitdir/buildq.service"
put 644 "$src/systemd/buildq.socket" "$unitdir/buildq.socket"
put 644 "$src/systemd/buildq.slice" "$unitdir/buildq.slice"
# A backstop for all builds together: if the estimates are wrong, the kernel slows and then
# stops builds before they can take the machine's memory. Edit or remove it freely.
# (The daemon also lowers these every few seconds to what the machine has free; see slice_guard.)
if [[ ! -e "$unitdir/buildq.slice.d/50-buildq-memory.conf" ]]; then
  mkdir -p "$unitdir/buildq.slice.d"
  printf '[Slice]\nMemoryHigh=75%%\nMemoryMax=85%%\n' >"$unitdir/buildq.slice.d/50-buildq-memory.conf"
  say "capped all builds together at 75%/85% of memory ($unitdir/buildq.slice.d/50-buildq-memory.conf)"
fi
systemctl --user daemon-reload
systemctl --user enable --now buildq.socket >/dev/null
systemctl --user enable buildq.service >/dev/null
# A restart loads the new code. Running builds carry on, and the new daemon picks them up.
systemctl --user restart buildq.service
# A daemon older than this one (Python 3.13+) removed systemd's socket file when it stopped; if the
# file is gone, have systemd create it again.
if [[ ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/buildq.sock" ]]; then
  systemctl --user stop buildq.service
  systemctl --user restart buildq.socket
  systemctl --user start buildq.service
fi
for _ in $(seq 1 40); do
  "$bindir/buildq" ping >/dev/null 2>&1 && break
  sleep 0.5
done
"$bindir/buildq" ping || { say "the daemon isn't answering; not enabling shims or the guard."; exit 1; }
# Shims and the guard only once the daemon answers: without it, they would hold builds up.
for option in "$@"; do
  case $option in
    --shims)
      mkdir -p "$shimdir"
      for tool in "${shim_tools[@]}"; do ln -sfn "$bindir/buildq-shim" "$shimdir/$tool"; done
      say "shims for ${shim_tools[*]} are in $shimdir. Put it first on PATH, at the end of ~/.bashrc:"
      say "    export PATH=\"$shimdir:\$PATH\""
      ;;
    --rustc-guard | --rustc-guard=*)
      targets=()
      if [[ $option == --rustc-guard ]]; then
        targets=("${CARGO_HOME:-$HOME/.cargo}/config.toml")
      else
        IFS=, read -r -a dirs <<<"${option#--rustc-guard=}"
        for dir in "${dirs[@]}"; do targets+=("${dir/#\~/$HOME}/.cargo/config.toml"); done
      fi
      for cargo_config in "${targets[@]}"; do
        rustc_wrapper "$bindir/buildq-rustc-guard" || exit 1
        grep -qxF "$cargo_config" "$guard_list" 2>/dev/null || echo "$cargo_config" >>"$guard_list"
        say "cargo under $(dirname "$(dirname "$cargo_config")") now compiles Rust only inside buildq builds"
      done
      ;;
    *) say "unknown option $option"; exit 2 ;;
  esac
done
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) say "note: $bindir is not on your PATH." ;;
esac

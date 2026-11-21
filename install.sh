#!/bin/sh
# SPDX-License-Identifier: MIT
# Install buildq for the current user. No root needed (unless you let it install missing system
# prerequisites like Python 3.11+).
#
#   ./install.sh                 check system, install or upgrade, and (re)start the daemon
#   ./install.sh --check         check OS compatibility and dependencies without installing
#   ./install.sh --install-deps  install missing prerequisites automatically (uses sudo/doas if needed)
#   ./install.sh --shims         also put cargo, pnpm, npm, npx, yarn, make, gmake, ninja, dotnet
#                                (plus cmake, ctest, zig and gradle when installed on PATH) shims in
#                                ~/.local/share/buildq/shims, which send heavy commands through
#                                the queue; put that directory first on PATH (it says how)
#   ./install.sh --rustc-guard=DIR[,DIR...]
#                                also make buildq-rustc-guard cargo's rustc wrapper for everything
#                                under each DIR (DIR/.cargo/config.toml), so Rust compiles there
#                                outside the queue are refused. Pick the directories your builds run
#                                in: CI runners' work trees and containers then never see it.
#   ./install.sh --rustc-guard   the same for every cargo run by this user ($CARGO_HOME/config.toml),
#                                CI jobs on this machine and containers that mount ~/.cargo included
#   ./install.sh --skill         also give coding agents (Claude Code, Codex, and agents that read
#                                ~/.agents/skills) a "buildq" skill on using the queue, with this
#                                machine's own notes from ~/.config/buildq/agents.md added at its end;
#                                run it again after changing those notes. (The page itself is always
#                                installed: `buildq help agents` prints it.)
#   ./install.sh --no-start      install without starting/restarting the daemon (for images/packaging)
#   ./install.sh --uninstall     stop and remove it all (keeps ~/.config/buildq and ~/.local/state/buildq)
#
# PREFIX (default ~/.local) sets where the programs go. PYTHON (default: auto-detected python3 >= 3.11
# on PATH) sets the interpreter they run with; it is written into them, so a different python3 on
# PATH later doesn't matter.
set -eu

say() { printf 'install.sh: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: ./install.sh [OPTIONS]

Options:
  --check                     Check OS compatibility and dependencies only (no changes)
  --install-deps              Automatically install missing dependencies via your package manager
  --shims                     Install PATH shims for cargo, npm, pnpm, make, ninja, dotnet, etc.
  --rustc-guard[=DIR[,DIR]]   Refuse Rust compiles outside buildq (globally or under given dirs)
  --skill                     Link the buildq skill into detected coding agent directories
  --no-start                  Install without starting or restarting the daemon
  --uninstall                 Stop and remove buildq (keeps config and history)
  -h, --help                  Show this help

Environment:
  PREFIX                      Install prefix (default: ~/.local)
  PYTHON                      Python 3.11+ interpreter to use (default: auto-detected on PATH)
EOF
}

# ---- Option parsing (before touching anything) ---------------------------------------------------
do_uninstall=no
do_check=no
auto_install_deps=no
no_start=no
do_shims=no
do_skill=no
do_rustc_guard_global=no
rustc_guard_dirs=""
has_install_opts=no

for _opt in "$@"; do
  case $_opt in
    -h|--help)
      usage
      exit 0
      ;;
    --uninstall)
      do_uninstall=yes
      ;;
    --check)
      do_check=yes
      ;;
    --install-deps)
      auto_install_deps=yes
      ;;
    --no-start)
      no_start=yes
      has_install_opts=yes
      ;;
    --shims)
      do_shims=yes
      has_install_opts=yes
      ;;
    --skill)
      do_skill=yes
      has_install_opts=yes
      ;;
    --rustc-guard)
      do_rustc_guard_global=yes
      has_install_opts=yes
      ;;
    --rustc-guard=*)
      _val=${_opt#--rustc-guard=}
      [ -n "$_val" ] || { say "--rustc-guard= requires at least one directory"; exit 2; }
      rustc_guard_dirs=${rustc_guard_dirs:+$rustc_guard_dirs,}$_val
      has_install_opts=yes
      ;;
    *)
      say "unknown option $_opt (run ./install.sh --help for usage)"
      exit 2
      ;;
  esac
done

if [ "$do_uninstall" = yes ] && { [ "$do_check" = yes ] || [ "$auto_install_deps" = yes ] || [ "$has_install_opts" = yes ]; }; then
  say "--uninstall cannot be combined with other options"
  exit 2
fi

# ---- Helpers -------------------------------------------------------------------------------------
have_realpath_m=no
if command -v realpath >/dev/null 2>&1 && realpath -m -- / >/dev/null 2>&1; then
  have_realpath_m=yes
fi

normpath() {
  _np=$1
  if [ "$have_realpath_m" = yes ]; then
    realpath -m -- "$_np" 2>/dev/null || printf '%s' "$_np"
    return
  fi
  if command -v realpath >/dev/null 2>&1 && { [ -e "$_np" ] || [ -L "$_np" ]; }; then
    realpath "$_np" 2>/dev/null && return
  fi
  if [ -n "${python:-}" ] && [ -x "${python:-}" ]; then
    "$python" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$_np" 2>/dev/null && return
  fi
  while [ "$_np" != / ]; do
    case $_np in
      */) _np=${_np%/} ;;
      *)  break ;;
    esac
  done
  printf '%s' "$_np"
}

have_systemd() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

# ---- System compatibility & dependency preflight -------------------------------------------------
distro_name=""
distro_id=""
distro_like=""
distro_version=""
env_tags=""
pkg_mgr=""
elevate_cmd=""

detect_os_release() {
  distro_name="Linux"
  distro_id=""
  distro_like=""
  distro_version=""
  if [ -r /etc/os-release ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
      case $_line in
        PRETTY_NAME=*|ID=*|ID_LIKE=*|VERSION_ID=*)
          _k=${_line%%=*}
          _v=${_line#*=}
          # Strip one pair of surrounding quotes without evaluating shell code.
          case $_v in
            \"*\") _v=${_v#\"}; _v=${_v%\"} ;;
            \'*\') _v=${_v#\'}; _v=${_v%\'} ;;
          esac
          case $_k in
            PRETTY_NAME) distro_name=$_v ;;
            ID)          distro_id=$_v ;;
            ID_LIKE)     distro_like=$_v ;;
            VERSION_ID)  distro_version=$_v ;;
          esac
          ;;
      esac
    done < /etc/os-release
  fi
  env_tags=""
  if [ -d /run/WSL ] || { [ -r /proc/version ] && grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }; then
    env_tags="WSL2"
  elif [ -f /.dockerenv ] || [ -f /run/.containerenv ] || [ -n "${container:-}" ]; then
    env_tags="container"
  fi
}

detect_runner_mode() {
  if have_systemd; then
    printf '%s' "systemd user session"
    return
  fi
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    _user=${USER:-$(id -un 2>/dev/null || true)}
    _cg=${BUILDQ_CGROUP_ROOT:-/sys/fs/cgroup/buildq-$_user/user}
    if [ -n "$_user" ] && [ -d "$_cg/builds" ] && [ -w "$_cg/builds" ]; then
      printf '%s' "non-systemd (cgroup v2 runner)"
      return
    fi
  fi
  printf '%s' "non-systemd (direct runner)"
}

check_os_compatible() {
  _os=$(uname -s 2>/dev/null || printf unknown)
  if [ "$_os" != "Linux" ]; then
    case $_os in
      Darwin)
        say "buildq only runs on Linux (detected macOS)."
        say "It relies on Linux's /proc and cgroups to measure and limit builds."
        ;;
      MINGW*|MSYS*|CYGWIN*)
        say "buildq only runs on Linux (detected $_os)."
        say "On Windows, install buildq inside a WSL2 Linux distribution instead."
        ;;
      *)
        say "buildq only runs on Linux (detected $_os)."
        ;;
    esac
    return 1
  fi
  if [ ! -r /proc/stat ] || [ ! -r /proc/meminfo ]; then
    say "buildq needs Linux /proc mounted (/proc/stat and /proc/meminfo are not readable here)."
    return 1
  fi
  detect_os_release
  _mode=$(detect_runner_mode)
  if [ -n "$env_tags" ]; then
    say "detected $distro_name ($env_tags) — $_mode"
  else
    say "detected $distro_name — $_mode"
  fi
  return 0
}

python=""
python_problem=""
python_found_ver=""

# Validate a candidate Python command (path or name on PATH) and set `python` to the resolved
# real interpreter (unwrapping virtualenvs and pyenv/asdf shims, matching kernel #! limits).
resolve_python_candidate() {
  _cand_name=$1
  _cand_path=$(command -v -- "$_cand_name" 2>/dev/null || true)
  if [ -z "$_cand_path" ] || [ ! -x "$_cand_path" ]; then
    python_problem="missing"
    return 1
  fi
  _cand_path=$(CDPATH='' cd -- "$(dirname -- "$_cand_path")" >/dev/null && pwd)/$(basename -- "$_cand_path")
  _ver=$("$_cand_path" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}")' 2>/dev/null || true)
  _chosen=$("$_cand_path" - <<'EOF' 2>/dev/null || true
import os, sys
if sys.version_info < (3, 11):
    sys.exit(1)
if sys.prefix != sys.base_prefix:  # a virtualenv: the Python it was made from
    found = sys._base_executable
    stable = os.path.join(os.path.dirname(found), "python3")
    if stable != found and os.path.realpath(stable) == os.path.realpath(found):
        found = stable  # the same program, by the name that outlives a version upgrade
    print(found)
else:
    print(sys.executable)  # itself, or what a shim ran
EOF
  )
  if [ -z "$_chosen" ]; then
    python_found_ver=$_ver
    if [ -n "$_ver" ]; then
      python_problem="old:$_cand_path:$_ver"
    else
      python_problem="broken:$_cand_path"
    fi
    return 1
  fi
  case $_chosen in
    *[[:space:]]*)
      python_problem="space:$_chosen"
      return 1
      ;;
  esac
  # "#!", the path and the newline must fit the 128-byte first-line buffer of kernels before 5.1.
  if [ "$(printf '%s' "$_chosen" | LC_ALL=C wc -c)" -gt 125 ]; then
    python_problem="long:$_chosen"
    return 1
  fi
  if [ ! -x "$_chosen" ] || ! "$_chosen" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    python_problem="badbase:$_cand_path:$_chosen"
    return 1
  fi
  python=$_chosen
  python_found_ver=$("$python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}")' 2>/dev/null || printf '3.11+')
  python_problem=""
  return 0
}

find_python() {
  python=""
  python_problem=""
  python_found_ver=""
  if [ -n "${PYTHON:-}" ]; then
    resolve_python_candidate "$PYTHON"
    return $?
  fi
  # Prefer python3, then fall back to versioned 3.11+ binaries (helpful on Ubuntu 22.04 / RHEL 9
  # where `python3` may be 3.9/3.10 while `python3.11` or `python3.12` is installed beside it).
  _first_problem=""
  for _pycand in python3 python3.14 python3.13 python3.12 python3.11; do
    if resolve_python_candidate "$_pycand"; then
      return 0
    fi
    if [ -z "$_first_problem" ] || [ "$_first_problem" = "missing" ]; then
      _first_problem=$python_problem
    fi
  done
  python_problem=${_first_problem:-missing}
  return 1
}

detect_pkg_mgr() {
  pkg_mgr=""
  for _pm in apk apt-get dnf pacman xbps-install zypper emerge; do
    if command -v "$_pm" >/dev/null 2>&1; then
      pkg_mgr=$_pm
      break
    fi
  done
  elevate_cmd=""
  if [ "$(id -u)" -eq 0 ]; then
    elevate_cmd=""
  elif command -v sudo >/dev/null 2>&1; then
    elevate_cmd="sudo"
  elif command -v doas >/dev/null 2>&1; then
    elevate_cmd="doas"
  else
    elevate_cmd="NONE"
  fi
}

# Build package list for the detected package manager from abstract dependency tokens.
# Only emits hardcoded package names for known tokens.
pkg_names_for() {
  _pm=$1
  shift
  _out=""
  for _dep in "$@"; do
    _pkg=""
    case $_pm in
      apk)
        case $_dep in
          python)    _pkg="python3" ;;
          flock)     _pkg="util-linux-misc" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      apt-get)
        case $_dep in
          python)
            # On Ubuntu 22.04 or older distros where default python3 is < 3.11, prefer python3.11 if available.
            if command -v python3 >/dev/null 2>&1; then
              if command -v apt-cache >/dev/null 2>&1 && apt-cache show python3.12 >/dev/null 2>&1; then
                _pkg="python3.12"
              elif command -v apt-cache >/dev/null 2>&1 && apt-cache show python3.11 >/dev/null 2>&1; then
                _pkg="python3.11"
              elif [ "$distro_id" = "ubuntu" ] && [ "$distro_version" = "22.04" ]; then
                _pkg="python3.11"
              else
                _pkg="python3"
              fi
            else
              _pkg="python3"
            fi
            ;;
          flock)     _pkg="util-linux" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      dnf)
        case $_dep in
          python)
            if command -v python3 >/dev/null 2>&1; then
              _pkg="python3.11"
            else
              _pkg="python3"
            fi
            ;;
          flock)     _pkg="util-linux" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      pacman)
        case $_dep in
          python)    _pkg="python" ;;
          flock)     _pkg="util-linux" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      xbps-install)
        case $_dep in
          python)    _pkg="python3" ;;
          flock)     _pkg="util-linux" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      zypper)
        case $_dep in
          python)
            if command -v python3 >/dev/null 2>&1; then
              _pkg="python311"
            else
              _pkg="python3"
            fi
            ;;
          flock)     _pkg="util-linux" ;;
          coreutils) _pkg="coreutils" ;;
          awk)       _pkg="gawk" ;;
          sed)       _pkg="sed" ;;
          grep)      _pkg="grep" ;;
          find)      _pkg="findutils" ;;
        esac
        ;;
      emerge)
        case $_dep in
          python)    _pkg="dev-lang/python" ;;
          flock)     _pkg="sys-apps/util-linux" ;;
          coreutils) _pkg="sys-apps/coreutils" ;;
          awk)       _pkg="sys-apps/gawk" ;;
          sed)       _pkg="sys-apps/sed" ;;
          grep)      _pkg="sys-apps/grep" ;;
          find)      _pkg="sys-apps/findutils" ;;
        esac
        ;;
    esac
    [ -n "$_pkg" ] || continue
    case " $_out " in
      *" $_pkg "*) ;;
      *) _out=${_out:+$_out }$_pkg ;;
    esac
  done
  printf '%s' "$_out"
}

format_install_command() {
  _pm=$1
  _pkgs=$2
  _pref=""
  case $elevate_cmd in
    sudo|doas) _pref="$elevate_cmd " ;;
  esac
  case $_pm in
    apk)          printf '%sapk add %s' "$_pref" "$_pkgs" ;;
    apt-get)      printf '%sapt-get update && %sapt-get install -y %s' "$_pref" "$_pref" "$_pkgs" ;;
    dnf)          printf '%sdnf install -y %s' "$_pref" "$_pkgs" ;;
    pacman)       printf '%spacman -S --needed %s' "$_pref" "$_pkgs" ;;
    xbps-install) printf '%sxbps-install -Sy %s' "$_pref" "$_pkgs" ;;
    zypper)       printf '%szypper install -y %s' "$_pref" "$_pkgs" ;;
    emerge)       printf '%semerge --noreplace %s' "$_pref" "$_pkgs" ;;
  esac
}

run_pkg_install() {
  _pm=$1
  _pkgs=$2
  # Word-splitting $_pkgs is intentional: pkg_names_for emits only safe hardcoded package names.
  # shellcheck disable=SC2086
  case $elevate_cmd in
    "")
      case $_pm in
        apk)          apk add $_pkgs ;;
        apt-get)      apt-get update && apt-get install -y $_pkgs ;;
        dnf)          dnf install -y $_pkgs ;;
        pacman)
          if [ "$auto_install_deps" = yes ]; then
            pacman -S --needed --noconfirm $_pkgs
          else
            pacman -S --needed $_pkgs
          fi
          ;;
        xbps-install) xbps-install -Sy $_pkgs ;;
        zypper)       zypper install -y $_pkgs ;;
        emerge)       emerge --noreplace $_pkgs ;;
      esac
      ;;
    sudo|doas)
      case $_pm in
        apk)          "$elevate_cmd" apk add $_pkgs ;;
        apt-get)      "$elevate_cmd" apt-get update && "$elevate_cmd" apt-get install -y $_pkgs ;;
        dnf)          "$elevate_cmd" dnf install -y $_pkgs ;;
        pacman)
          if [ "$auto_install_deps" = yes ]; then
            "$elevate_cmd" pacman -S --needed --noconfirm $_pkgs
          else
            "$elevate_cmd" pacman -S --needed $_pkgs
          fi
          ;;
        xbps-install) "$elevate_cmd" xbps-install -Sy $_pkgs ;;
        zypper)       "$elevate_cmd" zypper install -y $_pkgs ;;
        emerge)       "$elevate_cmd" emerge --noreplace $_pkgs ;;
      esac
      ;;
    *)
      return 1
      ;;
  esac
}

missing_deps=""
missing_desc=""

check_dependencies() {
  missing_deps=""
  missing_desc=""
  if ! find_python; then
    case $python_problem in
      space:*)
        say "the Python to use (${python_problem#space:}) has a space in its path, which a script's first line can't hold; PYTHON=... names another."
        exit 1
        ;;
      long:*)
        say "the Python to use (${python_problem#long:}) has too long a path for a script's first line; PYTHON=... names another."
        exit 1
        ;;
      badbase:*)
        _rest=${python_problem#badbase:}
        say "buildq needs Python 3.11 or newer, and ${_rest%%:*} stands for ${_rest#*:}, which isn't; PYTHON=... names another."
        exit 1
        ;;
    esac
    if [ -n "${PYTHON:-}" ]; then
      case $python_problem in
        old:*)
          _rest=${python_problem#old:}
          say "buildq needs Python 3.11 or newer, and $PYTHON is ${_rest#*:}; set PYTHON=... to a newer interpreter."
          ;;
        *)
          say "buildq needs Python 3.11 or newer, and $PYTHON isn't one that runs; set PYTHON=... to a valid interpreter."
          ;;
      esac
      exit 1
    fi
    missing_deps="python"
    case $python_problem in
      old:*)
        _rest=${python_problem#old:}
        missing_desc="Python 3.11+ (found ${_rest#*:} at ${_rest%%:*})"
        ;;
      *)
        missing_desc="Python 3.11+"
        ;;
    esac
  fi

  if ! command -v flock >/dev/null 2>&1; then
    missing_deps=${missing_deps:+$missing_deps }flock
    missing_desc=${missing_desc:+$missing_desc, }"flock (util-linux)"
  fi
  if ! command -v install >/dev/null 2>&1 || ! command -v mktemp >/dev/null 2>&1; then
    missing_deps=${missing_deps:+$missing_deps }coreutils
    missing_desc=${missing_desc:+$missing_desc, }"install/mktemp (coreutils)"
  fi
  for _u in awk sed grep find; do
    if ! command -v "$_u" >/dev/null 2>&1; then
      missing_deps=${missing_deps:+$missing_deps }$_u
      missing_desc=${missing_desc:+$missing_desc, }$_u
    fi
  done
  [ -z "$missing_deps" ]
}

ensure_dependencies() {
  if check_dependencies; then
    return 0
  fi

  say "missing required prerequisites: $missing_desc"
  detect_pkg_mgr
  _pkgs=""
  if [ -n "$pkg_mgr" ]; then
    # shellcheck disable=SC2086
    _pkgs=$(pkg_names_for "$pkg_mgr" $missing_deps)
  fi

  if [ -z "$pkg_mgr" ] || [ -z "$_pkgs" ]; then
    say "install Python 3.11+ and the missing tools above with your distribution's package manager, then re-run ./install.sh"
    return 1
  fi

  _cmd_display=$(format_install_command "$pkg_mgr" "$_pkgs")

  if [ "$do_check" = yes ]; then
    if [ "$elevate_cmd" = "NONE" ]; then
      say "install them as root with: $_cmd_display"
    else
      say "install them with: $_cmd_display"
      say "or run: ./install.sh --install-deps"
    fi
    return 1
  fi

  if [ "$elevate_cmd" = "NONE" ]; then
    say "no sudo or doas found; install missing prerequisites as root with:"
    say "    su -c '$_cmd_display'"
    return 1
  fi

  _proceed=no
  if [ "$auto_install_deps" = yes ]; then
    say "installing missing prerequisites: $_cmd_display"
    _proceed=yes
  elif [ -t 0 ] && [ -t 1 ]; then
    printf 'install.sh: run `%s` now? [y/N] ' "$_cmd_display"
    IFS= read -r _ans || _ans=""
    case $_ans in
      y|Y|yes|YES|Yes) _proceed=yes ;;
    esac
  else
    say "install them with:"
    say "    $_cmd_display"
    say "(or re-run interactively, or with --install-deps)"
    return 1
  fi

  if [ "$_proceed" != yes ]; then
    say "skipped installing dependencies. Run:"
    say "    $_cmd_display"
    say "then run ./install.sh again."
    return 1
  fi

  if ! run_pkg_install "$pkg_mgr" "$_pkgs"; then
    say "package installation failed; install the prerequisites manually and re-run ./install.sh"
    return 1
  fi

  if ! check_dependencies; then
    say "still missing required prerequisites after install: $missing_desc"
    say "if your distribution's default python3 is older than 3.11, install Python 3.11+ (e.g. via pyenv, uv, or your distro's newer package) and run: PYTHON=/path/to/python3.11 ./install.sh"
    return 1
  fi
  say "prerequisites installed (Python $python_found_ver at $python)"
  return 0
}

# ---- Preflight compatibility & dependency checks (before resolving paths or touching disk) -------
if [ "$do_uninstall" = no ]; then
  check_os_compatible || exit 1
  ensure_dependencies || exit 1
  if [ "$do_check" = yes ]; then
    say "all requirements met (Python $python_found_ver at $python, flock, core utilities) — ready to install!"
    exit 0
  fi
fi

# ---- Paths ---------------------------------------------------------------------------------------
prefix=${PREFIX:-$HOME/.local}
case $prefix in
  '')   say "PREFIX must not be empty"; exit 1 ;;
  *'
'*)     say "PREFIX must not contain newlines"; exit 1 ;;
  '~')  prefix=$HOME ;;
  '~/'*) prefix=$HOME/${prefix#'~/'} ;;
  /*)   ;;
  *)    prefix=$PWD/$prefix ;;
esac
norm=$(normpath "$prefix")
if [ -z "$norm" ] || [ "${norm#/}" = "$norm" ]; then
  say "could not resolve PREFIX ($prefix)"
  exit 1
fi
prefix=$norm
bindir=$prefix/bin
libdir=$prefix/lib/buildq
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
unitdir=$config_home/systemd/user
confdir=$config_home/buildq
shimdir=${XDG_DATA_HOME:-$HOME/.local/share}/buildq/shims
skilldir=${XDG_DATA_HOME:-$HOME/.local/share}/buildq/skill/buildq
cargo_config=${CARGO_HOME:-$HOME/.cargo}/config.toml
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# docker isn't among them: an image build's work runs in dockerd, outside the build's cgroup, so the
# queue can't measure it (see the README).
shim_tools="cargo pnpm npm npx yarn make gmake ninja dotnet"
# Optional compiler shims are linked only when a real binary exists outside $shimdir, so scripts
# using `command -v gradle || ./gradlew` never trip over a shim for an absent tool.
optional_shim_tools="cmake ctest zig gradle"
guard_list=$confdir/guard-configs  # every cargo config the guard was added to, for --uninstall
skill_list=$confdir/skill-links    # every skill link made, for --uninstall

has_real_tool() {
  _name=$1
  _real_shimdir=$(normpath "$shimdir")
  _real_shim=$(normpath "$bindir/buildq-shim")
  _rest=${PATH:-}
  while [ -n "$_rest" ]; do
    case $_rest in
      *:*) _dir=${_rest%%:*}; _rest=${_rest#*:} ;;
      *)   _dir=$_rest; _rest='' ;;
    esac
    [ -n "$_dir" ] || continue
    _real_dir=$(normpath "$_dir")
    [ "$_real_dir" = "$_real_shimdir" ] && continue
    _cand=$_dir/$_name
    if [ -f "$_cand" ] && [ -x "$_cand" ]; then
      [ "$(normpath "$_cand")" = "$_real_shim" ] && continue
      return 0
    fi
  done
  return 1
}

# Serialize concurrent installs/uninstalls against the same physical prefix directory inode
# (consistent across symlinked prefix spellings and regardless of XDG_RUNTIME_DIR).
acquire_lock() {
  command -v flock >/dev/null 2>&1 || { say "flock is required to serialize installs against $prefix"; exit 1; }
  mkdir -p "$prefix"
  exec 9<"$prefix"
  flock 9 || { say "couldn't lock $prefix for install"; exit 1; }
}

# Sets or clears build.rustc-wrapper in a cargo configuration, leaving everything else alone.
rustc_wrapper() {
  "${python:-python3}" - "$cargo_config" "$1" <<'EOF'
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

# ---- Uninstall -----------------------------------------------------------------------------------
if [ "$do_uninstall" = yes ]; then
  acquire_lock
  if have_systemd; then
    systemctl --user disable --now buildq.service buildq.socket 2>/dev/null || true
  elif [ -x "$bindir/buildq" ]; then
    "$bindir/buildq" daemon stop >/dev/null 2>&1 || true
  fi
  find_python || true
  keep_guard=no
  _default_cargo=${CARGO_HOME:-$HOME/.cargo}/config.toml
  if [ -e "$_default_cargo" ]; then
    cargo_config=$_default_cargo
    if ! rustc_wrapper --remove; then
      say "couldn't take the guard out of $cargo_config; keeping $bindir/buildq-rustc-guard so cargo still works"
      keep_guard=yes
    fi
  fi
  if [ -r "$guard_list" ]; then
    while IFS= read -r cargo_config || [ -n "$cargo_config" ]; do
      [ -n "$cargo_config" ] || continue
      [ "$cargo_config" = "$_default_cargo" ] && continue
      [ -e "$cargo_config" ] || continue
      if ! rustc_wrapper --remove; then
        say "couldn't take the guard out of $cargo_config; keeping $bindir/buildq-rustc-guard so cargo still works"
        keep_guard=yes
      fi
    done < "$guard_list"
  fi
  rm -f "$guard_list"
  if [ -r "$skill_list" ]; then
    _tab=$(printf '\t')
    while IFS= read -r _sline || [ -n "$_sline" ]; do
      [ -n "$_sline" ] || continue
      _link=${_sline%%"$_tab"*}
      case $_sline in
        *"$_tab"*) _target=${_sline#*"$_tab"} ;;
        *)         _target=$skilldir ;;
      esac
      [ -n "$_target" ] || _target=$skilldir
      if [ -L "$_link" ] && [ "$(readlink "$_link")" = "$_target" ]; then
        rm -f "$_link"
      fi
      case $_target in */buildq/skill/buildq) rm -rf "$(dirname "$_target")" ;; esac
    done < "$skill_list"
  fi
  rm -f "$skill_list"
  rm -rf "$shimdir" "$(dirname "$skilldir")" "$libdir"
  rm -f "$bindir/buildq" "$bindir/buildqd" "$bindir/buildq-shim" \
        "$unitdir/buildq.service" "$unitdir/buildq.socket" "$unitdir/buildq.slice"
  [ "$keep_guard" = no ] && rm -f "$bindir/buildq-rustc-guard"
  rm -f "$unitdir/buildq.slice.d/40-buildq-oomd.conf" "$unitdir/buildq.slice.d/60-buildq-oomd.conf" \
        "$unitdir/buildq.slice.d/50-buildq-memory.conf"
  have_systemd && systemctl --user daemon-reload
  say "removed. Kept $confdir and ${XDG_STATE_HOME:-$HOME/.local/state}/buildq; delete them by hand if you like."
  exit 0
fi

# ---- Staged atomic install -----------------------------------------------------------------------
acquire_lock
mkdir -p "$bindir" "$libdir" "$unitdir" "$confdir"
staged=""
release=""
publishing=no
prev_current=""
had_current=no

add_temp() {
  [ -n "$staged" ] && printf '%s\n' "$1" >>"$staged/temps"
}

# Copies a file into place by renaming a temporary copy over it, so a program running from the
# old file keeps the old one intact.
put() {
  _mode=$1 _from=$2 _to=$3
  _tmp="$_to.new.$$"
  add_temp "$_tmp"
  install -m "$_mode" "$_from" "$_tmp"
  mv -f "$_tmp" "$_to"
}

# A link to buildq's skill: it points at the skill, or it is one this script made (listed with the
# target it was given, which differs when XDG_DATA_HOME did).
our_link() {
  _link=$1
  [ -L "$_link" ] || return 1
  _target=$(readlink "$_link")
  [ "$_target" = "$skilldir" ] && return 0
  _tab=$(printf '\t')
  [ -r "$skill_list" ] && grep -qxF "${_link}${_tab}${_target}" "$skill_list"
}

# The skill for coding agents, with this machine's notes at its end (`buildq help agents` prints it).
write_skill() {
  mkdir -p "$skilldir"
  add_temp "$skilldir/SKILL.md.new.$$"
  {
    cat "$src/skills/buildq/SKILL.md"
    if [ -r "$confdir/agents.md" ]; then
      printf '\n## On this machine\n\n'
      cat "$confdir/agents.md"
    fi
  } >"$skilldir/SKILL.md.new.$$"
  mv -f "$skilldir/SKILL.md.new.$$" "$skilldir/SKILL.md"
}

# ... and linked where agents look for skills.
install_skill() {
  write_skill
  for _home in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" "${CODEX_HOME:-$HOME/.codex}" "$HOME/.agents"; do
    [ -d "$_home" ] || continue
    _link=$_home/skills/buildq
    if { [ -e "$_link" ] || [ -L "$_link" ]; } && ! our_link "$_link"; then
      say "$_link already exists and isn't buildq's; leaving it"
      continue
    fi
    mkdir -p "$_home/skills"
    ln -sfn "$skilldir" "$_link"
    if [ -r "$skill_list" ]; then  # one line per link, with its current target
      add_temp "$skill_list.new.$$"
      awk -F'\t' -v link="$_link" '$1 != link' "$skill_list" >"$skill_list.new.$$"
      mv -f "$skill_list.new.$$" "$skill_list"
    fi
    printf '%s\t%s\n' "$_link" "$skilldir" >>"$skill_list"
    say "agents reading $_home/skills now have the buildq skill"
  done
  if [ -r "$confdir/agents.md" ]; then
    say "the skill ends with this machine's notes from $confdir/agents.md"
  fi
}

cleanup() {
  if [ "$publishing" = yes ]; then
    if [ "$had_current" = yes ]; then
      ln -sfn "$prev_current" "$libdir/current.rollback.$$" 2>/dev/null &&
        mv -Tf "$libdir/current.rollback.$$" "$libdir/current" 2>/dev/null ||
        rm -f "$libdir/current.rollback.$$" 2>/dev/null || true
    else
      rm -f "$libdir/current" 2>/dev/null || true
    fi
    if [ -n "$staged" ] && [ -d "$staged/prev-bin" ]; then
      for _prog in buildq buildqd buildq-shim buildq-rustc-guard; do
        if [ -L "$staged/prev-bin/$_prog" ] || [ -e "$staged/prev-bin/$_prog" ]; then
          cp -a "$staged/prev-bin/$_prog" "$bindir/$_prog.rollback.$$" 2>/dev/null &&
            mv -Tf "$bindir/$_prog.rollback.$$" "$bindir/$_prog" 2>/dev/null ||
            rm -f "$bindir/$_prog.rollback.$$" 2>/dev/null || true
        else
          rm -f "$bindir/$_prog" 2>/dev/null || true
        fi
      done
    fi
  fi
  if [ -n "$staged" ] && [ -r "$staged/temps" ]; then
    while IFS= read -r _tf || [ -n "$_tf" ]; do
      [ -n "$_tf" ] && rm -f -- "$_tf" 2>/dev/null || true
    done < "$staged/temps"
  fi
  if [ -n "$release" ] && [ -e "$release" ]; then
    if [ ! -e "$libdir/current" ] || [ "$(normpath "$release")" != "$(normpath "$libdir/current")" ]; then
      rm -rf "$release" 2>/dev/null || true
    fi
  fi
  [ -n "$staged" ] && rm -rf "$staged" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

staged=$(mktemp -d "${TMPDIR:-/tmp}/buildq-install.XXXXXX")
# Stage and validate the versioned package release, generated launchers, and helper scripts
# (including destination-side temps) before the first live rename, so any error rolls back cleanly.
# Earlier releases stay in place until `--uninstall` so already-started processes keep working.
release=$(mktemp -d "$libdir/v-XXXXXX")
cp -R "$src/src/buildq" "$release/buildq"
find "$release/buildq" -type d -name '__pycache__' -prune -exec rm -rf {} +
"$python" -B -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import buildq.daemon, buildq.client' "$release"
"$python" - "$src/bin/buildq" "$staged/buildq" "$python" "$libdir/current" <<'EOF'
import sys
src_path, dst_path, py, pkg_lib = sys.argv[1:5]
lines = open(src_path).read().splitlines(keepends=True)
lines[0] = f"#!{py}\n"
text = "".join(lines)
if "LIBDIR = None" not in text:
    sys.exit(f"expected LIBDIR placeholder in {src_path}")
with open(dst_path, "w") as f:
    f.write(text.replace("LIBDIR = None", f"LIBDIR = {pkg_lib!r}", 1))
EOF
# buildq-shim stays standalone (zero package-import overhead on every shimmed command).
{ printf '#!%s\n' "$python"; tail -n +2 "$src/bin/buildq-shim"; } >"$staged/buildq-shim"
cp "$src/bin/buildq-rustc-guard" "$staged/buildq-rustc-guard"
"$python" -B -I -m py_compile "$staged/buildq" "$staged/buildq-shim"
/bin/sh -n "$staged/buildq-rustc-guard"
# Prepare every destination temp on the target filesystems before publishing anything.
for _tmp in "$libdir/current.new.$$" "$bindir/buildq.new.$$" "$bindir/buildqd.new.$$" \
            "$bindir/buildq-shim.new.$$" "$bindir/buildq-rustc-guard.new.$$"; do
  add_temp "$_tmp"
done
ln -sfn "$release" "$libdir/current.new.$$"
install -m 755 "$staged/buildq" "$bindir/buildq.new.$$"
ln -sfn buildq "$bindir/buildqd.new.$$"
install -m 755 "$staged/buildq-shim" "$bindir/buildq-shim.new.$$"
install -m 755 "$staged/buildq-rustc-guard" "$bindir/buildq-rustc-guard.new.$$"
# Snapshot previous live targets for rollback if any live rename fails.
if [ -L "$libdir/current" ]; then
  had_current=yes
  prev_current=$(readlink "$libdir/current")
fi
mkdir -p "$staged/prev-bin"
for _prog in buildq buildqd buildq-shim buildq-rustc-guard; do
  if [ -L "$bindir/$_prog" ] || [ -e "$bindir/$_prog" ]; then
    cp -a "$bindir/$_prog" "$staged/prev-bin/$_prog"
  fi
done
publishing=yes
mv -Tf "$libdir/current.new.$$" "$libdir/current"
mv -Tf "$bindir/buildq.new.$$" "$bindir/buildq"
mv -Tf "$bindir/buildqd.new.$$" "$bindir/buildqd"
mv -Tf "$bindir/buildq-shim.new.$$" "$bindir/buildq-shim"
mv -Tf "$bindir/buildq-rustc-guard.new.$$" "$bindir/buildq-rustc-guard"
publishing=no
say "installed buildq, buildqd, buildq-shim and buildq-rustc-guard in $bindir (Python $python_found_ver: $python)"

if [ ! -e "$confdir/config.toml" ]; then
  put 644 "$src/config.example.toml" "$confdir/config.toml"
  say "wrote $confdir/config.toml (all defaults; edit it for your machine)"
fi
"$bindir/buildqd" --check-config --config "$confdir/config.toml" >/dev/null ||
  { say "$confdir/config.toml doesn't load; fix it and run install.sh again."; exit 1; }

if [ "$do_skill" = yes ]; then
  install_skill
else
  write_skill
fi

if have_systemd; then
  add_temp "$unitdir/buildq.service.new.$$"
  sed "s|@BINDIR@|$bindir|g" "$src/systemd/buildq.service" >"$unitdir/buildq.service.new.$$"
  mv -f "$unitdir/buildq.service.new.$$" "$unitdir/buildq.service"
  put 644 "$src/systemd/buildq.socket" "$unitdir/buildq.socket"
  put 644 "$src/systemd/buildq.slice" "$unitdir/buildq.slice"
  # A backstop for all builds together: if the estimates are wrong, the kernel slows and then
  # stops builds before they can take the machine's memory. Edit or remove it freely.
  # (The daemon also lowers these every few seconds to what the machine has free; see slice_guard.)
  if [ ! -e "$unitdir/buildq.slice.d/50-buildq-memory.conf" ]; then
    mkdir -p "$unitdir/buildq.slice.d"
    printf '[Slice]\nMemoryHigh=75%%\nMemoryMax=85%%\n' >"$unitdir/buildq.slice.d/50-buildq-memory.conf"
    say "capped all builds together at 75%/85% of memory ($unitdir/buildq.slice.d/50-buildq-memory.conf)"
  fi
  # Distros such as Fedora set ManagedOOMMemoryPressure=kill on every user slice, so a single build
  # slowed at its own MemoryHigh can make systemd-oomd kill it even while plenty of RAM is free.
  # buildq enforces per-build MemoryMax, slice_guard and shed_memory itself; this user-range drop-in
  # disables pressure monitoring rooted at buildq.slice while leaving ancestor/system oomd intact.
  if [ ! -e "$unitdir/buildq.slice.d/60-buildq-oomd.conf" ]; then
    mkdir -p "$unitdir/buildq.slice.d"
    printf '[Slice]\nManagedOOMMemoryPressure=auto\nManagedOOMMemoryPressureLimit=\n' >"$unitdir/buildq.slice.d/60-buildq-oomd.conf"
    say "disabled per-slice systemd-oomd pressure kills on buildq.slice ($unitdir/buildq.slice.d/60-buildq-oomd.conf)"
  fi
  systemctl --user daemon-reload
  if [ "$no_start" = no ]; then
    systemctl --user enable --now buildq.socket >/dev/null
    systemctl --user enable buildq.service >/dev/null
    # A restart loads the new code. Running builds carry on, and the new daemon picks them up.
    systemctl --user restart buildq.service
    # A daemon older than this one (Python 3.13+) removed systemd's socket file when it stopped; if the
    # file is gone, have systemd create it again.
    if [ ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/buildq.sock" ]; then
      systemctl --user stop buildq.service
      systemctl --user restart buildq.socket
      systemctl --user start buildq.service
    fi
    _i=0
    while [ "$_i" -lt 40 ]; do
      "$bindir/buildq" ping >/dev/null 2>&1 && break
      sleep 0.5 2>/dev/null || sleep 1
      _i=$((_i + 1))
    done
    "$bindir/buildq" ping || { say "the daemon isn't answering; not enabling shims or the guard."; exit 1; }
  else
    say "installed systemd user units (--no-start: skipped enabling/restarting buildq.service)"
  fi
else
  if [ "$no_start" = no ]; then
    say "no systemd user session here; starting buildqd as a user background daemon"
    "$bindir/buildq" daemon restart || { say "couldn't start buildqd; not enabling shims or the guard."; exit 1; }
    "$bindir/buildq" ping || { say "the daemon isn't answering; not enabling shims or the guard."; exit 1; }
    say "manage it with: buildq daemon status|restart|reload|stop|logs"
    say "for boot-time supervision (OpenRC, runit, s6, dinit, sysvinit) or optional cgroup v2 limits, see $src/init/README.md"
  else
    say "no systemd user session here (--no-start: skipped starting buildqd; start it with \`buildq daemon start\`)"
  fi
fi

# Shims and the guard only once the daemon answers (or explicitly with --no-start for image builds).
if [ "$do_shims" = yes ]; then
  mkdir -p "$shimdir"
  installed_shims="$shim_tools"
  for _tool in $shim_tools; do
    ln -sfn "$bindir/buildq-shim" "$shimdir/$_tool"
  done
  for _tool in $optional_shim_tools; do
    if has_real_tool "$_tool"; then
      ln -sfn "$bindir/buildq-shim" "$shimdir/$_tool"
      installed_shims="$installed_shims $_tool"
    elif [ -L "$shimdir/$_tool" ]; then
      rm -f "$shimdir/$_tool"
    fi
  done
  say "shims for $installed_shims are in $shimdir. Put it first on PATH, at the end of ~/.bashrc:"
  say "    export PATH=\"$shimdir:\$PATH\""
fi

apply_rustc_guard() {
  cargo_config=$1
  rustc_wrapper "$bindir/buildq-rustc-guard" || exit 1
  grep -qxF "$cargo_config" "$guard_list" 2>/dev/null || printf '%s\n' "$cargo_config" >>"$guard_list"
  say "cargo under $(dirname "$(dirname "$cargo_config")") now compiles Rust only inside buildq builds"
}

if [ "$do_rustc_guard_global" = yes ]; then
  apply_rustc_guard "${CARGO_HOME:-$HOME/.cargo}/config.toml"
fi
if [ -n "$rustc_guard_dirs" ]; then
  _rem=$rustc_guard_dirs
  while [ -n "$_rem" ]; do
    case $_rem in
      *,*) _dir=${_rem%%,*}; _rem=${_rem#*,} ;;
      *)   _dir=$_rem; _rem='' ;;
    esac
    [ -n "$_dir" ] || continue
    case $_dir in
      '~')   _dir=$HOME ;;
      '~/'*) _dir=$HOME/${_dir#'~/'} ;;
    esac
    apply_rustc_guard "$_dir/.cargo/config.toml"
  done
fi

case ":${PATH:-}:" in
  *":$bindir:"*) ;;
  *) say "note: $bindir is not on your PATH. Add: export PATH=\"$bindir:\$PATH\"" ;;
esac
if [ "$no_start" = no ]; then
  say "ready! Try: buildq status"
fi

#!/bin/sh
set -eu
umask 077
export PYTHONDONTWRITEBYTECODE=1
borg_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
borg_home=${BORG_HOME:-$HOME/.borg}
borg_has_blueprint=false
borg_value_option=
for borg_arg in "$@"; do
  # Reject unknown/abbreviated options and missing values before any bootstrap
  # writes. Python's install parser uses the same exact long-option contract.
  if [ -n "$borg_value_option" ]; then
    case "$borg_arg" in --*) echo "$borg_value_option requires a value" >&2; exit 2 ;; esac
    if [ "$borg_value_option" = --home ]; then borg_home=$borg_arg; fi
    borg_value_option=
    continue
  fi
  case "$borg_arg" in
    --home|--owner|--port-base|--projects|--blueprint|--machine) borg_value_option=$borg_arg ;;
    --home=*|--owner=*|--port-base=*|--projects=*|--blueprint=*|--machine=*) ;;
    --system-dependencies|--no-start|--help|-h) ;;
    *) echo "Unknown installer option; use ./install.sh --help" >&2; exit 2 ;;
  esac
  case "$borg_arg" in --blueprint|--blueprint=*|--machine|--machine=*) borg_has_blueprint=true ;; esac
  case "$borg_arg" in --help|-h)
    echo 'Usage: ./install.sh [--home ABSOLUTE_PATH] [--owner OWNER] [--port-base PORT] [--blueprint FILE --machine ID] [--no-start]'
    echo 'Installs an independent BORG. Provider sign-in and optional external access use your own accounts.'
    exit 0 ;;
  esac
  case "$borg_arg" in --home=*) borg_home=${borg_arg#--home=} ;; esac
done
if [ -n "$borg_value_option" ]; then echo "$borg_value_option requires a value" >&2; exit 2; fi
case "$borg_home" in /*) ;; *) echo "BORG home must be an absolute path" >&2; exit 2 ;; esac
case "$borg_home" in */../*|*/./*|*/..|*/.) echo "Use a canonical BORG home" >&2; exit 2 ;; esac
borg_check=$borg_home
while [ "$borg_check" != / ]; do
  if [ -L "$borg_check" ]; then echo "BORG home must not contain symlinks" >&2; exit 2; fi
  borg_check=$(dirname -- "$borg_check")
done
if [ -e "$borg_home" ]; then
  if [ ! -d "$borg_home" ] || [ ! -O "$borg_home" ]; then
    echo "BORG home must be a directory owned by the current user" >&2; exit 2
  fi
  case "$(uname -s)" in
    Darwin) borg_home_mode=$(stat -f '%Lp' "$borg_home") ;;
    *) borg_home_mode=$(stat -c '%a' "$borg_home") ;;
  esac
  if [ "$borg_home_mode" != 700 ]; then
    echo "BORG home must have mode 0700 before setup" >&2; exit 2
  fi
fi
# Check managed roots before any bootstrap command can follow an old redirect.
while IFS= read -r borg_relative; do
  case "$borg_relative" in ""|/*|*..*) echo "Invalid managed-directory contract" >&2; exit 2 ;; esac
  borg_managed="$borg_home/$borg_relative"
  if [ -L "$borg_managed" ]; then echo "Managed BORG directories cannot be symlinks" >&2; exit 2; fi
  if [ -e "$borg_managed" ]; then
    if [ ! -d "$borg_managed" ] || [ ! -O "$borg_managed" ]; then
      echo "Managed BORG directories must belong to this user" >&2; exit 2
    fi
    case "$(uname -s)" in
      Darwin) borg_mode=$(stat -f '%Lp' "$borg_managed") ;;
      *) borg_mode=$(stat -c '%a' "$borg_managed") ;;
    esac
    if [ "$((0$borg_mode & 022))" != 0 ]; then echo "Managed directory is writable by another user" >&2; exit 2; fi
  fi
done < "$borg_script_dir/installer/managed-directories.txt"
if [ -L "$borg_home/config.json" ]; then echo "BORG config cannot be a symlink" >&2; exit 2; fi
# Blueprint imports must fail before bootstrap creates a home or downloads files.
# A host Python is used only for stdlib validation, never as the installed runtime.
if "$borg_has_blueprint"; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Blueprint validation requires Python 3 on PATH before bootstrap" >&2; exit 2
  fi
  PYTHONPATH= PYTHONHOME= python3 -B "$borg_script_dir/borg.py" install --validate-only --home "$borg_home" "$@"
fi
# An established installation reaches read-only source preflight before downloads.
if [ -f "$borg_home/config.json" ] && [ -x "$borg_home/mem0/venv/bin/python" ]; then
  borg_existing_python="$borg_home/mem0/venv/bin/python"
  borg_resolved_python=$borg_existing_python
  borg_links=0
  while [ -L "$borg_resolved_python" ]; do
    borg_links=$((borg_links + 1))
    if [ "$borg_links" -gt 40 ]; then echo "Python link chain is invalid" >&2; exit 2; fi
    borg_link=$(readlink "$borg_resolved_python")
    case "$borg_link" in
      /*) borg_resolved_python=$borg_link ;;
      *) borg_resolved_python="$(dirname -- "$borg_resolved_python")/$borg_link" ;;
    esac
  done
  borg_resolved_python="$(CDPATH= cd -P -- "$(dirname -- "$borg_resolved_python")" && pwd)/$(basename -- "$borg_resolved_python")"
  case "$borg_resolved_python" in "$borg_home/runtime/python/"*) ;; *) echo "Python must belong to this BORG runtime" >&2; exit 2 ;; esac
  if [ ! -O "$borg_resolved_python" ]; then echo "Python must belong to this user" >&2; exit 2; fi
  unset PYTHONHOME PYTHONPATH
  exec "$borg_existing_python" -B "$borg_script_dir/installer/bootstrap.py" "$borg_existing_python" -B "$borg_script_dir/borg.py" install --home "$borg_home" "$@"
fi
if [ -e "$borg_home/app" ]; then
  echo "Existing application has no managed Python; preserve it and repair the runtime before setup" >&2
  exit 2
fi
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)
    borg_uv_url=https://github.com/astral-sh/uv/releases/download/0.12.13/uv-aarch64-apple-darwin.tar.gz
    borg_uv_directory=uv-aarch64-apple-darwin
    borg_uv_sha=7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed
    ;;
  Darwin-x86_64)
    borg_uv_url=https://github.com/astral-sh/uv/releases/download/0.12.13/uv-x86_64-apple-darwin.tar.gz
    borg_uv_directory=uv-x86_64-apple-darwin
    borg_uv_sha=5e287ef61cb6a9b61b3a83fef124fd143e400468a7dac794230147a810e17119
    ;;
  Linux-aarch64)
    borg_uv_url=https://github.com/astral-sh/uv/releases/download/0.12.13/uv-aarch64-unknown-linux-gnu.tar.gz
    borg_uv_directory=uv-aarch64-unknown-linux-gnu
    borg_uv_sha=2eaa5d94f5db7b3a1a092156b9420459e42ab0217d917fe74a876309cef9b5e9
    ;;
  Linux-x86_64)
    borg_uv_url=https://github.com/astral-sh/uv/releases/download/0.12.13/uv-x86_64-unknown-linux-gnu.tar.gz
    borg_uv_directory=uv-x86_64-unknown-linux-gnu
    borg_uv_sha=745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd
    ;;
  *) echo "Supported hosts: macOS or Linux, ARM64 or x86-64" >&2; exit 2 ;;
esac
# Forward cancellation during bootstrap commands, before Python is available.
borg_cancel() {
  borg_cancel_status=$1
  trap '' INT TERM HUP
  kill -TERM "$borg_child_pid" 2>/dev/null || true
  borg_waits=0
  while kill -0 "$borg_child_pid" 2>/dev/null && [ "$borg_waits" -lt 20 ]; do
    sleep 1
    borg_waits=$((borg_waits + 1))
  done
  kill -KILL "$borg_child_pid" 2>/dev/null || true
  wait "$borg_child_pid" 2>/dev/null || true
  exit "$borg_cancel_status"
}
borg_run() {
  "$@" &
  borg_child_pid=$!
  trap 'borg_cancel 130' INT
  trap 'borg_cancel 143' TERM HUP
  wait "$borg_child_pid"
}
mkdir -p "$borg_home/bootstrap" "$borg_home/runtime" "$borg_home/cache"
borg_uv_archive="$borg_home/bootstrap/uv.tar.gz"
if [ -L "$borg_uv_archive" ] || [ -L "$borg_uv_archive.partial" ]; then
  echo "Bootstrap archives cannot be symlinks" >&2; exit 2
fi
if [ -e "$borg_uv_archive.partial" ]; then
  echo "An interrupted bootstrap download needs inspection: $borg_uv_archive.partial" >&2; exit 2
fi
if [ -e "$borg_uv_archive" ] && { [ ! -f "$borg_uv_archive" ] || [ ! -O "$borg_uv_archive" ]; }; then
  echo "Bootstrap archive must be an owned regular file" >&2; exit 2
fi
if [ ! -f "$borg_uv_archive" ]; then
  borg_run curl --connect-timeout 15 --max-time 300 --fail --location --proto "=https" --tlsv1.2 --output "$borg_uv_archive.partial" "$borg_uv_url"
  mv "$borg_uv_archive.partial" "$borg_uv_archive"
fi
if command -v sha256sum >/dev/null 2>&1; then
  borg_uv_actual=$(sha256sum "$borg_uv_archive" | cut -d " " -f 1)
else
  borg_uv_actual=$(shasum -a 256 "$borg_uv_archive" | cut -d " " -f 1)
fi
if [ "$borg_uv_actual" != "$borg_uv_sha" ]; then
  echo "uv checksum mismatch; no downloaded program was executed" >&2; exit 1
fi
borg_bootstrap=$(mktemp -d "$borg_home/bootstrap/verified-uv.XXXXXX")
trap 'rm -rf -- "$borg_bootstrap"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
borg_run tar -xzf "$borg_uv_archive" -C "$borg_bootstrap"
borg_uv="$borg_bootstrap/$borg_uv_directory/uv"
test -n "$borg_uv"
export UV_PYTHON_INSTALL_DIR="$borg_home/runtime/python"
export UV_CACHE_DIR="$borg_home/cache/uv"
export UV_NO_MODIFY_PATH=1
borg_run "$borg_uv" python install --no-bin 3.12.12
borg_python=$("$borg_uv" python find --managed-python 3.12.12)
export BORG_BOOTSTRAP_UV="$borg_uv"
unset PYTHONHOME PYTHONPATH
borg_run "$borg_python" -B "$borg_script_dir/installer/bootstrap.py" "$borg_python" -B "$borg_script_dir/borg.py" install --home "$borg_home" "$@"

#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Sunshine Jetson fork, 2026-10-20.
set -euo pipefail
if (( EUID == 0 )); then
  echo "请用桌面登录用户运行，不要 sudo bash；脚本会在需要时调用 sudo。" >&2
  exit 1
fi
repo=https://github.com/jdluuu/sunshine-jetson.git
ref=v0.20.0-jetson.1
if ! command -v git >/dev/null || ! command -v python3 >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends git python3 ca-certificates
fi
cache="${XDG_CACHE_HOME:-$HOME/.cache}/sunshine-jetson"
mkdir -p "$cache"
checkout="$cache/source-$ref"
if [[ ! -d "$checkout" ]]; then
  git clone --depth 1 --branch "$ref" "$repo" "$checkout"
fi
if [[ "$(git -C "$checkout" remote get-url origin)" != "$repo" ]] ||
   [[ "$(git -C "$checkout" describe --tags --exact-match HEAD)" != "$ref" ]] ||
   [[ -n "$(git -C "$checkout" status --porcelain --untracked-files=no)" ]]; then
  echo "缓存源码与发布版本不符，请保留本地改动并使用一个新的 XDG_CACHE_HOME 重试。" >&2
  exit 1
fi
exec python3 "$checkout/scripts/jetson/install.py" "$@"

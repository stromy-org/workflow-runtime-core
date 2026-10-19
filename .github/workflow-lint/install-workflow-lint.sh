#!/usr/bin/env bash
# ci-inputs:
# Installs exact analyzer releases into an owned directory; never a floating installer.
set -euo pipefail
tools_dir=${1:?usage: install-workflow-lint.sh tools-directory}
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)
    actionlint_asset=linux_amd64
    actionlint_sha=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
    shellcheck_asset=linux.x86_64
    shellcheck_sha=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198 ;;
  Linux/aarch64|Linux/arm64)
    actionlint_asset=linux_arm64
    actionlint_sha=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
    shellcheck_asset=linux.aarch64
    shellcheck_sha=12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588 ;;
  Darwin/arm64)
    actionlint_asset=darwin_arm64
    actionlint_sha=aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f
    shellcheck_asset=darwin.aarch64
    shellcheck_sha=56affdd8de5527894dca6dc3d7e0a99a873b0f004d7aabc30ae407d3f48b0a79 ;;
  Darwin/x86_64)
    actionlint_asset=darwin_amd64
    actionlint_sha=5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644
    shellcheck_asset=darwin.x86_64
    shellcheck_sha=3c89db4edcab7cf1c27bff178882e0f6f27f7afdf54e859fa041fca10febe4c6 ;;
  *) echo 'Pinned analyzer binaries are unavailable for this runner platform.' >&2; exit 3 ;;
esac
mkdir -p "$tools_dir"
tools_dir=$(cd "$tools_dir" && pwd)

verify_archive() {
  local actual
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$1")
  else
    actual=$(shasum -a 256 "$1")
  fi
  if [[ ${actual%% *} != "$2" ]]; then
    echo 'Analyzer archive checksum mismatch; refusing extraction.' >&2
    return 1
  fi
}
download_dir=$(mktemp -d)
trap 'rm -f "$download_dir/actionlint.tar.gz" "$download_dir/shellcheck.tar.xz"; rmdir "$download_dir"' EXIT

curl --fail --silent --show-error --location --connect-timeout 10 --max-time 45 --max-filesize 20000000 \
  "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_${actionlint_asset}.tar.gz" \
  --output "$download_dir/actionlint.tar.gz"
verify_archive "$download_dir/actionlint.tar.gz" "$actionlint_sha"
tar -xzf "$download_dir/actionlint.tar.gz" -C "$tools_dir" actionlint

curl --fail --silent --show-error --location --connect-timeout 10 --max-time 45 --max-filesize 20000000 \
  "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.${shellcheck_asset}.tar.xz" \
  --output "$download_dir/shellcheck.tar.xz"
verify_archive "$download_dir/shellcheck.tar.xz" "$shellcheck_sha"
tar -xJf "$download_dir/shellcheck.tar.xz" -C "$tools_dir" --strip-components=1 shellcheck-v0.11.0/shellcheck

# uv resolves the package's pinned wheel once; it neither calls GitHub nor installs
# into the consumer project. Keep this installed executable beside the other tools.
env UV_TOOL_DIR="$tools_dir/uv-tools" UV_TOOL_BIN_DIR="$tools_dir" uv tool install 'zizmor==1.30.1'
"$tools_dir/actionlint" -version
"$tools_dir/shellcheck" --version
"$tools_dir/zizmor" --version

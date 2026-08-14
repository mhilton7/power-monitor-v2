#!/usr/bin/env bash
set -Eeuo pipefail

readonly version="1.7.12"
readonly temporary_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/pm-actionlint.XXXXXX")"

case "$(uname -s)" in
  Linux)
    readonly archive="actionlint_${version}_linux_amd64.tar.gz"
    readonly expected_sha256="8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
    readonly executable="${temporary_dir}/actionlint"
    ;;
  MINGW*|MSYS*)
    readonly archive="actionlint_${version}_windows_amd64.zip"
    readonly expected_sha256="6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"
    readonly executable="${temporary_dir}/actionlint.exe"
    ;;
  *)
    printf 'unsupported actionlint validation host: %s\n' "$(uname -s)" >&2
    exit 2
    ;;
esac

cleanup() {
  [[ "$temporary_dir" == */pm-actionlint.* ]]
  rm -rf -- "$temporary_dir"
}
trap cleanup EXIT

curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location \
  --max-time 120 --retry 3 --retry-all-errors \
  "https://github.com/rhysd/actionlint/releases/download/v${version}/${archive}" \
  --output "${temporary_dir}/${archive}"
printf '%s  %s\n' "$expected_sha256" "${temporary_dir}/${archive}" | sha256sum --check --strict
if [[ "$archive" == *.zip ]]; then
  unzip -q "${temporary_dir}/${archive}" actionlint.exe -d "$temporary_dir"
else
  tar --extract --gzip --file "${temporary_dir}/${archive}" --directory "$temporary_dir" actionlint
fi
"$executable"

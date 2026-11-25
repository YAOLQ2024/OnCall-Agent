#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
launcher=$(wslpath -w "$project_dir/scripts/start-cloud.ps1")
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$launcher"

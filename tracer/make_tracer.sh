#!/usr/bin/env bash
set -euo pipefail
: "${PIN_ROOT:?Set PIN_ROOT to the extracted Intel Pin directory}"
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p obj-intel64
make PIN_ROOT="$PIN_ROOT" obj-intel64/champsim_tracer.so

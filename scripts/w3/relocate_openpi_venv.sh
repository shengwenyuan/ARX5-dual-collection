#!/usr/bin/env bash
set -euo pipefail

PI05_W3_ROOT="${PI05_W3_ROOT:-/home/lenovo/swy/pi05-runtime}"
OPENPI_ROOT="${PI05_W3_ROOT}/workspace/openpi"
PYTHON_ROOT="${PI05_W3_ROOT}/runtime/python/cpython-3.11.15-linux-x86_64-gnu"
VENV_ROOT="${OPENPI_ROOT}/.venv"

test -x "${PYTHON_ROOT}/bin/python3.11"
test -d "${VENV_ROOT}/bin"

ln -sfn "${PYTHON_ROOT}/bin/python3.11" "${VENV_ROOT}/bin/python"

while IFS= read -r -d '' file; do
  sed -i \
    -e "s#/workspace/openpi#${OPENPI_ROOT}#g" \
    -e "s#/root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu#${PYTHON_ROOT}#g" \
    "${file}"
done < <(
  find "${VENV_ROOT}/bin" "${VENV_ROOT}/lib/python3.11/site-packages" \
    -maxdepth 2 -type f -print0 \
    | xargs -0 grep -IlZ \
        -e '/workspace/openpi' \
        -e '/root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu' \
    || true
)

sed -i "s#^home = .*#home = ${PYTHON_ROOT}/bin#" "${VENV_ROOT}/pyvenv.cfg"
"${VENV_ROOT}/bin/python" --version

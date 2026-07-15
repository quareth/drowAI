# syntax=docker/dockerfile:1.7
# Canonical DrowAI Kali runtime image: Kali base + execution-plane assets.
# Build workflow: use the architecture base tag, overlay runtime layers, push runtime tag.

ARG KALI_BASE_IMAGE=drowai/kali-pentesting:arm64-base

FROM ${KALI_BASE_IMAGE} AS runtime
USER root

ENV DROWAI_RUNTIME_ROOT=/opt/drowai/runtime \
    DROWAI_RUNTIME_PYTHON_ROOT=/opt/drowai/runtime/python \
    DROWAI_RUNTIME_VPN_ROOT=/opt/drowai/runtime/vpn \
    DROWAI_RUNTIME_MANIFEST_ROOT=/opt/drowai/runtime/manifests \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/drowai/runtime/python

RUN mkdir -p "${DROWAI_RUNTIME_PYTHON_ROOT}" "${DROWAI_RUNTIME_VPN_ROOT}" "${DROWAI_RUNTIME_MANIFEST_ROOT}"

COPY runtime/image/runtime-requirements.txt /opt/drowai/runtime/runtime-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /opt/drowai/runtime/runtime-requirements.txt

COPY agent/workspace_init.py /opt/drowai/runtime/python/workspace_init.py
COPY kali_executor/__init__.py /opt/drowai/runtime/python/kali_executor/__init__.py
COPY kali_executor/communication/__init__.py /opt/drowai/runtime/python/kali_executor/communication/__init__.py
COPY kali_executor/communication/file_comm.py /opt/drowai/runtime/python/kali_executor/communication/file_comm.py
COPY kali_executor/executor_daemon.py /opt/drowai/runtime/python/kali_executor/executor_daemon.py
COPY runtime_shared/__init__.py /opt/drowai/runtime/python/runtime_shared/__init__.py
COPY runtime_shared/file_comm_contracts.py /opt/drowai/runtime/python/runtime_shared/file_comm_contracts.py
COPY runtime_shared/runtime_manifest.py /opt/drowai/runtime/python/runtime_shared/runtime_manifest.py
COPY runtime_shared/vpn_observability.py /opt/drowai/runtime/python/runtime_shared/vpn_observability.py
COPY runtime/vpn/vpn-manager.sh /opt/drowai/runtime/vpn/vpn-manager.sh
COPY runtime/manifests/runtime-assets.lock /opt/drowai/runtime/manifests/runtime-assets.lock

RUN ln -s kali_executor/executor_daemon.py "${DROWAI_RUNTIME_PYTHON_ROOT}/executor_daemon.py" \
    && sed -i 's/\r$//' "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && chmod 0755 "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh"

RUN PYTHONPYCACHEPREFIX=/tmp/drowai-pycache python3 -m py_compile \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/workspace_init.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/executor_daemon.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/kali_executor/__init__.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/kali_executor/communication/__init__.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/kali_executor/communication/file_comm.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/runtime_shared/__init__.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/runtime_shared/file_comm_contracts.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/runtime_shared/runtime_manifest.py" \
    "${DROWAI_RUNTIME_PYTHON_ROOT}/runtime_shared/vpn_observability.py" \
    && rm -rf /tmp/drowai-pycache

RUN python3 "${DROWAI_RUNTIME_PYTHON_ROOT}/executor_daemon.py" --runtime-info \
    | python3 -c "import json,sys; p=json.load(sys.stdin); assert p.get('runtime_contract_version')=='1.1', p"

FROM runtime AS runtime-assets-check
USER root
ENV DROWAI_RUNTIME_PYTHON_ROOT=/opt/drowai/runtime/python
ENV DROWAI_RUNTIME_VPN_ROOT=/opt/drowai/runtime/vpn
ENV DROWAI_RUNTIME_MANIFEST_ROOT=/opt/drowai/runtime/manifests

RUN test -f "${DROWAI_RUNTIME_MANIFEST_ROOT}/runtime-assets.lock"
RUN test -f "${DROWAI_RUNTIME_PYTHON_ROOT}/workspace_init.py" \
    && test -f "${DROWAI_RUNTIME_PYTHON_ROOT}/executor_daemon.py"
RUN test -f "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && test -x "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && bash -n "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && grep -Eq '^[[:space:]]*connect\)' "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && grep -Eq '^[[:space:]]*disconnect\)' "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && grep -Eq '^[[:space:]]*status\)' "${DROWAI_RUNTIME_VPN_ROOT}/vpn-manager.sh" \
    && command -v flock >/dev/null

RUN python3 -c "from workspace_init import init_workspace; from executor_daemon import run_daemon; from kali_executor.communication.file_comm import FileCommExecutor; from runtime_shared.vpn_observability import classify_vpn_failure; print('runtime-assets-ok')"

RUN python3 "${DROWAI_RUNTIME_PYTHON_ROOT}/executor_daemon.py" --runtime-info \
    | python3 -c "import json,sys; p=json.load(sys.stdin); assert p.get('runtime_contract_version')=='1.1', p"

RUN python3 - <<'PY'
import hashlib
import json
import pathlib

manifest_path = pathlib.Path("/opt/drowai/runtime/manifests/runtime-assets.lock")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
assert manifest.get("manifest_version") == 1, "manifest_version must be 1"
assert manifest.get("runtime_version"), "runtime_version marker required"

assets = manifest.get("assets") or []
assert assets, "assets list is required"

expected_python_paths = {
    pathlib.Path(asset["path"])
    for asset in assets
    if pathlib.Path(asset["path"]).suffix == ".py"
}
python_root = pathlib.Path("/opt/drowai/runtime/python")
actual_python_paths = {
    path
    for path in python_root.rglob("*.py")
    if path.is_file()
}
assert actual_python_paths == expected_python_paths, (
    "runtime Python inventory mismatch: "
    f"expected={sorted(map(str, expected_python_paths))} "
    f"actual={sorted(map(str, actual_python_paths))}"
)

entrypoint = python_root / "executor_daemon.py"
package_entrypoint = python_root / "kali_executor/executor_daemon.py"
assert entrypoint.is_symlink(), "top-level executor_daemon.py must be a symlink"
assert entrypoint.resolve() == package_entrypoint.resolve(), (
    "top-level executor_daemon.py must target kali_executor/executor_daemon.py"
)

cache_paths = [
    path
    for path in python_root.rglob("*")
    if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
]
assert not cache_paths, f"runtime Python cache files are forbidden: {cache_paths}"

for asset in assets:
    path = pathlib.Path(asset["path"])
    assert path.is_file(), f"missing runtime asset: {path}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = asset["sha256"]
    assert digest == expected, f"checksum mismatch for {path}: expected {expected}, got {digest}"
PY

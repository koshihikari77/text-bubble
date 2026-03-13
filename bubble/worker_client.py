from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOCKET_PATH = Path(os.environ.get("TEXT_BUBBLE_WORKER_SOCKET", f"/tmp/text-bubble-worker-{os.getuid()}.sock"))
DEFAULT_LOG_PATH = Path(os.environ.get("TEXT_BUBBLE_WORKER_LOG", f"/tmp/text-bubble-worker-{os.getuid()}.log"))


def _socket_path() -> Path:
    return DEFAULT_SOCKET_PATH


def _worker_cmd() -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "text_bubble_worker.py"),
        "--socket",
        str(_socket_path()),
    ]


def _wait_for_socket(path: Path, timeout_s: float = 8.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def ensure_worker_running() -> Path:
    socket_path = _socket_path()
    if socket_path.exists():
        return socket_path
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_LOG_PATH.open("ab") as handle:
        subprocess.Popen(
            _worker_cmd(),
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    if not _wait_for_socket(socket_path):
        raise RuntimeError(f"worker did not start: {socket_path}")
    return socket_path


def worker_request(command: str, payload: dict[str, Any], *, mode: str) -> dict[str, Any] | None:
    if mode not in {"auto", "on", "off"}:
        raise RuntimeError(f"invalid worker mode: {mode}")
    if mode == "off":
        return None
    try:
        socket_path = ensure_worker_running()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(600)
            client.connect(str(socket_path))
            request = {"command": command, "payload": payload}
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            response_chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response_chunks.append(chunk)
                if b"\n" in chunk:
                    break
        raw = b"".join(response_chunks).split(b"\n", 1)[0]
        response = json.loads(raw.decode("utf-8"))
        if response.get("status") == "error":
            raise RuntimeError(response.get("message", "worker request failed"))
        return response
    except Exception:
        if mode == "auto":
            return None
        raise

"""stdio launcher for Eyescope's local MCP server.

MCP clients spawn this process. It finds the running engine, reads the
user-scoped secret from the app data directory, and proxies stdio to
``POST /mcp`` on 127.0.0.1. The secret is never written to a client config
and never printed.

If the engine is not listening, this tries the launch record the app wrote
(``mcp-app.json``) and then the known install paths. When that does not
bring the engine up, every request gets a visible Open Eyescope error.

OAuth is not implemented. ``load_credentials`` is the seam a later login
can replace; the proxy would keep sending ``Authorization: Bearer``.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPEN_EYESCOPE = "Open Eyescope. The local engine is not running."
SECRET_MISSING = "Open Eyescope. The local secret is not available."
PRODUCT_DIRS = ("eyescope-organizer-dev", "eyescope-organizer", "Eyescope")
_SERVER_ICON: dict | None = None
_ICON_LOADED = False


class EngineDown(Exception):
    pass


def candidate_data_dirs() -> list[Path]:
    override = os.environ.get("EYESCOPE_DATA_DIR", "").strip()
    if override:
        return [Path(override)]
    home = Path.home()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        roots = [appdata / name for name in PRODUCT_DIRS]
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        roots = [base / name for name in PRODUCT_DIRS]
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        roots = [base / name for name in PRODUCT_DIRS]
    return [root / "backend" for root in roots]


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def choose_data_dir() -> Path | None:
    candidates = candidate_data_dirs()
    live = []
    for path in candidates:
        record = _read_json(path / "mcp-engine.json")
        port = record.get("port") if record else None
        if isinstance(port, int) and _port_open("127.0.0.1", port):
            live.append(path)
    if live:
        return live[0]
    with_secret = [path for path in candidates if (path / "mcp-token").is_file()]
    if with_secret:
        return with_secret[0]
    existing = [path for path in candidates if path.is_dir()]
    return existing[0] if existing else (candidates[0] if candidates else None)


def load_credentials(data_dir: Path) -> tuple[str, str]:
    """Return the scheme and secret for the local HTTP MCP.

    Today this is the bearer secret in ``mcp-token``. An OAuth login can
    replace this function and return a bearer access token instead.
    """
    path = data_dir / "mcp-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise EngineDown(SECRET_MISSING) from exc
    if not token:
        raise EngineDown(SECRET_MISSING)
    return "Bearer", token


def _endpoint(data_dir: Path) -> tuple[str, str, int]:
    override = os.environ.get("EYESCOPE_MCP_URL", "").strip()
    if override:
        url = override
    else:
        record = _read_json(data_dir / "mcp-engine.json") or {}
        port = record.get("port")
        if not isinstance(port, int):
            raw = os.environ.get("EYESCOPE_BACKEND_PORT", "8123").strip() or "8123"
            try:
                port = int(raw)
            except ValueError:
                port = 8123
        url = f"http://127.0.0.1:{port}/mcp"
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        raise EngineDown("Open Eyescope. The MCP endpoint is not local.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return url, host, port


def _known_launch_commands() -> list[tuple[str, list[str]]]:
    if sys.platform == "darwin":
        return [("/Applications/EyeScope.app/Contents/MacOS/EyeScope", ["--mcp-engine"])]
    if os.name == "nt":
        names = []
        program_files = os.environ.get("ProgramFiles")
        local = os.environ.get("LOCALAPPDATA")
        if program_files:
            names.append(str(Path(program_files) / "EyeScope" / "EyeScope.exe"))
        if local:
            names.append(str(Path(local) / "Programs" / "EyeScope" / "EyeScope.exe"))
        return [(name, ["--mcp-engine"]) for name in names]
    return []


def _spawn(executable: str, args: list[str]) -> bool:
    if not executable or not os.path.exists(executable):
        return False
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([executable, *args], **kwargs)
    except OSError as exc:
        print(f"eyescope-mcp could not start the app: {exc}", file=sys.stderr)
        return False
    print(f"eyescope-mcp starting {executable}", file=sys.stderr)
    return True


def try_start(data_dir: Path) -> bool:
    record = _read_json(data_dir / "mcp-app.json")
    if record is not None:
        executable = record.get("executable")
        args = record.get("args") if isinstance(record.get("args"), list) else []
        if isinstance(executable, str):
            return _spawn(executable, [str(arg) for arg in args])
        return False
    for executable, args in _known_launch_commands():
        if _spawn(executable, args):
            return True
    return False


def ensure_engine(data_dir: Path) -> tuple[str, str]:
    _scheme, secret = load_credentials(data_dir)
    url, host, port = _endpoint(data_dir)
    if _port_open(host, port):
        return url, secret
    started = try_start(data_dir)
    timeout = float(os.environ.get("EYESCOPE_MCP_START_TIMEOUT", "20") or "20")
    deadline = time.monotonic() + max(timeout, 0)
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return url, secret
        if not started:
            break
        time.sleep(0.2)
    raise EngineDown(OPEN_EYESCOPE)


def _parse_response(raw: bytes, content_type: str) -> dict | None:
    if not raw.strip():
        return None
    text = raw.decode("utf-8")
    if "text/event-stream" in content_type or text.startswith("data:") or text.startswith("event:"):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not data:
            raise RuntimeError("MCP endpoint returned an event stream with no data.")
        return json.loads("\n".join(data))
    return json.loads(text)


def forward(url: str, token: str, message: dict) -> dict | None:
    body = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
            return _parse_response(payload, content_type)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32001, "message": payload.decode("utf-8", errors="replace")},
            }
        return parsed if isinstance(parsed, dict) else None


def server_icon() -> dict | None:
    """Mac app tile returned on initialize so clients can draw the server."""
    global _SERVER_ICON, _ICON_LOADED
    if _ICON_LOADED:
        return _SERVER_ICON
    _ICON_LOADED = True
    path = Path(__file__).with_name("icon.png")
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    encoded = base64.standard_b64encode(raw).decode("ascii")
    _SERVER_ICON = {
        "src": f"data:image/png;base64,{encoded}",
        "mimeType": "image/png",
        "sizes": ["1024x1024"],
    }
    return _SERVER_ICON


def with_server_icon(message: dict, response: dict | None) -> dict | None:
    icon = server_icon()
    if response is None or icon is None or message.get("method") != "initialize":
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    info = result.get("serverInfo")
    if not isinstance(info, dict) or info.get("icons"):
        return response
    info["icons"] = [icon]
    return response


def failure_response(message: dict, text: str) -> dict | None:
    if "id" not in message:
        return None
    if message.get("method") == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": True,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "error": {"code": -32000, "message": text},
    }


def read_message(stream) -> tuple[dict | None, str | None]:
    while True:
        prefix = stream.read(1)
        if not prefix:
            return None, None
        if prefix in b" \t\r\n":
            continue
        break
    if prefix == b"{":
        rest = stream.readline()
        if not rest:
            return None, None
        try:
            return json.loads((prefix + rest).decode("utf-8")), "line"
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON-RPC message: {exc}") from exc
    header = prefix + stream.readline()
    headers: dict[str, str] = {}
    while header not in (b"\r\n", b"\n", b""):
        if b":" in header:
            key, value = header.decode("ascii", errors="replace").split(":", 1)
            headers[key.strip().lower()] = value.strip()
        header = stream.readline()
        if not header:
            return None, None
    length = int(headers.get("content-length", "0") or "0")
    if length <= 0:
        return None, None
    body = stream.read(length)
    return json.loads(body.decode("utf-8")), "content-length"


def write_message(stream, payload: dict, framing: str | None) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if framing == "content-length":
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    else:
        stream.write(body + b"\n")
    stream.flush()


def main() -> int:
    data_dir = choose_data_dir()
    url = ""
    token = ""
    ready = False
    if data_dir is not None:
        try:
            url, token = ensure_engine(data_dir)
            ready = True
        except EngineDown as exc:
            print(str(exc), file=sys.stderr)
            down_message = str(exc)
        except Exception as exc:
            print(f"eyescope-mcp failed: {exc}", file=sys.stderr)
            down_message = OPEN_EYESCOPE
        else:
            down_message = ""
    else:
        down_message = OPEN_EYESCOPE
        print(OPEN_EYESCOPE, file=sys.stderr)

    while True:
        try:
            message, framing = read_message(sys.stdin.buffer)
        except Exception as exc:
            print(f"eyescope-mcp could not read stdio: {exc}", file=sys.stderr)
            return 1
        if message is None:
            return 0
        if not ready:
            response = failure_response(message, down_message or OPEN_EYESCOPE)
        else:
            try:
                response = forward(url, token, message)
            except Exception as exc:
                print(f"eyescope-mcp forward failed: {exc}", file=sys.stderr)
                response = failure_response(message, OPEN_EYESCOPE)
        response = with_server_icon(message, response)
        if response is None:
            continue
        try:
            write_message(sys.stdout.buffer, response, framing)
        except Exception as exc:
            print(f"eyescope-mcp could not write stdio: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

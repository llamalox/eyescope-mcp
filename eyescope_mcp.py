"""stdio launcher for Eyescope's local MCP server.

MCP clients spawn this process. It finds the running engine, reads the
user-scoped secret from the app data directory, and proxies stdio to
``POST /mcp`` on 127.0.0.1. The secret is never written to a client config
and never printed.

Opening the client does not start the engine. A tool call does. If the
engine is not listening then, this tries the launch record the app wrote
(``mcp-app.json``) and then the known install paths. When that does not
bring the engine up, the tool call gets a visible Open Eyescope error.
The handshake and the tool list are answered here, so a client can connect
while the engine is stopped.

OAuth is not implemented. ``load_credentials`` is the seam a later login
can replace; the proxy would keep sending ``Authorization: Bearer``.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPEN_EYESCOPE = "Open Eyescope. The local engine is not running."
SECRET_MISSING = "Open Eyescope. The local secret is not available."
# Keep in sync with MCP_SIGN_IN_REQUIRED_MESSAGE in backend.app.mcp.auth.
SIGN_IN_REQUIRED = (
    "Sign in to Eyescope to use agents. Open Eyescope and sign in, "
    "or use Sign in from the menu bar icon if the app is in the background."
)
PRODUCT_DIRS = ("eyescope-organizer-dev", "eyescope-organizer", "Eyescope")
# Cursor sends these as soon as the process starts. They must not launch EyeScope.
QUIET_METHODS = frozenset({
    "initialize",
    "ping",
    "tools/list",
    "resources/list",
    "prompts/list",
    "resources/templates/list",
})
_OFFLINE_TOOLS: list[dict] | None = None
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
        return [("/Applications/EyeScope.app/Contents/MacOS/EyeScope", ["--background"])]
    if os.name == "nt":
        names = []
        program_files = os.environ.get("ProgramFiles")
        local = os.environ.get("LOCALAPPDATA")
        if program_files:
            names.append(str(Path(program_files) / "EyeScope" / "EyeScope.exe"))
        if local:
            names.append(str(Path(local) / "Programs" / "EyeScope" / "EyeScope.exe"))
        return [(name, ["--background"]) for name in names]
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


def needs_engine(method: str) -> bool:
    """A tool call needs the engine. The connect handshake does not."""
    if not method or method.startswith("notifications/"):
        return False
    return method not in QUIET_METHODS


def running_connection(data_dir: Path) -> tuple[str, str] | None:
    """Attach when the port is already open. This does not launch the app."""
    try:
        url, host, port = _endpoint(data_dir)
    except EngineDown:
        return None
    if not _port_open(host, port):
        return None
    try:
        _scheme, secret = load_credentials(data_dir)
    except EngineDown:
        return None
    return url, secret


def _annotation_type(node: ast.AST | None) -> str:
    if node is None:
        return "string"
    text = ast.unparse(node)
    if text.startswith("list") or text.startswith("List"):
        return "array"
    if text in {"int", "float"} or text.startswith("int ") or text.startswith("float "):
        return "number"
    if text == "bool" or text.startswith("bool "):
        return "boolean"
    return "string"


def offline_tools() -> list[dict]:
    """Tool list for a client that has not started the engine yet."""
    global _OFFLINE_TOOLS
    if _OFFLINE_TOOLS is not None:
        return _OFFLINE_TOOLS
    root = Path(__file__).resolve().parents[1]
    server = (root / "backend" / "app" / "mcp" / "server.py").read_text(encoding="utf-8")
    names = re.findall(r"tool_ops\.([A-Za-z_][A-Za-z0-9_]*)", server.split("_TOOLS = (", 1)[-1].split(")", 1)[0])
    tree = ast.parse((root / "backend" / "app" / "mcp" / "tools.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    tools = []
    for name in names:
        fn = functions.get(name)
        if fn is None:
            continue
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []
        for arg in fn.args.args:
            if arg.arg == "services":
                continue
            properties[arg.arg] = {"type": _annotation_type(arg.annotation)}
        defaults = fn.args.defaults
        required_count = len(fn.args.args) - len(defaults)
        for arg in fn.args.args[:required_count]:
            if arg.arg != "services":
                required.append(arg.arg)
        doc = ast.get_docstring(fn) or ""
        tools.append({
            "name": name,
            "description": doc.split("\n", 1)[0],
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    _OFFLINE_TOOLS = tools
    return tools


def client_name(message: dict) -> str | None:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    name = info.get("name") if isinstance(info, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def local_result(message: dict) -> dict | None:
    """Handshake answer used while the engine is stopped."""
    method = message.get("method") if isinstance(message.get("method"), str) else ""
    if method.startswith("notifications/"):
        return None
    if "id" not in message:
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        version = params.get("protocolVersion")
        if not isinstance(version, str) or not version:
            version = "2025-03-26"
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Eyescope", "version": "0.1.3"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": offline_tools()}}
    if method in {"resources/list", "prompts/list", "resources/templates/list"}:
        key = "prompts" if method == "prompts/list" else "resourceTemplates" if "templates" in method else "resources"
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {key: []}}
    return failure_response(message, OPEN_EYESCOPE)


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
        if exc.code == 401 and isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                msg = str(error.get("message") or "")
                if (
                    (isinstance(data, dict) and data.get("eyescopeCode") == "sign_in_required")
                    or msg.startswith("Sign in to Eyescope")
                ):
                    error["message"] = SIGN_IN_REQUIRED
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


class EngineClient:
    """Registers this launcher as an MCP client and heartbeats until stdin ends."""

    def __init__(self, data_dir: Path, mcp_url: str) -> None:
        self.data_dir = data_dir
        self.base = mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url
        self.client_id = f"mcp:{os.getpid()}"
        self.name = "Agent"
        self.registered = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def launch_token(self) -> str:
        path = self.data_dir / "engine-token"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EngineDown("Open Eyescope. The engine launch token is not available.") from exc
        if not token:
            raise EngineDown("Open Eyescope. The engine launch token is not available.")
        return token

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self.launch_token()}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def register(self, name: str | None = None) -> None:
        if isinstance(name, str) and name.strip():
            self.name = name.strip()
        status, payload = self._request(
            "POST",
            "/api/v1/engine/clients",
            {
                "clientId": self.client_id,
                "kind": "mcp",
                "name": self.name,
                "contractVersion": 1,
                "holdsEngine": False,
            },
        )
        if status != 200:
            text = payload.decode("utf-8", errors="replace")
            raise EngineDown(f"Open Eyescope. The agent could not register with the engine. {text}")
        self.registered = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._beat, name="mcp-heartbeat", daemon=True)
            self._thread.start()

    def note_initialize(self, message: dict) -> None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        name = info.get("name") if isinstance(info, dict) else None
        self.register(name if isinstance(name, str) else None)

    def _beat(self) -> None:
        quoted = urllib.parse.quote(self.client_id, safe="")
        while not self._stop.wait(10):
            try:
                status, _payload = self._request("POST", f"/api/v1/engine/clients/{quoted}/heartbeat")
            except Exception as exc:
                print(f"eyescope-mcp heartbeat failed: {exc}", file=sys.stderr)
                continue
            if status not in (200, 404):
                print(f"eyescope-mcp heartbeat failed: HTTP {status}", file=sys.stderr)

    def close(self) -> None:
        self._stop.set()
        if not self.registered:
            return
        quoted = urllib.parse.quote(self.client_id, safe="")
        try:
            status, payload = self._request("DELETE", f"/api/v1/engine/clients/{quoted}")
        except Exception as exc:
            print(f"eyescope-mcp could not disconnect: {exc}", file=sys.stderr)
            return
        if status not in (200, 404):
            text = payload.decode("utf-8", errors="replace")
            print(f"eyescope-mcp could not disconnect: HTTP {status} {text}", file=sys.stderr)


def _attach(data_dir: Path, url: str, name: str | None) -> EngineClient:
    attached = EngineClient(data_dir, url)
    attached.register(name)
    return attached


def main() -> int:
    data_dir = choose_data_dir()
    url = ""
    token = ""
    ready = False
    client: EngineClient | None = None
    known_name: str | None = None
    down_message = ""

    while True:
        try:
            message, framing = read_message(sys.stdin.buffer)
        except Exception as exc:
            print(f"eyescope-mcp could not read stdio: {exc}", file=sys.stderr)
            return 1
        if message is None:
            if client is not None:
                client.close()
            return 0
        method = message.get("method") if isinstance(message.get("method"), str) else ""
        if method == "initialize":
            known_name = client_name(message) or known_name
        if data_dir is not None and not ready:
            attached = running_connection(data_dir)
            if attached is not None and (needs_engine(method) or method in {"initialize", "tools/list"}):
                url, token = attached
                try:
                    client = _attach(data_dir, url, known_name)
                    ready = True
                    down_message = ""
                except EngineDown as exc:
                    print(str(exc), file=sys.stderr)
                    down_message = str(exc)
            elif needs_engine(method):
                try:
                    url, token = ensure_engine(data_dir)
                    client = _attach(data_dir, url, known_name)
                    ready = True
                    down_message = ""
                except EngineDown as exc:
                    print(str(exc), file=sys.stderr)
                    down_message = str(exc)
                except Exception as exc:
                    print(f"eyescope-mcp failed: {exc}", file=sys.stderr)
                    down_message = OPEN_EYESCOPE
        if ready and client is not None and method == "initialize":
            try:
                client.note_initialize(message)
            except EngineDown as exc:
                print(str(exc), file=sys.stderr)
                ready = False
                down_message = str(exc)
        if needs_engine(method) and not ready:
            response = failure_response(message, down_message or OPEN_EYESCOPE)
        elif ready:
            try:
                response = forward(url, token, message)
            except Exception as exc:
                print(f"eyescope-mcp forward failed: {exc}", file=sys.stderr)
                response = failure_response(message, OPEN_EYESCOPE)
        else:
            response = local_result(message)
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

#!/usr/bin/env python3
"""HTTP server: infrastructure only.

Tab-specific endpoints are registered by modules in tabs/*/routes.py.
Each routes.py must define:
  TAB_METADATA = {"id": str, "label": str, "order": int}
  register(get_routes: dict, post_routes: dict) -> None
"""
import os
import sys

# ── Ensure the cir-utils conda env is used ────────────────────────────────────
_conda_env_bin = "/opt/anaconda3/envs/cir-utils/bin"
_conda_python  = os.path.join(_conda_env_bin, "python3")

# Replace PATH so subprocesses resolve tools exclusively from the cir-utils env.
# Other conda env bin dirs are stripped; standard system paths are preserved.
_CONDA_ROOT = "/opt/anaconda3"
_system_path_entries = [
    p for p in os.environ.get("PATH", "").split(os.pathsep)
    if not p.startswith(_CONDA_ROOT)
]
os.environ["PATH"] = os.pathsep.join([_conda_env_bin] + _system_path_entries)
if os.path.isfile(_conda_python) and os.path.realpath(sys.executable) != os.path.realpath(_conda_python):
    os.execv(_conda_python, [_conda_python] + sys.argv)

import http.server
import importlib.util
import json
import re
import socket
import time
import hmac
import secrets
import signal
import atexit

PORT       = int(os.environ.get("PORT", 8080))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN") or secrets.token_urlsafe(16)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Overview file ──────────────────────────────────────────────────────────────
_OVERVIEW_FILE = None  # absolute path to ../*_overview.html, or None


def _detect_overview_file():
    """Locate a single *_overview.html one directory above this repo."""
    global _OVERVIEW_FILE
    parent = os.path.realpath(os.path.join(_SCRIPT_DIR, ".."))
    try:
        for fname in sorted(os.listdir(parent)):
            if re.match(r'^[^/\\]+_overview\.html$', fname):
                candidate = os.path.realpath(os.path.join(parent, fname))
                if os.path.dirname(candidate) == parent and os.path.isfile(candidate):
                    _OVERVIEW_FILE = candidate
                    return
    except Exception:
        pass


# ── Global server reference ────────────────────────────────────────────────────
_httpd = None


def cleanup_server():
    global _httpd
    if _httpd:
        try:
            _httpd.server_close()
            if not getattr(cleanup_server, '_called_by_signal', False):
                print(f"\nServer stopped cleanly on port {PORT}")
        except Exception:
            pass


def signal_handler(signum, frame):
    print(f"\nRemote received signal {signum}, shutting down...")
    print()
    cleanup_server._called_by_signal = True
    global _httpd
    if _httpd:
        try:
            _httpd.server_close()
            print(f"Server stopped cleanly on port {PORT}")
            print()
        except Exception:
            pass
    sys.exit(0)


atexit.register(cleanup_server)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ── Rate limiting ──────────────────────────────────────────────────────────────
_request_times = {}
_RATE_LIMIT    = 75  # max requests per minute per IP


def _rate_limit_check(client_ip):
    now = time.time()
    if client_ip in _request_times:
        _request_times[client_ip] = [t for t in _request_times[client_ip] if now - t < 60]
        if len(_request_times[client_ip]) >= _RATE_LIMIT:
            return False
        _request_times[client_ip].append(now)
    else:
        _request_times[client_ip] = [now]
    return True


# ── Auth ───────────────────────────────────────────────────────────────────────
def _check_auth(request_path, query_params):
    if request_path in ('/', '/index.html'):
        return True
    token = query_params.get('token', [None])[0]
    return bool(token and hmac.compare_digest(token, AUTH_TOKEN))


# ── Port helpers ───────────────────────────────────────────────────────────────
def _check_port_available(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("localhost", port))
        sock.close()
        return True
    except OSError:
        return False


def _find_available_port(start_port=8080, max_attempts=10):
    for i in range(max_attempts):
        p = start_port + i
        if _check_port_available(p):
            return p
    return None


# ── Tab / route discovery ──────────────────────────────────────────────────────
_GET_ROUTES  = {}   # path -> handler(h, query_params)
_POST_ROUTES = {}   # path -> handler(h, body_dict)
_TABS        = []   # list of TAB_METADATA dicts, sorted by "order"


def _detect_project_root(script_dir):
    """Return /data/projects/<PROJECT_NAME> for any nested repo location.

    This keeps tab visibility checks stable even if this repository is moved
    deeper under utility folders.
    """
    resolved = os.path.realpath(script_dir)
    m = re.match(r"^(/data/projects/[^/]+)(?:/|$)", resolved)
    if m:
        return m.group(1)
    # Fallback for non-standard deployments where the repo lives directly
    # inside the project folder (e.g. /some/path/<project>/cir-utils).
    return os.path.realpath(os.path.join(script_dir, ".."))


_PROJECT_ROOT = _detect_project_root(_SCRIPT_DIR)


def _tab_is_visible(tab):
    required_path = tab.get("requires_path")
    if not required_path:
        return True
    required_abs = os.path.realpath(os.path.join(_PROJECT_ROOT, required_path))
    return os.path.isdir(required_abs)


def _discover_tabs():
    tabs_root = os.path.join(_SCRIPT_DIR, "tabs")
    if not os.path.isdir(tabs_root):
        return

    for tab_name in sorted(os.listdir(tabs_root)):
        # Validate name to prevent path traversal
        if not re.match(r'^[a-zA-Z0-9_-]+$', tab_name):
            continue
        routes_path = os.path.join(tabs_root, tab_name, "routes.py")
        if not os.path.isfile(routes_path):
            continue
        try:
            spec   = importlib.util.spec_from_file_location(f"tab_{tab_name}_routes", routes_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.register(_GET_ROUTES, _POST_ROUTES)
            _TABS.append(module.TAB_METADATA)
        except Exception as exc:
            print(f"Warning: failed to load tab '{tab_name}': {exc}")

    _TABS.sort(key=lambda t: t.get("order", 99))


# ── HTTP handler ───────────────────────────────────────────────────────────────
class ReuseAddrHTTPServer(http.server.HTTPServer):
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class _ServerWithToken(ReuseAddrHTTPServer):
    """Exposes the auth token to route handlers via h.server._auth_token."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._auth_token = AUTH_TOKEN


class Handler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        client_ip = self.client_address[0]
        if not _rate_limit_check(client_ip):
            self.send_error(429, "Too many requests")
            return

        parsed = urlparse(self.path)
        query  = parse_qs(parsed.query)
        path   = parsed.path

        if not _check_auth(path, query):
            self._send_auth_error()
            return

        # ── Built-in infrastructure endpoints ──
        if path == '/api/tabs':
            visible = [tab for tab in _TABS if _tab_is_visible(tab)]
            if _OVERVIEW_FILE and os.path.isfile(_OVERVIEW_FILE):
                overview_tab = {
                    "id": "overview",
                    "label": "Project Overview",
                    "order": -1,
                    "type": "overview",
                }
                visible = [overview_tab] + visible
            self._send_json(visible)
            return

        if path == '/overview-file':
            if not _OVERVIEW_FILE or not os.path.isfile(_OVERVIEW_FILE):
                self.send_error(404, "Overview file not found")
                return
            with open(_OVERVIEW_FILE, encoding="utf-8") as fh:
                body = fh.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/tab-content':
            tab_id = query.get('id', [None])[0]
            if not tab_id or not re.match(r'^[a-zA-Z0-9_-]+$', tab_id):
                self.send_error(400, "Invalid tab id")
                return
            tab_file = os.path.realpath(
                os.path.join(_SCRIPT_DIR, "tabs", tab_id, "tab.html")
            )
            allowed = os.path.realpath(os.path.join(_SCRIPT_DIR, "tabs"))
            if not tab_file.startswith(allowed + os.sep):
                self.send_error(403, "Path outside tabs directory")
                return
            if not os.path.isfile(tab_file):
                self.send_error(404, "Tab not found")
                return
            with open(tab_file, encoding="utf-8") as fh:
                body = fh.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ('/', '/index.html'):
            super().do_GET()
            return

        # ── Dynamic tab routes ──
        handler = _GET_ROUTES.get(path)
        if handler:
            handler(self, query)
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        client_ip = self.client_address[0]
        if not _rate_limit_check(client_ip):
            self.send_error(429, "Too many requests")
            return

        parsed = urlparse(self.path)
        query  = parse_qs(parsed.query)
        path   = parsed.path

        if not _check_auth(path, query):
            self._send_auth_error()
            return

        handler = _POST_ROUTES.get(path)
        if handler:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self.send_error(400, "Invalid JSON body")
                return
            handler(self, body)
            return

        self.send_error(404, "Not found")

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_auth_error(self):
        body = b"""<!DOCTYPE html>
<html><head><title>Authentication Required</title></head>
<body style="font-family:monospace;text-align:center;margin-top:100px;">
<h2>Authentication Required</h2>
<p>Please provide a valid authentication token.</p>
<form method="get" action="/">
  <input type="password" name="token" placeholder="Enter token"
         style="padding:8px;width:200px;" />
  <button type="submit" style="padding:8px 16px;">Access</button>
</form>
</body></html>"""
        self.send_response(401)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress request logs


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _detect_overview_file()
    _discover_tabs()

    if not _TABS:
        print("Warning: no tabs discovered in tabs/*/routes.py")

    if not _check_port_available(PORT):
        print(f"Warning: Port {PORT} is already in use")
        alt = _find_available_port(PORT)
        if alt:
            print(f"Using alternative port {alt}")
            PORT = alt
        else:
            print("Error: No available ports found")
            sys.exit(1)

    try:
        _httpd = _ServerWithToken(("localhost", PORT), Handler)
        print(f"Process ID: {os.getpid()}")
        print(f"Authentication token: {AUTH_TOKEN}")
        print()
        print(f"\033[1;32mAccess URL: http://localhost:{PORT}/?token={AUTH_TOKEN}\033[0m")
        print()
        print("Ctrl+C to stop process and close tunnel")
        print()
        _httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        print(f"Server error: {e}")
        sys.exit(1)

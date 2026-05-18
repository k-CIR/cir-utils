#!/usr/bin/env python3
"""MR-BIDS tab: route registration and request handlers.

TAB_METADATA is read by the server at startup to register this tab.
register() is called once to populate the shared GET/POST route dicts.
"""
import json
import importlib.util
import os
import re
import subprocess

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\([A-Z]")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)

# Sibling modules live in the same directory
import sys
_TAB_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_project_root(script_dir):
    """Return /data/projects/<project> for nested repo locations."""
    resolved = os.path.realpath(script_dir)
    match = re.match(r"^(/data/projects/[^/]+)(?:/|$)", resolved)
    if match:
        return match.group(1)
    # Fallback: tab dir is <project>/cir-utils/tabs/<tab>
    return os.path.realpath(os.path.join(script_dir, "..", "..", ".."))

_CFG_PATH = os.path.join(_TAB_DIR, "config_builder.py")
_CFG_SPEC = importlib.util.spec_from_file_location("mr_bids_config_builder", _CFG_PATH)
config_builder = importlib.util.module_from_spec(_CFG_SPEC)
_CFG_SPEC.loader.exec_module(config_builder)
_LOCAL_CONFIG_FILE = os.path.realpath(os.path.join(_TAB_DIR, "..", "..", "dcm2bids_config_mr.json"))
if os.path.realpath(getattr(config_builder, "CONFIG_FILE", _LOCAL_CONFIG_FILE)) != _LOCAL_CONFIG_FILE:
    config_builder.CONFIG_FILE = _LOCAL_CONFIG_FILE

_RUNNER_PATH = os.path.join(_TAB_DIR, "bids_runner.py")
_RUNNER_SPEC = importlib.util.spec_from_file_location("mr_bids_runner", _RUNNER_PATH)
bids_runner = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(bids_runner)

# ── Tab metadata ──────────────────────────────────────────────────────────────
TAB_METADATA = {
    "id":    "mr-bids",
    "label": "MR BIDS",
    "order": 0,
    "requires_path": "raw/mri",
}

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT   = _detect_project_root(_TAB_DIR)
_RAW_MRI_DIR    = os.path.join(_PROJECT_ROOT, "raw", "mri")
_OUTPUT_DIR     = os.path.join(_TAB_DIR, "dcm2bids_helper")


def _find_executable(name):
    import shutil
    path = shutil.which(name)
    if path:
        return path
    # Fall back to same directory as the current Python interpreter (handles
    # conda envs where PATH may not be fully set at import time).
    candidate = os.path.join(os.path.dirname(os.path.realpath(sys.executable)), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


_DCM2BIDS_HELPER = _find_executable("dcm2bids_helper")


def _get_default_helper_path():
    """Return (rel_path, warning) for the first session in <project>/raw/mri."""
    if not os.path.isdir(_RAW_MRI_DIR):
        return None, "No MRI data found in path"

    entries = sorted(
        e for e in os.listdir(_RAW_MRI_DIR)
        if os.path.isdir(os.path.join(_RAW_MRI_DIR, e))
    )
    if not entries:
        return None, "No MRI data found in path"

    first     = entries[0]
    first_abs = os.path.join(_RAW_MRI_DIR, first)

    if re.match(r"^\d+_\d{8}_\d{6}$", first):
        return os.path.relpath(first_abs, _PROJECT_ROOT), None

    if re.match(r"^sub-", first):
        ses_entries = sorted(
            s for s in os.listdir(first_abs)
            if s.startswith("ses-") and os.path.isdir(os.path.join(first_abs, s))
        )
        if ses_entries:
            return os.path.relpath(os.path.join(first_abs, ses_entries[0]), _PROJECT_ROOT), None

    return os.path.relpath(first_abs, _PROJECT_ROOT), None


# ── Handler functions ──────────────────────────────────────────────────────────
# Each handler receives (handler_instance, query_params) for GET
# or (handler_instance, parsed_body_dict) for POST.

def _handle_get_config(h, params):
    default_path, warning = _get_default_helper_path()
    config_rel = os.path.relpath(
        os.path.realpath(config_builder.CONFIG_FILE),
        os.path.realpath(_PROJECT_ROOT)
    )
    h._send_json({
        "project_root": _PROJECT_ROOT,
        "default_path": default_path,
        "warning":      warning,
        "auth_token":   h.server._auth_token,
        "config_file":  config_rel,
    })


def _handle_get_helper_summary(h, params):
    rows = config_builder.read_helper_jsons()
    h._send_json({"rows": rows})


def _handle_get_bids_config(h, params):
    cfg = config_builder.load_config()
    h._send_json({"config": cfg})


def _handle_discover_sessions(h, params):
    dicom_root = params.get("dicom_root", [None])[0]
    if not dicom_root:
        h.send_error(400, "Missing dicom_root parameter")
        return
    dicom_root = dicom_root.lstrip("/")
    full_root  = os.path.realpath(os.path.join(_PROJECT_ROOT, dicom_root))
    allowed    = os.path.realpath(_PROJECT_ROOT)
    if not (full_root == allowed or full_root.startswith(allowed + os.sep)):
        h.send_error(403, "Path outside project root")
        return
    sessions = bids_runner.discover_sessions(full_root)
    h._send_json({"sessions": sessions})


def _handle_get_recode_table(h, params):
    h._send_json({"recode": config_builder.load_recode_table()})


def _handle_run_dcm2bids_helper(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing path parameter")
        return

    rel_path  = rel_path.lstrip("/")
    full_path = os.path.realpath(os.path.join(_PROJECT_ROOT, rel_path))
    allowed   = os.path.realpath(_PROJECT_ROOT)
    if not (full_path == allowed or full_path.startswith(allowed + os.sep)):
        h.send_error(403, "Path outside project root")
        return

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.end_headers()

    def emit(obj):
        h.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        h.wfile.flush()

    if not os.path.isdir(full_path):
        emit({"error": "invalid_path"})
        return

    if not _DCM2BIDS_HELPER:
        emit({"line": "Error: dcm2bids_helper not found. Is it installed in this Python environment?"})
        emit({"done": True, "returncode": 1})
        return

    force = params.get("force", ["0"])[0] == "1"
    cmd   = [_DCM2BIDS_HELPER, "-d", full_path, "-o", _OUTPUT_DIR]
    if force:
        cmd.append("--force")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                emit({"line": _strip_ansi(line.rstrip("\n"))})
            proc.wait(timeout=300)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            emit({"line": "Error: Process timed out (5 minutes)"})
            rc = 1
    except FileNotFoundError:
        emit({"line": f"Error: could not launch {_DCM2BIDS_HELPER}"})
        rc = 1
    except Exception as exc:
        emit({"line": f"Error: {exc}"})
        rc = 1

    emit({"done": True, "returncode": rc})


def _handle_stream_dcm2bids_job(h, params):
    job_id = params.get("job_id", [None])[0]
    if not job_id:
        h.send_error(400, "Missing job_id")
        return

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.end_headers()

    def emit(obj):
        h.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        h.wfile.flush()

    try:
        for entry in bids_runner.stream_job(job_id):
            emit(entry)
            if entry.get("type") == "done":
                break
    except (BrokenPipeError, ConnectionResetError):
        pass


def _handle_save_bids_config(h, body):
    if not isinstance(body, dict) or "descriptions" not in body:
        h.send_error(400, "Invalid config: missing 'descriptions'")
        return
    try:
        config_builder.save_config(body)
        h._send_json({"ok": True})
    except Exception as exc:
        h.send_error(500, f"Failed to save config: {exc}")


def _handle_save_recode_table(h, body):
    recode = body.get("recode") if isinstance(body, dict) else None
    if not isinstance(recode, dict):
        h.send_error(400, "Missing recode dict")
        return
    cleaned = {}
    for label, rec in recode.items():
        if not isinstance(rec, dict):
            continue
        rp = str(rec.get("recoded_participant") or "").strip()
        rs = str(rec.get("recoded_session")     or "").strip()
        if rp and not re.match(r"^\d+$", rp):
            h.send_error(400, f"Invalid recoded_participant: {rp}")
            return
        if rs and not re.match(r"^\d+$", rs):
            h.send_error(400, f"Invalid recoded_session: {rs}")
            return
        cleaned[label] = {"recoded_participant": rp, "recoded_session": rs}
    config_builder.save_recode_table(cleaned)
    h._send_json({"ok": True})


def _handle_run_dcm2bids(h, body):
    try:
        dicom_root_rel = body.get("dicom_root")
        output_rel     = body.get("output_dir")
        config_rel     = body.get("config_file")
        selected       = body.get("sessions")
        max_workers    = int(body.get("max_workers", 8))
        clobber        = bool(body.get("clobber", False))
    except (TypeError, ValueError):
        h.send_error(400, "Invalid parameters")
        return

    if not all([dicom_root_rel, output_rel, config_rel]):
        h.send_error(400, "Missing required parameters")
        return

    max_workers = max(1, min(max_workers, 24))
    allowed     = os.path.realpath(_PROJECT_ROOT)

    def _resolve(rel):
        p = os.path.realpath(os.path.join(_PROJECT_ROOT, str(rel).lstrip("/")))
        if not (p == allowed or p.startswith(allowed + os.sep)):
            raise ValueError(f"Path outside project root: {rel}")
        return p

    try:
        dicom_root  = _resolve(dicom_root_rel)
        output_dir  = _resolve(output_rel)
        config_file = _resolve(config_rel)
    except ValueError as exc:
        h.send_error(403, str(exc))
        return

    if not os.path.isfile(config_file):
        h._send_json({"error": "config_not_found"})
        return

    all_sessions = bids_runner.discover_sessions(dicom_root)
    if selected:
        sel_set  = set(selected)
        sessions = [s for s in all_sessions if s["label"] in sel_set]
    else:
        sessions = all_sessions

    recode = body.get("recode") or {}
    if isinstance(recode, dict) and recode:
        recoded = []
        for s in sessions:
            rec = recode.get(s["label"], {})
            rp  = str(rec.get("recoded_participant") or "").strip()
            rs  = str(rec.get("recoded_session")     or "").strip()
            if (rp and not re.match(r"^\d+$", rp)) or (rs and not re.match(r"^\d+$", rs)):
                h._send_json({"error": f"Invalid recode values for {s['label']}"})
                return
            ns = dict(s)
            if rp:
                ns["participant"] = rp
            if rs:
                ns["session"] = rs
            recoded.append(ns)
        sessions = recoded

    if not sessions:
        h._send_json({"error": "no_sessions"})
        return

    try:
        job_id = bids_runner.start_conversion(
            sessions, dicom_root, output_dir, config_file, max_workers, clobber
        )
        h._send_json({"job_id": job_id})
    except RuntimeError as exc:
        h._send_json({"error": str(exc)})


# ── Registration ───────────────────────────────────────────────────────────────

def register(get_routes, post_routes):
    """Populate get_routes and post_routes with this tab's endpoints."""
    get_routes["/mr-get-config"]           = _handle_get_config
    get_routes["/mr-get-helper-summary"]   = _handle_get_helper_summary
    get_routes["/mr-get-bids-config"]      = _handle_get_bids_config
    get_routes["/mr-discover-sessions"]    = _handle_discover_sessions
    get_routes["/mr-get-recode-table"]     = _handle_get_recode_table
    get_routes["/mr-run-dcm2bids-helper"]  = _handle_run_dcm2bids_helper
    get_routes["/mr-stream-dcm2bids-job"]  = _handle_stream_dcm2bids_job

    post_routes["/mr-save-bids-config"]    = _handle_save_bids_config
    post_routes["/mr-save-recode-table"]   = _handle_save_recode_table
    post_routes["/mr-run-dcm2bids"]        = _handle_run_dcm2bids

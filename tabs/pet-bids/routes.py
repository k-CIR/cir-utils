#!/usr/bin/env python3
"""PET-BIDS tab: route registration and request handlers."""
import csv
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\([A-Z]")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)

TAB_METADATA = {
    "id": "pet-bids",
    "label": "PET BIDS",
    "order": 1,
    "requires_path": "raw/pet",
}

_TAB_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_project_root(script_dir):
    """Return /data/projects/<project> for nested repo locations."""
    resolved = os.path.realpath(script_dir)
    
    # Primary: match /data/projects/<project>
    match = re.match(r"^(/data/projects/[^/]+)(?:/|$)", resolved)
    if match:
        project_root = match.group(1)
        # Verify it's a valid directory
        if os.path.isdir(project_root):
            return project_root
    
    # Fallback: tab dir is typically <project>/cir-utils/tabs/<tab>
    # Go up 3 levels from script_dir to reach <project>
    fallback = os.path.realpath(os.path.join(script_dir, "..", "..", ".."))
    
    # Verify fallback path is valid and contains expected structure
    if os.path.isdir(fallback) and os.path.basename(fallback) in os.listdir('/data/projects'):
        return fallback
    
    # Last resort: return at least the /data/projects/<name> if we can extract it
    if '/data/projects/' in resolved:
        parts = resolved.split('/')
        if len(parts) > 3:  # /data/projects/<name>/...
            return '/'.join(parts[:4])  # /data/projects/<name>
    
    # Absolute fallback
    return fallback

_CFG_PATH = os.path.join(_TAB_DIR, "config_builder.py")
_CFG_SPEC = importlib.util.spec_from_file_location("pet_bids_config_builder", _CFG_PATH)
config_builder = importlib.util.module_from_spec(_CFG_SPEC)
_CFG_SPEC.loader.exec_module(config_builder)
_LOCAL_CONFIG_FILE = os.path.realpath(os.path.join(_TAB_DIR, "..", "..", "dcm2bids_config_pet.json"))
if os.path.realpath(getattr(config_builder, "CONFIG_FILE", _LOCAL_CONFIG_FILE)) != _LOCAL_CONFIG_FILE:
    config_builder.CONFIG_FILE = _LOCAL_CONFIG_FILE

_RUNNER_PATH = os.path.join(_TAB_DIR, "bids_runner.py")
_RUNNER_SPEC = importlib.util.spec_from_file_location("pet_bids_runner", _RUNNER_PATH)
bids_runner = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(bids_runner)

_PET_RUNNER_PATH = os.path.join(_TAB_DIR, "pet2bids_runner.py")
_PET_RUNNER_SPEC = importlib.util.spec_from_file_location("pet2bids_runner", _PET_RUNNER_PATH)
pet2bids_runner = importlib.util.module_from_spec(_PET_RUNNER_SPEC)
_PET_RUNNER_SPEC.loader.exec_module(pet2bids_runner)

PROJECT_ROOT = _detect_project_root(_TAB_DIR)
_RAW_PET_HELPER_DIR = os.path.join(PROJECT_ROOT, "raw", "pet")
_OUTPUT_DIR = os.path.join(_TAB_DIR, "dcm2bids_helper")

_CSV_RAW_PET_DIR = os.path.join(PROJECT_ROOT, "BIDS")
_DEFAULT_PET_DATA_DIR = os.path.join(PROJECT_ROOT, "BIDS")
_DEFAULT_PETPREP_HTML_DIR = os.path.join(PROJECT_ROOT, "BIDS", "derivatives", "petprep")


def _resolve_config_path(value, default_path):
    raw = str(value or "").strip()
    if not raw:
        return os.path.realpath(default_path)
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    return os.path.realpath(os.path.join(PROJECT_ROOT, raw))


def _split_path_list(value):
    return [p.strip() for p in str(value or "").split(":") if p.strip()]


_RAW_PET_DIR = _resolve_config_path(os.environ.get("BIDS_UTILS_PET_DATA_DIR"), _DEFAULT_PET_DATA_DIR)
_CSV_DERIVATIVES_DIR_CANDIDATES = [
    os.path.join(_CSV_RAW_PET_DIR, "derivatives"),
    os.path.join(PROJECT_ROOT, "BIDS_pet", "derivatives"),
    os.path.join(PROJECT_ROOT, "derivatives"),
]
_PET_DERIVATIVES_DIR_CANDIDATES = [
    _resolve_config_path(path, path)
    for path in _split_path_list(os.environ.get("BIDS_UTILS_PET_DERIVATIVES_DIRS"))
] or [
    os.path.join(_RAW_PET_DIR, "derivatives"),
]


def _find_executable(name):
    import shutil

    path = shutil.which(name)
    if path:
        return path

    candidate = os.path.join(os.path.dirname(os.path.realpath(sys.executable)), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate

    home = os.path.expanduser("~")
    for prefix in [
        "/opt/anaconda3", "/opt/miniconda3", "/opt/conda",
        os.path.join(home, "anaconda3"), os.path.join(home, "miniconda3"),
        os.path.join(home, "mambaforge"), os.path.join(home, "miniforge3"),
    ]:
        c = os.path.join(prefix, "bin", name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


_DCM2BIDS_HELPER = _find_executable("dcm2bids_helper")


def _normalize_subject(value):
    s = str(value or "").strip()
    if not s:
        return ""
    if s.lower().startswith("sub-"):
        return s
    return f"sub-{s}"


def _normalize_session(value):
    s = str(value or "").strip()
    if not s:
        return ""
    if s.lower().startswith("ses-"):
        return s
    return f"ses-{s}"


def _detect_petprep_done(subject, session):
    candidate_dirs = []
    for derivatives_dir in _PET_DERIVATIVES_DIR_CANDIDATES:
        candidate_dirs.extend([
            os.path.join(derivatives_dir, "petprep", subject, session),
            os.path.join(derivatives_dir, "petprep", subject, session, "pet"),
        ])

    candidate_files = [
        f"{subject}_{session}_pet.nii.gz",
        f"{subject}_{session}_desc-preproc_pet.nii.gz",
        f"{subject}_{session}_space-T1w_desc-preproc_pet.nii.gz",
    ]
    candidate_prefixes = [f"{subject}_{session}_"]
    candidate_suffixes = ["_pet.nii.gz", "_desc-preproc_pet.nii.gz"]

    for base_dir in candidate_dirs:
        for filename in candidate_files:
            if os.path.isfile(os.path.join(base_dir, filename)):
                return True

        if os.path.isdir(base_dir):
            try:
                for entry in os.listdir(base_dir):
                    lowered = entry.lower()
                    if not lowered.endswith(".nii.gz"):
                        continue
                    if not any(lowered.startswith(prefix.lower()) for prefix in candidate_prefixes):
                        continue
                    if any(lowered.endswith(suffix.lower()) for suffix in candidate_suffixes):
                        return True
            except OSError:
                pass

    return False


def _auto_detect_statuses(headers, rows):
    if not headers or not rows:
        return []

    header_idx = {str(h).strip().lower(): i for i, h in enumerate(headers)}
    subject_i = next((header_idx[k] for k in ("subject", "sub", "participant_id") if k in header_idx), None)
    session_i = next((header_idx[k] for k in ("session", "ses", "visit") if k in header_idx), None)

    if subject_i is None or session_i is None:
        return []

    out = []
    for row_index, row in enumerate(rows):
        subject_raw = row[subject_i] if subject_i < len(row) else ""
        session_raw = row[session_i] if session_i < len(row) else ""
        subject = _normalize_subject(subject_raw)
        session = _normalize_session(session_raw)

        if not subject or not session:
            out.append({"row_index": row_index, "bids_done": False, "petprep_done": False})
            continue

        bids_pet_path = os.path.join(_RAW_PET_DIR, subject, session, "pet", f"{subject}_{session}_pet.nii.gz")
        bids_done = os.path.isfile(bids_pet_path)
        petprep_done = _detect_petprep_done(subject, session)

        out.append({"row_index": row_index, "bids_done": bids_done, "petprep_done": petprep_done})

    return out


def _existing_csv_derivatives_dirs():
    return [d for d in _CSV_DERIVATIVES_DIR_CANDIDATES if os.path.isdir(d)]


def _is_top_level_file_in_dir(file_path, parent_dir):
    return os.path.dirname(os.path.realpath(file_path)) == os.path.realpath(parent_dir)


def _resolve_top_level_derivatives_csv_path(rel_path):
    rel_path = rel_path.lstrip("/")
    full_path = os.path.realpath(os.path.join(PROJECT_ROOT, rel_path))
    allowed = os.path.realpath(PROJECT_ROOT)
    if not (full_path == allowed or full_path.startswith(allowed + os.sep)):
        return None
    if not full_path.lower().endswith(".csv"):
        return None

    for derivatives_dir in _existing_csv_derivatives_dirs():
        if _is_top_level_file_in_dir(full_path, derivatives_dir):
            return full_path
    return None


def _resolve_project_csv_path(rel_path):
    full_path = _resolve_project_path(rel_path)
    if not full_path or not full_path.lower().endswith(".csv"):
        return None
    return full_path


def _resolve_project_path(rel_path):
    allowed = os.path.realpath(PROJECT_ROOT)
    rel_path = str(rel_path or "").strip()
    if os.path.isabs(rel_path):
        full_path = os.path.realpath(rel_path)
    else:
        full_path = os.path.realpath(os.path.join(PROJECT_ROOT, rel_path.lstrip("/")))
    if not (full_path == allowed or full_path.startswith(allowed + os.sep)):
        return None
    return full_path


def _get_default_raw_pet_path():
    if not os.path.isdir(_RAW_PET_HELPER_DIR):
        return None, "No PET data found in path"

    entries = sorted(
        e for e in os.listdir(_RAW_PET_HELPER_DIR)
        if os.path.isdir(os.path.join(_RAW_PET_HELPER_DIR, e))
    )
    if not entries:
        return None, "No PET data found in path"

    first = entries[0]
    first_abs = os.path.join(_RAW_PET_HELPER_DIR, first)

    if re.match(r"^\d+_\d{8}_\d{6}$", first):
        return os.path.relpath(first_abs, PROJECT_ROOT), None

    if re.match(r"^sub-", first):
        ses_entries = sorted(
            s for s in os.listdir(first_abs)
            if s.startswith("ses-") and os.path.isdir(os.path.join(first_abs, s))
        )
        if ses_entries:
            ses_abs = os.path.join(first_abs, ses_entries[0])
            return os.path.relpath(ses_abs, PROJECT_ROOT), None

    return os.path.relpath(first_abs, PROJECT_ROOT), None


def _get_default_petprep_html_path():
    petprep_dir = os.path.realpath(_DEFAULT_PETPREP_HTML_DIR)
    if os.path.isdir(petprep_dir):
        return os.path.relpath(petprep_dir, PROJECT_ROOT), None
    return os.path.relpath(petprep_dir, PROJECT_ROOT), "PETPrep derivatives folder not found"


def _handle_run_dcm2bids_helper_pet(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing path parameter")
        return

    full_path = _resolve_project_path(rel_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return

    pet_root = os.path.realpath(_RAW_PET_HELPER_DIR)
    if not (full_path == pet_root or full_path.startswith(pet_root + os.sep)):
        h.send_error(403, "Path must be inside raw/pet for PET helper")
        return

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.end_headers()

    if not os.path.isdir(full_path):
        _emit_sse(h, {"error": "invalid_path"})
        return

    if not _DCM2BIDS_HELPER:
        _emit_sse(h, {"line": "Error: dcm2bids_helper not found. Is it installed in this Python environment?"})
        _emit_sse(h, {"done": True, "returncode": 1})
        return

    # dcm2bids >= 3.x does not rerun helper conversion if tmp_dcm2bids exists
    # unless --force_dcm2bids is supplied. We keep outputs by default and force
    # rerun via CLI, while optional UI force also clears temp output directory.
    force = params.get("force", ["0"])[0] == "1"
    tmp_dir = os.path.join(_OUTPUT_DIR, "tmp_dcm2bids")
    if force and os.path.isdir(tmp_dir):
        try:
            shutil.rmtree(tmp_dir)
        except OSError as exc:
            _emit_sse(h, {"line": f"Warning: could not clear previous helper temp directory: {exc}"})

    cmd = [_DCM2BIDS_HELPER, "-d", full_path, "-o", _OUTPUT_DIR, "--force_dcm2bids"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                _emit_sse(h, {"line": _strip_ansi(line.rstrip("\n"))})
            proc.wait(timeout=300)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            _emit_sse(h, {"line": "Error: Process timed out (5 minutes)"})
            rc = 1
    except FileNotFoundError:
        _emit_sse(h, {"line": f"Error: could not launch {_DCM2BIDS_HELPER}"})
        rc = 1
    except Exception as exc:
        _emit_sse(h, {"line": f"Error: {exc}"})
        rc = 1

    _emit_sse(h, {"done": True, "returncode": rc})


def _find_derivatives_csv_files(max_count=200):
    csv_paths = []
    seen = set()

    for derivatives_dir in _existing_csv_derivatives_dirs():
        try:
            files = sorted(os.listdir(derivatives_dir))
        except OSError:
            continue
        for filename in files:
            abs_path = os.path.join(derivatives_dir, filename)
            if not os.path.isfile(abs_path):
                continue
            if not filename.lower().endswith(".csv"):
                continue

            rel_path = os.path.relpath(abs_path, PROJECT_ROOT)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            csv_paths.append(rel_path)
            if len(csv_paths) >= max_count:
                return csv_paths

    return csv_paths


def _find_completed_pet_sessions_files(max_count=200):
    csv_paths = []
    seen = set()

    for derivatives_dir in _existing_csv_derivatives_dirs():
        try:
            files = sorted(os.listdir(derivatives_dir))
        except OSError:
            continue

        for filename in files:
            abs_path = os.path.join(derivatives_dir, filename)
            if not os.path.isfile(abs_path):
                continue
            if not filename.lower().endswith(".csv"):
                continue
            if "completed_pet_sessions" not in filename.lower():
                continue

            rel_path = os.path.relpath(abs_path, PROJECT_ROOT)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            csv_paths.append(rel_path)
            if len(csv_paths) >= max_count:
                return csv_paths

    return csv_paths


def _find_transfer_log_files(max_count=200):
    csv_paths = []
    seen = set()
    utils_dir = os.path.join(PROJECT_ROOT, "utils")

    if not os.path.isdir(utils_dir):
        return csv_paths

    for root, _dirs, files in os.walk(utils_dir):
        for filename in sorted(files):
            if not filename.lower().endswith("transfer_log.csv"):
                continue
            abs_path = os.path.join(root, filename)
            if not os.path.isfile(abs_path):
                continue

            rel_path = os.path.relpath(abs_path, PROJECT_ROOT)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            csv_paths.append(rel_path)
            if len(csv_paths) >= max_count:
                return csv_paths

    csv_paths.sort()
    return csv_paths



def _find_petprep_container_files(max_count=200):
    container_root = "/scratch/singularityContainers"
    matches = []

    if not os.path.isdir(container_root):
        return matches

    for root, _dirs, files in os.walk(container_root):
        for filename in sorted(files):
            if "petprep" not in filename.lower():
                continue
            abs_path = os.path.join(root, filename)
            if not os.path.isfile(abs_path):
                continue
            matches.append(abs_path)
            if len(matches) >= max_count:
                return sorted(matches)

    return sorted(matches)


def _petprep_command_path():
    return os.path.join(PROJECT_ROOT, "utils", "petprep_command.txt")


def _petprep_run_log_path():
    return os.path.join(PROJECT_ROOT, "utils", "petprep_run.log")


def _petprep_run_pid_path():
    return os.path.join(PROJECT_ROOT, "utils", "petprep_run.pid")


def _pid_is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail_text_file(path, max_lines=250):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "".join(lines)


def _handle_get_petprep_container_files(h, params):
    h._send_json({"container_files": _find_petprep_container_files()})


def _handle_save_petprep_command(h, body):
    command = str(body.get("command", "")).rstrip()
    if not command:
        h.send_error(400, "Missing command")
        return

    target = _petprep_command_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(command)
            if not command.endswith("\n"):
                f.write("\n")
    except Exception:
        h.send_error(500, "Failed to save PETPrep command")
        return

    h._send_json({"ok": True, "path": os.path.relpath(target, PROJECT_ROOT)})


def _handle_load_petprep_command(h, params):
    target = _petprep_command_path()
    if not os.path.isfile(target):
        h.send_error(404, "PETPrep command not found")
        return

    try:
        with open(target, "r", encoding="utf-8") as f:
            command = f.read().strip()
    except Exception:
        h.send_error(500, "Failed to read PETPrep command")
        return

    h._send_json({"command": command, "path": os.path.relpath(target, PROJECT_ROOT)})


def _handle_run_petprep_command(h, body):
    command = str(body.get("command", "")).rstrip()
    if not command:
        h.send_error(400, "Missing command")
        return

    script_path = os.path.join(PROJECT_ROOT, "utils", "petprep_run.sh")
    log_path = _petprep_run_log_path()
    pid_path = _petprep_run_pid_path()
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    try:
        with open(script_path, "w", encoding="utf-8", newline="") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write(command)
            if not command.endswith("\n"):
                f.write("\n")
        os.chmod(script_path, 0o755)
    except Exception:
        h.send_error(500, "Failed to write run script")
        return

    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(["bash", script_path], stdout=lf, stderr=lf, cwd=PROJECT_ROOT)
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(p.pid) + "\n")
    except Exception:
        h.send_error(500, "Failed to start PETPrep command")
        return

    h._send_json({"ok": True, "pid": p.pid, "log": os.path.relpath(log_path, PROJECT_ROOT)})


def _handle_get_petprep_run_log(h, params):
    log_path = _petprep_run_log_path()
    pid_path = _petprep_run_pid_path()
    pid = None
    running = False
    if os.path.isfile(pid_path):
        try:
            with open(pid_path, "r", encoding="utf-8") as f:
                pid_text = f.read().strip()
            if pid_text:
                pid = int(pid_text)
                running = _pid_is_running(pid)
        except Exception:
            pid = None
            running = False

    content = _tail_text_file(log_path, max_lines=250)
    h._send_json({"pid": pid, "running": running, "content": content, "log": os.path.relpath(log_path, PROJECT_ROOT)})


def _get_csv_browser_config():
    found_derivatives_dir = bool(_existing_csv_derivatives_dirs())
    if not found_derivatives_dir:
        return {
            "default_csv": None,
            "warning": "No derivatives directory found in expected locations.",
            "csv_files": [],
        }

    csv_files = _find_derivatives_csv_files()
    if not csv_files:
        return {
            "default_csv": None,
            "warning": "No CSV files found directly inside derivatives.",
            "csv_files": [],
        }

    return {
        "default_csv": csv_files[0],
        "warning": None,
        "csv_files": csv_files,
    }


def _iter_raw_pet_overview_lines(base_dir):
    found_any = False
    try:
        entries = sorted(os.listdir(base_dir))
    except OSError:
        entries = []

    for filename in entries:
        if not filename.lower().endswith(".html"):
            continue

        file_path = os.path.join(base_dir, filename)
        if not os.path.isfile(file_path):
            continue

        found_any = True
        rel_path = os.path.relpath(file_path, PROJECT_ROOT)
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = -1
        yield {
            "file": filename,
            "rel_path": rel_path,
            "size": size,
        }

    if not found_any:
        yield "No .html files found."


def _emit_sse(h, obj):
    h.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
    h.wfile.flush()


def _rewrite_html_asset_links(html_text, rel_html_path, auth_token):
    """Rewrite relative src/href links so assets resolve via /open-file."""
    rel_dir = os.path.dirname(rel_html_path)

    def _replace(match):
        attr = match.group(1)
        quote_char = match.group(2)
        url = (match.group(3) or "").strip()
        lower = url.lower()

        if (
            not url
            or lower.startswith(("http://", "https://", "data:", "javascript:", "mailto:"))
            or url.startswith(("#", "/"))
        ):
            return match.group(0)

        target_rel = os.path.normpath(os.path.join(rel_dir, url)).replace("\\", "/")
        target_full = _resolve_project_path(target_rel)
        if not target_full or not os.path.isfile(target_full):
            return match.group(0)

        open_rel = quote(target_rel.lstrip("/"), safe="/")
        rewritten = f"/open-file?path={open_rel}&token={quote(auth_token)}"
        return f"{attr}={quote_char}{rewritten}{quote_char}"

    return re.sub(r'(src|href)\s*=\s*(["\'])([^"\']+)\2', _replace, html_text, flags=re.IGNORECASE)


def _handle_get_config(h, params):
    default_path, warning = _get_default_raw_pet_path()
    html_default_path, html_warning = _get_default_petprep_html_path()
    h._send_json({
        "project_root": PROJECT_ROOT,
        "default_path": default_path,
        "warning": warning,
        "html_default_path": html_default_path,
        "html_warning": html_warning,
    })


def _handle_get_helper_summary(h, params):
    rows = config_builder.read_helper_jsons()
    helper_dir = os.path.join(_OUTPUT_DIR, "tmp_dcm2bids", "helper")
    helper_files = []
    helper_nii_files = []
    if os.path.isdir(helper_dir):
        try:
            helper_files = sorted(f for f in os.listdir(helper_dir) if f.endswith('.json'))
            helper_nii_files = sorted(f for f in os.listdir(helper_dir) if f.endswith('.nii.gz'))
        except OSError:
            helper_files = []
            helper_nii_files = []
    h._send_json({
        "rows": rows,
        "debug": {
            "helper_dir": helper_dir,
            "helper_file_count": len(helper_files),
            "helper_files_preview": helper_files[:20],
            "helper_nii_file_count": len(helper_nii_files),
            "helper_nii_files_preview": helper_nii_files[:20],
        },
    })


def _handle_get_bids_config(h, params):
    cfg = config_builder.load_config()
    h._send_json({"config": cfg})


def _handle_discover_sessions(h, params):
    dicom_root = params.get("dicom_root", [None])[0]
    if not dicom_root:
        h.send_error(400, "Missing dicom_root parameter")
        return

    full_root = _resolve_project_path(dicom_root)
    if not full_root:
        h.send_error(403, "Path outside project root")
        return

    sessions = bids_runner.discover_sessions(full_root)
    h._send_json({"sessions": sessions})


def _handle_get_recode_table(h, params):
    h._send_json({"recode": config_builder.load_recode_table()})


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
        rs = str(rec.get("recoded_session") or "").strip()
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
        output_rel = body.get("output_dir")
        config_rel = body.get("config_file")
        selected = body.get("sessions")
        max_workers = int(body.get("max_workers", 8))
        clobber = bool(body.get("clobber", False))
    except (TypeError, ValueError):
        h.send_error(400, "Invalid parameters")
        return

    if not all([dicom_root_rel, output_rel, config_rel]):
        h.send_error(400, "Missing required parameters")
        return

    max_workers = max(1, min(max_workers, 24))

    dicom_root = _resolve_project_path(dicom_root_rel)
    output_dir = _resolve_project_path(output_rel)
    config_file = _resolve_project_path(config_rel)
    if not dicom_root or not output_dir or not config_file:
        h.send_error(403, "Path outside project root")
        return

    if not os.path.isfile(config_file):
        h._send_json({"error": "config_not_found"})
        return

    all_sessions = bids_runner.discover_sessions(dicom_root)
    if selected:
        selected_set = set(selected)
        sessions = [s for s in all_sessions if s["label"] in selected_set]
    else:
        sessions = all_sessions
    
    # Enrich sessions with actual DICOM metadata for config matching
    sessions = bids_runner.enrich_sessions_with_metadata(sessions)

    recode = body.get("recode") or {}
    if isinstance(recode, dict) and recode:
        recoded = []
        for s in sessions:
            rec = recode.get(s["label"], {})
            rp = str(rec.get("recoded_participant") or "").strip()
            rs = str(rec.get("recoded_session") or "").strip()
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
        job_id = pet2bids_runner.start_conversion(
            sessions, dicom_root, output_dir, config_file, max_workers, clobber
        )
        h._send_json({"job_id": job_id})
    except RuntimeError as exc:
        h._send_json({"error": str(exc)})


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

    try:
        for entry in pet2bids_runner.stream_job(job_id):
            _emit_sse(h, entry)
            if entry.get("type") == "done":
                break
    except (BrokenPipeError, ConnectionResetError):
        pass


def _handle_save_bids_config(h, body):
    if not isinstance(body, dict) or "descriptions" not in body:
        h.send_error(400, "Invalid config: missing 'descriptions'")
        return

    descriptions = body.get("descriptions")
    if isinstance(descriptions, list):
        for desc in descriptions:
            if not isinstance(desc, dict):
                continue
            custom_entities = desc.get("custom_entities")
            if isinstance(custom_entities, list):
                desc["custom_entities"] = [
                    item for item in custom_entities
                    if not str(item or "").strip().casefold().startswith("desc-")
                ]
            desc.pop("desc", None)

    try:
        config_builder.save_config(body)
        h._send_json({"ok": True})
    except Exception as exc:
        h.send_error(500, f"Failed to save config: {exc}")


def _handle_raw_pet_overview(h, params):
    rel_path = params.get("path", [None])[0] or os.path.relpath(_DEFAULT_PETPREP_HTML_DIR, PROJECT_ROOT)
    petprep_root = os.path.realpath(_DEFAULT_PETPREP_HTML_DIR)

    full_path = _resolve_project_path(rel_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return
    if not (full_path == petprep_root or full_path.startswith(petprep_root + os.sep)):
        h.send_error(403, "Path must be inside BIDS/derivatives/petprep")
        return

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "keep-alive")
    h.end_headers()

    if not os.path.isdir(full_path):
        _emit_sse(h, {"error": "invalid_path"})
        return

    try:
        for item in _iter_raw_pet_overview_lines(full_path):
            if isinstance(item, str):
                _emit_sse(h, {"line": item})
                continue

            size = item.get("size", -1)
            size_text = f"{size} B" if size >= 0 else "size unknown"
            open_rel = quote(item["rel_path"], safe="/")
            open_url = f"/open-file?path={open_rel}&token={quote(h.server._auth_token)}"
            _emit_sse(h, {
                "file": item["file"],
                "open_url": open_url,
                "size_text": size_text,
            })
        rc = 0
    except Exception as exc:
        _emit_sse(h, {"line": f"Error while generating overview: {exc}"})
        rc = 1

    _emit_sse(h, {"done": True, "returncode": rc})


def _handle_open_html(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing path parameter")
        return

    full_path = _resolve_project_path(rel_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return
    if not full_path.lower().endswith(".html"):
        h.send_error(400, "Only .html files are allowed")
        return
    if not os.path.isfile(full_path):
        h.send_error(404, "File not found")
        return

    open_rel = quote(rel_path.lstrip("/"), safe="/")
    location = f"/open-file?path={open_rel}&token={quote(h.server._auth_token)}"
    h.send_response(302)
    h.send_header("Location", location)
    h.end_headers()


def _handle_open_file(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing file path")
        return

    rel_path = unquote(rel_path)
    full_path = _resolve_project_path(rel_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return
    if not os.path.isfile(full_path):
        h.send_error(404, "File not found")
        return

    content_type, _ = mimetypes.guess_type(full_path)
    if not content_type:
        content_type = "application/octet-stream"

    try:
        if content_type == "text/html":
            with open(full_path, "r", encoding="utf-8") as f:
                html_text = f.read()
            html_text = _rewrite_html_asset_links(html_text, rel_path, h.server._auth_token)
            body = html_text.encode("utf-8")
        else:
            with open(full_path, "rb") as f:
                body = f.read()
    except OSError:
        h.send_error(500, "Failed to read file")
        return

    if content_type.startswith("text/"):
        content_type += "; charset=utf-8"

    h.send_response(200)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def _handle_get_csv_config(h, params):
    cfg = _get_csv_browser_config()
    h._send_json({
        "project_root": PROJECT_ROOT,
        "default_csv": cfg["default_csv"],
        "warning": cfg["warning"],
        "csv_files": cfg["csv_files"],
    })


def _handle_get_completed_sessions_files(h, params):
    completed_files = _find_completed_pet_sessions_files()
    h._send_json({"completed_files": completed_files})


def _handle_get_transfer_log_files(h, params):
    transfer_log_files = _find_transfer_log_files()
    h._send_json({"transfer_log_files": transfer_log_files})


def _handle_get_csv(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing path parameter")
        return

    full_path = _resolve_project_csv_path(rel_path)
    if not full_path:
        h.send_error(400, "Path must be a .csv within the project root")
        return
    if not os.path.isfile(full_path):
        h.send_error(404, "File not found")
        return

    rows = []
    try:
        with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
    except UnicodeDecodeError:
        h.send_error(400, "CSV must be UTF-8 encoded")
        return
    except OSError:
        h.send_error(500, "Failed to read file")
        return

    if not rows:
        h._send_json({
            "path": os.path.relpath(full_path, PROJECT_ROOT),
            "headers": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "auto_detected": [],
        })
        return

    # For source CSVs, always treat the first row as the header row and the
    # remaining rows as data. The client is responsible for choosing which
    # columns to display from those headers.
    headers = rows[0]
    data_rows = rows[1:]

    max_rows = 1000
    truncated = len(data_rows) > max_rows
    if truncated:
        data_rows = data_rows[:max_rows]

    auto_detected = _auto_detect_statuses(headers, data_rows)

    h._send_json({
        "path": os.path.relpath(full_path, PROJECT_ROOT),
        "headers": headers,
        "rows": data_rows,
        "row_count": len(rows) - 1,
        "truncated": truncated,
        "auto_detected": auto_detected,
    })


def _handle_load_target_file(h, params):
    rel_path = params.get("path", [None])[0]
    if not rel_path:
        h.send_error(400, "Missing path parameter")
        return

    full_path = _resolve_top_level_derivatives_csv_path(rel_path)
    if not full_path:
        h.send_error(400, "Path must be a .csv directly in derivatives")
        return
    if not os.path.isfile(full_path):
        h.send_error(404, "File not found")
        return

    rows = []
    try:
        with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
    except UnicodeDecodeError:
        h.send_error(400, "CSV must be UTF-8 encoded")
        return
    except OSError:
        h.send_error(500, "Failed to read file")
        return

    if not rows:
        h._send_json({
            "path": os.path.relpath(full_path, PROJECT_ROOT),
            "headers": [],
            "rows": [],
            "row_count": 0,
            "auto_detected": [],
        })
        return

    def _looks_like_header(row):
        if not row:
            return False
        tokens = {str(c).strip().lower() for c in row}
        common = {"subject", "sub", "participant_id", "id", "session", "ses", "visit", "bids", "petprep"}
        return any(t in common for t in tokens)

    if _looks_like_header(rows[0]):
        headers = rows[0]
        data_rows = rows[1:]
    else:
        max_cols = max((len(r) for r in rows), default=0)
        synthesized = []
        if max_cols >= 1:
            synthesized.append("subject")
        if max_cols >= 2:
            synthesized.append("session")
        for i in range(3, max_cols + 1):
            synthesized.append(f"col{i}")
        headers = synthesized
        data_rows = rows

    h._send_json({
        "path": os.path.relpath(full_path, PROJECT_ROOT),
        "headers": headers,
        "rows": data_rows,
        "row_count": len(data_rows),
        "auto_detected": _auto_detect_statuses(headers, data_rows),
    })


def _read_json_body(h):
    content_length = int(h.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return None
    raw = h.rfile.read(content_length)
    return json.loads(raw.decode("utf-8"))


def _handle_update_csv_cell(h, body):
    rel_path = str(body.get("path", "")).strip()
    column = str(body.get("column", "")).strip()
    value = str(body.get("value", "")).strip()
    row_index = body.get("row_index")

    if not rel_path or not column or row_index is None:
        h.send_error(400, "Missing required fields: path, row_index, column")
        return
    if not isinstance(row_index, int) or row_index < 0:
        h.send_error(400, "row_index must be a non-negative integer")
        return

    full_path = _resolve_top_level_derivatives_csv_path(rel_path)
    if not full_path:
        h.send_error(400, "Path must be a .csv directly in derivatives")
        return
    if not os.path.isfile(full_path):
        h.send_error(404, "File not found")
        return

    try:
        with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
            rows = [row for row in csv.reader(f)]
    except Exception:
        h.send_error(500, "Failed to read CSV")
        return

    if not rows:
        h.send_error(400, "CSV is empty")
        return

    headers = rows[0]
    if column not in headers:
        h.send_error(400, "Column not found")
        return

    data_row_offset = row_index + 1
    if data_row_offset >= len(rows):
        h.send_error(400, "row_index out of range")
        return

    col_index = headers.index(column)
    row = rows[data_row_offset]
    if len(row) <= col_index:
        row.extend([""] * (col_index + 1 - len(row)))
    row[col_index] = value

    try:
        with open(full_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    except Exception:
        h.send_error(500, "Failed to write CSV")
        return

    h._send_json({"ok": True})


def _handle_save_target_file(h, body):
    rel_path = str(body.get("path", "")).strip()
    headers = body.get("headers", [])
    rows = body.get("rows", [])

    if not rel_path:
        h.send_error(400, "Missing path")
        return

    full_path = _resolve_project_csv_path(rel_path)
    if not full_path:
        h.send_error(400, "Path must be a .csv within the project root")
        return

    if not headers and os.path.isfile(full_path):
        try:
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
                first_row = next(csv.reader(f), [])
                headers = first_row if first_row else []
        except Exception:
            pass

    try:
        with open(full_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if headers:
                writer.writerow(headers)
            writer.writerows(rows)
    except Exception:
        h.send_error(500, "Failed to write file")
        return

    h._send_json({"ok": True, "path": os.path.relpath(full_path, PROJECT_ROOT)})


def _handle_reset_target_file(h, body):
    rel_path = str(body.get("path", "")).strip()
    headers = body.get("headers", [])
    rows = body.get("rows", [])

    if not rel_path:
        h.send_error(400, "Missing path")
        return

    full_path = _resolve_project_csv_path(rel_path)
    if not full_path:
        h.send_error(400, "Path must be a .csv within the project root")
        return

    if os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except Exception:
            h.send_error(500, "Failed to delete existing target file")
            return

    try:
        with open(full_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if headers:
                writer.writerow(headers)
            writer.writerows(rows)
    except Exception:
        h.send_error(500, "Failed to recreate target file")
        return

    h._send_json({"ok": True, "path": os.path.relpath(full_path, PROJECT_ROOT)})


def _handle_merge_completed_sessions(h, body):
    target_rel = str(body.get("target_path", "")).strip()
    completed_rel = str(body.get("completed_path", "")).strip()

    if not target_rel or not completed_rel:
        h.send_error(400, "Missing target_path or completed_path")
        return

    target_full = _resolve_project_csv_path(target_rel)
    completed_full = _resolve_top_level_derivatives_csv_path(completed_rel)

    if not target_full:
        h.send_error(400, "Target path must be a .csv within the project root")
        return
    if not completed_full:
        h.send_error(400, "Completed path must be a .csv directly in derivatives")
        return
    if not os.path.isfile(completed_full):
        h.send_error(404, "Completed file not found")
        return

    target_rows = []
    if os.path.isfile(target_full):
        try:
            with open(target_full, "r", encoding="utf-8-sig", newline="") as f:
                target_rows = [row for row in csv.reader(f)]
        except Exception:
            h.send_error(500, "Failed to read target file")
            return

    try:
        with open(completed_full, "r", encoding="utf-8-sig", newline="") as f:
            completed_rows = [row for row in csv.reader(f)]
    except Exception:
        h.send_error(500, "Failed to read completed file")
        return

    if not completed_rows:
        completed_headers = []
        completed_data = []
    else:
        completed_headers = completed_rows[0]
        completed_data = completed_rows[1:]

    if not target_rows:
        if completed_headers:
            target_headers = completed_headers[:]
        else:
            target_headers = ["subject", "session", "status"]
        target_data = []
    else:
        target_headers = target_rows[0]
        target_data = target_rows[1:]

    target_idx = {header: i for i, header in enumerate(target_headers)}
    completed_idx = {header: i for i, header in enumerate(completed_headers)}

    # Support case-insensitive detection of key columns (e.g. "Subject" vs "subject")
    target_idx_lc = {header.lower(): i for i, header in enumerate(target_headers)}
    completed_idx_lc = {header.lower(): i for i, header in enumerate(completed_headers)}

    preferred_key_columns = ["subject", "session"]
    key_columns = []
    for k in preferred_key_columns:
        if k in target_idx_lc and k in completed_idx_lc:
            # use the actual header name from target_headers (preserve original case)
            actual = next((h for h in target_headers if h.lower() == k), None)
            if actual:
                key_columns.append(actual)

    if not key_columns:
        key_columns = [h for h in target_headers if h.lower() in completed_idx_lc][:2]

    def _key_from_row(row, index_map):
        if key_columns:
            return tuple(row[index_map[c]].strip() if index_map[c] < len(row) else "" for c in key_columns)
        return tuple((v or "").strip() for v in row)

    existing_keys = {_key_from_row(row, target_idx) for row in target_data}
    merged_count = 0

    for completed_row in completed_data:
        key = _key_from_row(completed_row, completed_idx)
        if key in existing_keys:
            continue

        new_row = [""] * len(target_headers)
        for col_name, target_i in target_idx.items():
            completed_i = completed_idx.get(col_name)
            if completed_i is not None and completed_i < len(completed_row):
                new_row[target_i] = completed_row[completed_i]

        target_data.append(new_row)
        existing_keys.add(key)
        merged_count += 1

    try:
        with open(target_full, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(target_headers)
            writer.writerows(target_data)
    except Exception:
        h.send_error(500, "Failed to write merged file")
        return

    h._send_json({
        "path": os.path.relpath(target_full, PROJECT_ROOT),
        "headers": target_headers,
        "rows": target_data,
        "row_count": len(target_data),
        "merged_count": merged_count,
    })


def _safe_get_json_body(h):
    try:
        body = _read_json_body(h)
    except Exception:
        h.send_error(400, "Invalid JSON body")
        return None
    if body is None:
        h.send_error(400, "Missing request body")
        return None
    if not isinstance(body, dict):
        h.send_error(400, "Invalid JSON body")
        return None
    return body


def _post_update_csv_cell(h, body):
    _handle_update_csv_cell(h, body)


def _post_save_target_file(h, body):
    _handle_save_target_file(h, body)


def _post_reset_target_file(h, body):
    _handle_reset_target_file(h, body)


def _post_merge_completed_sessions(h, body):
    _handle_merge_completed_sessions(h, body)


def _post_wrapper(fn):
    def _inner(h, body):
        if body is None:
            parsed = _safe_get_json_body(h)
            if parsed is None:
                return
            fn(h, parsed)
            return
        fn(h, body)
    return _inner


def register(get_routes, post_routes):
    get_routes["/pet-get-config"] = _handle_get_config
    get_routes["/pet-get-helper-summary"] = _handle_get_helper_summary
    get_routes["/pet-get-bids-config"] = _handle_get_bids_config
    get_routes["/pet-discover-sessions"] = _handle_discover_sessions
    get_routes["/pet-get-recode-table"] = _handle_get_recode_table
    get_routes["/pet-stream-dcm2bids-job"] = _handle_stream_dcm2bids_job
    get_routes["/run-dcm2bids-helper-pet"] = _handle_run_dcm2bids_helper_pet
    get_routes["/raw-pet-overview"] = _handle_raw_pet_overview
    get_routes["/open-html"] = _handle_open_html
    get_routes["/open-file"] = _handle_open_file
    get_routes["/get-csv-config"] = _handle_get_csv_config
    get_routes["/get-completed-sessions-files"] = _handle_get_completed_sessions_files
    get_routes["/pet-get-transfer-log-files"] = _handle_get_transfer_log_files
    get_routes["/petprep-get-container-files"] = _handle_get_petprep_container_files
    get_routes["/petprep-load-command"] = _handle_load_petprep_command
    get_routes["/petprep-run-log"] = _handle_get_petprep_run_log
    get_routes["/get-csv"] = _handle_get_csv
    get_routes["/load-target-file"] = _handle_load_target_file
    
    post_routes["/petprep-run-command"] = _post_wrapper(_handle_run_petprep_command)

    post_routes["/update-csv-cell"] = _post_wrapper(_post_update_csv_cell)
    post_routes["/pet-save-bids-config"] = _post_wrapper(_handle_save_bids_config)
    post_routes["/pet-save-recode-table"] = _post_wrapper(_handle_save_recode_table)
    post_routes["/pet-run-dcm2bids"] = _post_wrapper(_handle_run_dcm2bids)
    post_routes["/save-target-file"] = _post_wrapper(_post_save_target_file)
    post_routes["/petprep-save-command"] = _post_wrapper(_handle_save_petprep_command)
    post_routes["/reset-target-file"] = _post_wrapper(_post_reset_target_file)
    post_routes["/merge-completed-sessions"] = _post_wrapper(_post_merge_completed_sessions)

#!/usr/bin/env python3
"""MEG-BIDS tab: route registration and request handlers for main server integration.

TAB_METADATA is read by the server at startup to register this tab.
register() is called once to populate the shared GET/POST route dicts.
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# Sibling modules live in the same directory
_TAB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TAB_DIR)
_REPO_ROOT = os.path.realpath(os.path.join(_TAB_DIR, '..', '..'))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import constants as CONSTS

# Import from refactored bidsify package
from bidsify.simple import load_minimal_config, get_default_config, bidsify_simple
from bidsify.conversion_table import load_conversion_table, update_conversion_table, _normalize_table
from bidsify.pipeline import bidsify, update_bids_report, run_qa_analysis
from bidsify.templates import create_dataset_description, create_proc_description
from bidsify.sidecars import update_sidecars
from bidsify.utils import setLogPath


# ── Tab metadata ──────────────────────────────────────────────────────────────
TAB_METADATA = {
    "id": CONSTS.TAB_ID_MEG,
    "label": CONSTS.TAB_LABEL_MEG,
    "order": CONSTS.TAB_ORDER_MEG,
    "requires_path": CONSTS.TAB_REQUIRES_PATH_MEG,
}

# ── Paths ─────────────────────────────────────────────────────────────────────
def _detect_project_root(script_dir):
    """Return /data/projects/<project> for nested repo locations."""
    resolved = os.path.realpath(script_dir)
    match = re.match(r"^(/data/projects/[^/]+)(?:/|$)", resolved)
    if match:
        return match.group(1)
    return os.path.realpath(os.path.join(script_dir, "..", "..", ".."))

_PROJECT_ROOT = _detect_project_root(_TAB_DIR)
_RAW_MEG_DIR = os.path.join(_PROJECT_ROOT, CONSTS.RAW_MEG_SUBDIR)
_LEGACY_RAW_MEG_DIR = os.path.join(_PROJECT_ROOT, CONSTS.RAW_MEG_LEGACY_SUBDIR)
_LOGS_DIR = os.path.join(_PROJECT_ROOT, CONSTS.DEFAULT_LOG_DIR)
_DEFAULT_CONFIG_FILE = CONSTS.MEG_DEFAULT_CONFIG_FILE
_MEG_BIDS_JOBS = {}
_MEG_BIDS_JOBS_LOCK = threading.Lock()


def _update_bids_job(job_id, **fields):
    with _MEG_BIDS_JOBS_LOCK:
        if job_id in _MEG_BIDS_JOBS:
            _MEG_BIDS_JOBS[job_id].update(fields)
            _MEG_BIDS_JOBS[job_id]['updated_at'] = time.time()


def _run_bidsify_job(job_id, config, verbose):
    try:
        _update_bids_job(job_id, state='running', stage='setup', message='Preparing dataset metadata')
        create_dataset_description(config)
        create_proc_description(config)

        _update_bids_job(job_id, stage='loading', message='Loading conversion table')
        conversion_table, conversion_file = load_conversion_table(config, refresh_status=False)

        def _progress_cb(payload):
            _update_bids_job(
                job_id,
                stage=payload.get('stage', ''),
                message=payload.get('message', ''),
                total=int(payload.get('total', 0) or 0),
                processed=int(payload.get('processed', 0) or 0),
                errors=int(payload.get('errors', 0) or 0),
                current_file=payload.get('current_file'),
                last_error=payload.get('last_error'),
                recent_errors=payload.get('recent_errors', [])
            )

        summary = bidsify(
            config,
            conversion_table=conversion_table,
            conversion_file=conversion_file,
            verbose=verbose,
            progress_callback=_progress_cb,
        )

        _update_bids_job(job_id, stage='sidecars', message='Updating sidecars')
        update_sidecars(config)

        if not isinstance(summary, dict):
            summary = {}

        final_counts = summary.get('final_status_counts', {})
        message = (
            "BIDS conversion completed: "
            f"total={summary.get('total', 0)}, "
            f"attempted={summary.get('to_process', 0)}, "
            f"processed_now={summary.get('processed_now', 0)}, "
            f"errors_now={summary.get('errors_now', 0)}, "
            f"processed_total={final_counts.get('processed', 0)}, "
            f"error_total={final_counts.get('error', 0)}"
        )

        _update_bids_job(job_id, state='completed', stage='done', done=True, summary=summary, message=message)
    except Exception as e:
        error_message = str(e)
        fatal_error = {
            'raw_name': '',
            'reason': error_message,
            'exception_type': e.__class__.__name__,
        }
        if verbose:
            import traceback
            fatal_error['traceback'] = traceback.format_exc()
        _update_bids_job(
            job_id,
            state='failed',
            stage='failed',
            done=True,
            error=error_message,
            message='Conversion failed',
            last_error=fatal_error,
            recent_errors=[fatal_error],
        )


def _detect_raw_meg_dir():
    """Return the preferred raw MEG directory, with legacy fallback."""
    if os.path.isdir(_RAW_MEG_DIR):
        return _RAW_MEG_DIR
    if os.path.isdir(_LEGACY_RAW_MEG_DIR):
        return _LEGACY_RAW_MEG_DIR
    return _RAW_MEG_DIR


def _resolve_project_path(rel_path):
    """Resolve a relative path to absolute within project root."""
    allowed = os.path.realpath(_PROJECT_ROOT)
    rel_path = str(rel_path or "").strip()
    if os.path.isabs(rel_path):
        full_path = os.path.realpath(rel_path)
    else:
        full_path = os.path.realpath(os.path.join(_PROJECT_ROOT, rel_path.lstrip("/")))
    if not (full_path == allowed or full_path.startswith(allowed + os.sep)):
        return None
    return full_path


def _build_runtime_config(client_config=None):
    """Build a runtime config with paths normalized to project-root absolute paths."""
    config = get_default_config()

    if not client_config:
        config.update({
            'Name': os.path.basename(_PROJECT_ROOT),
            'Root': _PROJECT_ROOT,
            'Raw': _detect_raw_meg_dir(),
            'BIDS': _resolve_project_path(CONSTS.DEFAULT_BIDS_DIR),
            'Tasks': [],
            'Conversion_file': _resolve_project_path(CONSTS.MEG_DEFAULT_CONVERSION_FILE),
            'config_file': _DEFAULT_CONFIG_FILE,
            'overwrite': False,
        })
        return config, None

    name = client_config.get('project_name') or os.path.basename(_PROJECT_ROOT)
    raw_dir = client_config.get('raw_dir', CONSTS.RAW_MEG_SUBDIR)
    bids_dir = client_config.get('bids_dir', CONSTS.DEFAULT_BIDS_DIR)
    conversion_file = client_config.get('conversion_file', CONSTS.MEG_DEFAULT_CONVERSION_FILE)
    config_file = client_config.get('config_file', _DEFAULT_CONFIG_FILE)

    raw_path = _resolve_project_path(raw_dir)
    bids_path = _resolve_project_path(bids_dir)
    conversion_path = _resolve_project_path(conversion_file)
    config_file_path = _resolve_project_path(config_file)

    if not raw_path:
        return None, 'Raw directory path is outside project root'
    if not bids_path:
        return None, 'BIDS directory path is outside project root'
    if not conversion_path:
        return None, 'Conversion file path is outside project root'
    if not config_file_path:
        return None, 'Config file path is outside project root'

    config.update({
        'Name': name,
        'Root': _PROJECT_ROOT,
        'Raw': raw_path,
        'BIDS': bids_path,
        'Tasks': client_config.get('tasks', []),
        'Conversion_file': conversion_path,
        'config_file': config_file,
        'overwrite': client_config.get('overwrite', False),
        'Overwrite_conversion': client_config.get('overwrite_conversion', False),
    })
    return config, None


# ── Handler functions ─────────────────────────────────────────────────────────

def _handle_get_config(h, params):
    """Return project configuration for the tab."""
    config_exists = os.path.exists(os.path.join(_PROJECT_ROOT, _DEFAULT_CONFIG_FILE))
    raw_meg_dir = _detect_raw_meg_dir()
    h._send_json({
        "project_root": _PROJECT_ROOT,
        "raw_meg_dir": raw_meg_dir if os.path.isdir(raw_meg_dir) else None,
        "logs_dir": _LOGS_DIR,
        "config_exists": config_exists,
        "default_config": get_default_config(),
    })


def _handle_get_project_root(h, params):
    """Return the detected project root path and project name."""
    project_name = os.path.basename(_PROJECT_ROOT)
    h._send_json({
        "project_root": _PROJECT_ROOT,
        "project_name": project_name,
    })


def _handle_validate_paths(h, body):
    """Validate that paths exist on the filesystem."""
    paths = body.get("paths", [])
    results = {}
    
    for path_info in paths:
        path_id = path_info.get("id", "unknown")
        rel_path = path_info.get("path", "")
        
        full_path = _resolve_project_path(rel_path)
        if full_path:
            exists = os.path.exists(full_path)
            is_dir = os.path.isdir(full_path) if exists else False
            is_file = os.path.isfile(full_path) if exists else False
            results[path_id] = {
                "exists": exists,
                "is_dir": is_dir,
                "is_file": is_file,
                "resolved": full_path,
                "rel_path": rel_path
            }
        else:
            results[path_id] = {
                "exists": False,
                "is_dir": False,
                "is_file": False,
                "resolved": None,
                "rel_path": rel_path,
                "error": "Path outside project root"
            }
    
    h._send_json({"results": results})


def _handle_load_config(h, params):
    """Load a minimal JSON configuration file."""
    config_path = params.get("path", [None])[0]
    if not config_path:
        h.send_error(400, "Missing config path")
        return

    full_path = _resolve_project_path(config_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return

    if not os.path.isfile(full_path):
        h._send_json({"error": "Config file not found"})
        return

    try:
        config = load_minimal_config(full_path)
        h._send_json({"config": config, "path": config_path})
    except Exception as e:
        h._send_json({"error": str(e)})


def _handle_get_conversion_table(h, params):
    """Return the current conversion table."""
    config_path = params.get("config_path", [None])[0]

    if config_path:
        try:
            config = load_minimal_config(_resolve_project_path(config_path))
        except Exception as e:
            h._send_json({"error": f"Failed to load config: {e}"})
            return
    else:
        config = get_default_config()
        config['Raw'] = _detect_raw_meg_dir()
        config['BIDS'] = os.path.join(_PROJECT_ROOT, "BIDS")
        config['Root'] = _PROJECT_ROOT

    try:
        conversion_table, conversion_file = load_conversion_table(config, refresh_status=True)

        # Convert DataFrame to dict for JSON serialization
        table_data = conversion_table.fillna('').to_dict('records') if not conversion_table.empty else []

        h._send_json({
            "table": table_data,
            "file": conversion_file,
            "row_count": len(table_data)
        })
    except Exception as e:
        h._send_json({"error": str(e)})


def _handle_save_conversion_table(h, body):
    """Save the conversion table."""
    import pandas as pd

    table_data = body.get("table", [])
    file_path = body.get("file")

    if not file_path:
        h.send_error(400, "Missing file path")
        return

    full_path = _resolve_project_path(file_path)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return

    try:
        df = pd.DataFrame(table_data)
        df = _normalize_table(df)
        df.to_csv(full_path, sep='\t', index=False)
        h._send_json({"ok": True, "path": file_path, "rows": len(table_data)})
    except Exception as e:
        h.send_error(500, f"Failed to save table: {e}")


def _handle_run_analysis(h, body):
    """Generate/update conversion table (analyze step)."""
    force_scan = body.get("force_scan", False)
    client_config = body.get("config")

    config, config_error = _build_runtime_config(client_config)
    if config_error:
        h._send_json({"error": config_error})
        return

    try:
        conversion_table, conversion_file, run_conversion = update_conversion_table(config, force_scan=force_scan)

        # Save the table
        conversion_table.to_csv(conversion_file, sep='\t', index=False)

        table_data = conversion_table.fillna('').to_dict('records') if not conversion_table.empty else []

        h._send_json({
            "ok": True,
            "table": table_data,
            "file": conversion_file,
            "run_conversion": run_conversion,
            "row_count": len(table_data)
        })
    except Exception as e:
        h._send_json({"error": str(e)})


def _handle_load_conversion_table(h, body):
    """Load existing conversion table using current runtime config."""
    client_config = body.get("config")
    config, config_error = _build_runtime_config(client_config)
    if config_error:
        h._send_json({"error": config_error})
        return

    try:
        conversion_table, conversion_file = load_conversion_table(config, refresh_status=True)

        table_data = conversion_table.fillna('').to_dict('records') if not conversion_table.empty else []

        h._send_json({
            "ok": True,
            "table": table_data,
            "file": conversion_file,
            "row_count": len(table_data)
        })
    except Exception as e:
        h._send_json({"error": str(e)})


def _handle_run_bidsify(h, body):
    """Start asynchronous BIDS conversion job."""
    verbose = body.get("verbose", False)
    client_config = body.get("config")

    config, config_error = _build_runtime_config(client_config)
    if config_error:
        h._send_json({"error": config_error})
        return

    job_id = uuid.uuid4().hex[:12]
    with _MEG_BIDS_JOBS_LOCK:
        _MEG_BIDS_JOBS[job_id] = {
            'job_id': job_id,
            'state': 'queued',
            'stage': 'queued',
            'message': 'Queued',
            'total': 0,
            'processed': 0,
            'errors': 0,
            'current_file': None,
            'done': False,
            'summary': None,
            'error': None,
            'last_error': None,
            'recent_errors': [],
            'updated_at': time.time(),
        }

    worker = threading.Thread(target=_run_bidsify_job, args=(job_id, config, verbose), daemon=True)
    worker.start()

    h._send_json({"ok": True, "job_id": job_id, "message": "BIDS conversion started"})


def _handle_bidsify_progress(h, params):
    """Return current progress for a BIDS conversion job."""
    job_id = params.get("job_id", [None])[0]
    if not job_id:
        h._send_json({"error": "Missing job_id"})
        return

    with _MEG_BIDS_JOBS_LOCK:
        job = _MEG_BIDS_JOBS.get(job_id)

    if not job:
        h._send_json({"error": "Unknown job_id"})
        return

    h._send_json({"ok": True, "job": job})


def _handle_run_report(h, body):
    """Generate BIDS report and QA analysis."""
    client_config = body.get("config")

    config, config_error = _build_runtime_config(client_config)
    if config_error:
        h._send_json({"error": config_error})
        return

    try:
        conversion_table, _ = load_conversion_table(config, refresh_status=False)
        update_bids_report(conversion_table, config)

        # Run QA analysis
        qa_results = run_qa_analysis(config, conversion_table)

        logPath = setLogPath(config)
        results_file = os.path.join(logPath, 'bids_results.json')

        if 'error' not in qa_results and os.path.exists(results_file):
            with open(results_file, 'r') as f:
                report = json.load(f)
            report['QA Analysis'] = {
                'timestamp': qa_results.get('timestamp', datetime.now().isoformat()),
                'summary': qa_results.get('summary', {}),
                'findings': qa_results.get('findings', []),
            }
            with open(results_file, 'w') as f:
                json.dump(report, f, indent=4, allow_nan=False)

        h._send_json({
            "ok": True,
            "report_file": results_file if os.path.exists(results_file) else None,
            "qa_summary": qa_results.get('summary', {}) if 'error' not in qa_results else None
        })
    except Exception as e:
        h._send_json({"error": str(e)})


def _handle_get_report(h, body):
    """Get the BIDS report data."""
    client_config = body.get("config")

    config, config_error = _build_runtime_config(client_config)
    if config_error:
        h._send_json({"error": config_error})
        return

    logPath = setLogPath(config)
    report_file = os.path.join(logPath, 'bids_results.json')

    if not os.path.exists(report_file):
        h._send_json({"error": "No report found"})
        return

    try:
        with open(report_file, 'r') as f:
            report_data = json.load(f)
        h._send_json({"report": report_data})
    except Exception as e:
        h._send_json({"error": f"Failed to read report: {e}"})


def _handle_get_static_js(h, params):
    """Serve the meg-tab.js static file."""
    js_path = os.path.join(_TAB_DIR, "meg-tab.js")
    if not os.path.isfile(js_path):
        h.send_error(404, "JavaScript file not found")
        return
    try:
        with open(js_path, encoding="utf-8") as fh:
            body = fh.read().encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "application/javascript; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    except Exception as e:
        h.send_error(500, f"Failed to read JavaScript file: {e}")


def _handle_get_static_css(h, params):
    """Serve the tab.css static file."""
    css_path = os.path.join(_TAB_DIR, "tab.css")
    if not os.path.isfile(css_path):
        h.send_error(404, "CSS file not found")
        return
    try:
        with open(css_path, encoding="utf-8") as fh:
            body = fh.read().encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "text/css; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    except Exception as e:
        h.send_error(500, f"Failed to read CSS file: {e}")


def _handle_save_config(h, body):
    """Save configuration file to project path."""
    config_data = body.get("config", {})
    config_file = body.get("config_file", _DEFAULT_CONFIG_FILE)

    if not config_file:
        h.send_error(400, "Missing config file path")
        return

    full_path = _resolve_project_path(config_file)
    if not full_path:
        h.send_error(403, "Path outside project root")
        return

    # Ensure parent directory exists
    parent_dir = os.path.dirname(full_path)
    if parent_dir and not os.path.exists(parent_dir):
        try:
            os.makedirs(parent_dir, exist_ok=True)
        except Exception as e:
            h.send_error(500, f"Failed to create directory: {e}")
            return

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2)
        h._send_json({"ok": True, "path": config_file, "full_path": full_path})
    except Exception as e:
        h.send_error(500, f"Failed to save config: {e}")


# ── Registration ───────────────────────────────────────────────────────────────

def register(get_routes, post_routes):
    """Populate get_routes and post_routes with this tab's endpoints."""
    get_routes[CONSTS.MEG_API_GET_CONFIG] = _handle_get_config
    get_routes[CONSTS.MEG_API_GET_PROJECT_ROOT] = _handle_get_project_root
    get_routes[CONSTS.MEG_API_LOAD_CONFIG] = _handle_load_config
    get_routes[CONSTS.MEG_API_GET_CONVERSION_TABLE] = _handle_get_conversion_table
    get_routes[CONSTS.MEG_API_BIDSIFY_PROGRESS] = _handle_bidsify_progress
    get_routes[CONSTS.MEG_ASSET_JS] = _handle_get_static_js
    get_routes[CONSTS.MEG_ASSET_CSS] = _handle_get_static_css

    post_routes[CONSTS.MEG_API_SAVE_CONVERSION_TABLE] = _handle_save_conversion_table
    post_routes[CONSTS.MEG_API_LOAD_CONVERSION_TABLE] = _handle_load_conversion_table
    post_routes[CONSTS.MEG_API_SAVE_CONFIG] = _handle_save_config
    post_routes[CONSTS.MEG_API_RUN_ANALYSIS] = _handle_run_analysis
    post_routes[CONSTS.MEG_API_RUN_BIDSIFY] = _handle_run_bidsify
    post_routes[CONSTS.MEG_API_RUN_REPORT] = _handle_run_report
    post_routes[CONSTS.MEG_API_GET_REPORT] = _handle_get_report
    post_routes[CONSTS.MEG_API_VALIDATE_PATHS] = _handle_validate_paths

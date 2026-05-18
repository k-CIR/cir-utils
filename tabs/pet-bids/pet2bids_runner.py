#!/usr/bin/env python3
"""Run PET BIDS conversions through a configurable backend.

The host-side code keeps the config adapter logic local and delegates the
actual PET conversion to an external command. That command can be a container
entrypoint once the Singularity/Apptainer image is available.
"""
import json
import os
import queue
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import threading

_PROJECT_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ADAPTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_config_adapter.py")
_ADAPTER_SPEC = importlib.util.spec_from_file_location("pet_config_adapter", _ADAPTER_PATH)
pet_config_adapter = importlib.util.module_from_spec(_ADAPTER_SPEC)
_ADAPTER_SPEC.loader.exec_module(pet_config_adapter)

_jobs_lock = threading.Lock()
_active_jobs = {}
_job_counter = 0


def _safe_label(value):
    text = str(value or "").strip()
    if not text:
        return "session"
    out = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def _runtime_home_dir(job_id, label):
    default_base = os.path.join(
        tempfile.gettempdir(),
        "cir-utils-pet2bids-home",
        _safe_label(os.environ.get("USER") or "user"),
    )
    base = os.environ.get(
        "PET2BIDS_RUNTIME_HOME_BASE",
        default_base,
    ).strip()
    base = os.path.realpath(base)
    path = os.path.join(base, f"job-{job_id}", _safe_label(label))
    os.makedirs(path, exist_ok=True)
    cfg = os.path.join(path, ".pet2bidsconfig")
    if not os.path.exists(cfg):
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("\n")
    return path


def _find_executable(name):
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


def _new_job_id():
    global _job_counter
    with _jobs_lock:
        _job_counter += 1
        return str(_job_counter)


def _append_log(job_id, entry):
    with _jobs_lock:
        job = _active_jobs.get(job_id)
        if job:
            job["log"].append(entry)
            job["queue"].put(entry)


def _normalized_bind(value):
    path = os.path.realpath(str(value or "").strip())
    if not path or not os.path.exists(path):
        return None
    return f"{path}:{path}"


def _build_backend_command(plan_file, plan, runtime_home):
    engine = os.environ.get("PET2BIDS_CONTAINER_ENGINE", "").strip()
    image = os.environ.get(
        "PET2BIDS_CONTAINER_IMAGE",
        "/scratch/singularityContainers/pet2bids.sif",
    ).strip()
    entrypoint = os.environ.get(
        "PET2BIDS_CONTAINER_ENTRYPOINT",
        os.path.join(_PROJECT_ROOT, "tabs", "pet-bids", "pet2bids_container_entrypoint.py"),
    ).strip()

    if not image:
        raise RuntimeError(
            "PET2BIDS_CONTAINER_IMAGE is not set. Configure the container image before running PET conversions."
        )

    if engine:
        engine_path = _find_executable(engine)
    else:
        engine_path = _find_executable("apptainer") or _find_executable("singularity")
    if not engine_path:
        raise RuntimeError(
            "No container engine found. Install apptainer/singularity or set PET2BIDS_CONTAINER_ENGINE."
        )

    if not os.path.isfile(image):
        raise RuntimeError(f"PET2BIDS container image not found: {image}")

    bind_specs = []
    seen = set()

    def _add_bind(path):
        spec = _normalized_bind(path)
        if spec and spec not in seen:
            seen.add(spec)
            bind_specs.extend(["-B", spec])

    _add_bind(_PROJECT_ROOT)
    _add_bind(runtime_home)
    launch = plan.get("launch") if isinstance(plan, dict) else {}
    session = plan.get("session") if isinstance(plan, dict) else {}
    _add_bind((launch or {}).get("dicom_root"))
    _add_bind((launch or {}).get("output_dir"))
    _add_bind((session or {}).get("folder"))
    cmd = [
        engine_path,
        "exec",
        "--cleanenv",
    ]
    cmd += [
        "--home",
        f"{runtime_home}:{runtime_home}",
        *bind_specs,
        image,
        "python",
        "-s",
        entrypoint,
        "--plan-file",
        plan_file,
    ]

    extra_bind = os.environ.get("PET2BIDS_CONTAINER_BINDS", "").strip()
    if extra_bind:
        # Rebuild command with extra bind mounts. Use ';' to separate bind specs.
        bind_args = []
        for item in extra_bind.split(";"):
            item = item.strip()
            if item:
                bind_args.extend(["-B", item])
        cmd = [engine_path, "exec", "--cleanenv"]
        cmd += [
            "--home",
            f"{runtime_home}:{runtime_home}",
            *bind_args,
            *bind_specs,
            image,
            "python",
            "-s",
            entrypoint,
            "--plan-file",
            plan_file,
        ]

    return cmd


def start_conversion(sessions, dicom_root, output_dir, config_file, max_workers=8, clobber=False):
    if not sessions:
        raise RuntimeError("No PET sessions selected")

    job_id = _new_job_id()
    log_queue = queue.Queue()

    with _jobs_lock:
        _active_jobs[job_id] = {
            "status": "running",
            "log": [],
            "queue": log_queue,
        }

    thread = threading.Thread(
        target=_run_pool,
        args=(job_id, sessions, dicom_root, output_dir, config_file, max_workers, clobber),
        daemon=True,
    )
    thread.start()
    return job_id


def _run_pool(job_id, sessions, dicom_root, output_dir, config_file, max_workers, clobber):
    sem = threading.Semaphore(max(1, int(max_workers) if max_workers else 1))
    threads = []

    def _worker(sess):
        with sem:
            _run_single(job_id, sess, dicom_root, output_dir, config_file, clobber)

    for sess in sessions:
        t = threading.Thread(target=_worker, args=(sess,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    _append_log(job_id, {"type": "done"})
    with _jobs_lock:
        _active_jobs[job_id]["status"] = "done"


def _run_single(job_id, sess, dicom_root, output_dir, config_file, clobber):
    label = sess.get("label", "")
    runtime_home = _runtime_home_dir(job_id, label)
    config = pet_config_adapter.load_config(config_file)
    if config is None:
        _append_log(job_id, {"type": "error", "label": label, "text": f"Config not found: {config_file}"})
        _append_log(job_id, {"type": "exit", "label": label, "returncode": 1})
        return

    config["__config_file__"] = config_file
    plan = pet_config_adapter.build_conversion_plan(config, sess, {
        "recoded_participant": sess.get("participant", ""),
        "recoded_session": sess.get("session", ""),
    })
    plan["matched_descriptions"] = plan.get("descriptions", []) or []
    plan["launch"] = {
        "dicom_root": dicom_root,
        "output_dir": output_dir,
        "clobber": bool(clobber),
    }

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        plan_file = fh.name

    cmd = None
    rc = 0
    try:
        cmd = _build_backend_command(plan_file, plan, runtime_home)
        _append_log(job_id, {"type": "start", "label": label, "cmd": " ".join(cmd)})
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for line in proc.stdout:
                _append_log(job_id, {"type": "line", "label": label, "text": line.rstrip("\n")})
            proc.wait(timeout=600)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            _append_log(job_id, {"type": "line", "label": label, "text": "ERROR: timed out after 10 minutes"})
            rc = 1
    except Exception as exc:
        _append_log(job_id, {"type": "line", "label": label, "text": f"ERROR: {exc}"})
        rc = 1
    finally:
        try:
            os.unlink(plan_file)
        except OSError:
            pass

    if cmd is None:
        _append_log(job_id, {"type": "start", "label": label, "cmd": "unavailable"})

    _append_log(job_id, {"type": "exit", "label": label, "returncode": rc})


def stream_job(job_id):
    with _jobs_lock:
        job = _active_jobs.get(job_id)
        if not job:
            yield {"type": "error", "text": "Job not found"}
            return
        backlog = list(job["log"])
        q = job.get("queue")

    for entry in backlog:
        yield entry
        if entry.get("type") == "done":
            return

    if q is None:
        return

    while True:
        try:
            entry = q.get(timeout=30)
            yield entry
            if entry.get("type") == "done":
                break
        except queue.Empty:
            yield {"type": "keepalive"}

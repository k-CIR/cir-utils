#!/usr/bin/env python3
"""Container entrypoint for PET BIDS conversion.

The host-side runner passes a normalized conversion plan to this script once
the PET container image exists. The actual PET execution API still needs to be
filled in against the pinned pypet2bids version inside that image.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import site
import tempfile


def _read_plan(plan_file):
    with open(plan_file, encoding="utf-8") as fh:
        return json.load(fh)


def _ensure_runtime_packages():
    marker = os.path.join(os.path.expanduser("~"), ".cache", "pet2bids_runtime_ready")
    local_bin = os.path.join(site.USER_BASE, "bin")
    os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")

    if os.path.isfile(marker):
        return

    os.makedirs(os.path.dirname(marker), exist_ok=True)
    cmd = [
        "python",
        "-m",
        "pip",
        "install",
        "--user",
        "--no-input",
        "pypet2bids",
        "pydicom",
        "pandas",
        "json-maj",
        "dcm2niix",
    ]
    print("Bootstrapping PET runtime packages...")
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        raise RuntimeError("Failed to install runtime PET packages inside container")

    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("ok\n")


def _norm_text(value):
    return str(value or "").strip()


def _matches_text(expected, actual):
    exp = _norm_text(expected)
    act = _norm_text(actual)
    if not exp:
        return False
    if exp.startswith("/") and exp.endswith("/") and len(exp) > 2:
        try:
            return re.search(exp[1:-1], act, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return exp.casefold() == act.casefold()


def _match_image_type(expected, actual):
    """Check if actual ImageType list contains all expected ImageType items (case-insensitive)."""
    if not expected:
        return True
    expected_items = expected if isinstance(expected, (list, tuple)) else [expected]
    actual_items = actual if isinstance(actual, (list, tuple)) else [actual]
    actual_normalized = [_norm_text(item).casefold() for item in actual_items]
    for exp_item in expected_items:
        exp_norm = _norm_text(exp_item).casefold()
        if exp_norm and exp_norm not in actual_normalized:
            return False
    return True


def _first_expected(criteria, key):
    value = (criteria or {}).get(key)
    if isinstance(value, list):
        for item in value:
            text = _norm_text(item)
            if text:
                return text
        return ""
    return _norm_text(value)


def _target_suffix(matched_desc):
    matched = matched_desc if isinstance(matched_desc, dict) else {}
    suffix = _norm_text(matched.get("suffix"))
    if suffix:
        return suffix
    datatype = _norm_text(matched.get("datatype"))
    if datatype.casefold() == "ct":
        return "ct"
    return "pet"


def _is_ct_target(matched_desc):
    return _target_suffix(matched_desc).casefold() == "ct"


def _resolve_source_dir(src, matched_desc=None):
    src = os.path.realpath(str(src or "").strip())
    if not src or not os.path.isdir(src):
        raise ValueError(f"Source folder does not exist: {src}")

    target = "ct" if _is_ct_target(matched_desc) else "pet"
    criteria = (matched_desc or {}).get("criteria") or {}
    expected_sd = _first_expected(criteria, "SeriesDescription")
    expected_pn = _first_expected(criteria, "ProtocolName")
    expected_it = criteria.get("ImageType")  # ImageType may be a list

    try:
        import pydicom  # type: ignore
    except Exception:
        pydicom = None

    dir_stats = {}
    for root, _, files in os.walk(src):
        if not files:
            continue
        stat = dir_stats.setdefault(root, {
            "dicom_count": 0,
            "sd_match": 0,
            "pn_match": 0,
            "it_match": 0,
            "pt_modality": 0,
            "ct_modality": 0,
            "pt_named_dir": os.path.basename(root).lower() in {"pt", "pet"},
            "ct_named_dir": os.path.basename(root).lower() in {"ct"},
        })
        for fname in files:
            fpath = os.path.join(root, fname)
            if not os.path.isfile(fpath):
                continue
            if pydicom is None:
                # Without pydicom we cannot safely match criteria.
                return None
            try:
                ds = pydicom.dcmread(
                    fpath,
                    stop_before_pixels=True,
                    force=True,
                    specific_tags=["SeriesDescription", "ProtocolName", "Modality", "ImageType"],
                )
            except Exception:
                continue

            stat["dicom_count"] += 1
            if _matches_text(expected_sd, getattr(ds, "SeriesDescription", "")):
                stat["sd_match"] += 1
            if _matches_text(expected_pn, getattr(ds, "ProtocolName", "")):
                stat["pn_match"] += 1
            
            # Check ImageType if specified in criteria
            if expected_it:
                actual_it_raw = getattr(ds, "ImageType", None)
                # Try to iterate; MultiValue from pydicom may not be a list/tuple
                try:
                    actual_it = list(actual_it_raw)
                except TypeError:
                    actual_it = [str(actual_it_raw)] if actual_it_raw else []
                if _match_image_type(expected_it, actual_it):
                    stat["it_match"] += 1
            
            modality = _norm_text(getattr(ds, "Modality", "")).upper()
            if modality in {"PT", "PET"}:
                stat["pt_modality"] += 1
            if modality == "CT":
                stat["ct_modality"] += 1

    if not dir_stats:
        return src if not (expected_sd or expected_pn or expected_it) else None

    def _score(item):
        path, st = item
        matches = st["sd_match"] + st["pn_match"] + st["it_match"]
        has_criteria = bool(expected_sd or expected_pn or expected_it)
        criteria_hit = matches > 0 if has_criteria else False
        modality_hits = st["ct_modality"] if target == "ct" else st["pt_modality"]
        named_dir_hit = st["ct_named_dir"] if target == "ct" else st["pt_named_dir"]
        return (
            1 if criteria_hit else 0,
            matches,
            modality_hits,
            1 if named_dir_hit else 0,
            st["dicom_count"],
            -len(path),
        )

    best_dir, best_stat = max(dir_stats.items(), key=_score)

    # If any specific criteria were provided, require each provided criterion
    # to have at least one matching DICOM in the selected directory. This
    # enforces AND semantics for specified criteria (SeriesDescription,
    # ProtocolName, ImageType) rather than accepting a directory when only
    # a subset of criteria (or modality) match.
    if expected_sd or expected_pn or expected_it:
        if (
            (expected_sd and best_stat["sd_match"] == 0)
            or (expected_pn and best_stat["pn_match"] == 0)
            or (expected_it and best_stat["it_match"] == 0)
        ):
            return None

    print(f"Resolved {target.upper()} source folder: {best_dir}")
    if expected_sd or expected_pn or expected_it:
        it_str = ", ".join(expected_it) if isinstance(expected_it, (list, tuple)) else _norm_text(expected_it)
        print(
            "Selection criteria:",
            f"SeriesDescription={expected_sd or '<none>'}",
            f"ProtocolName={expected_pn or '<none>'}",
            f"ImageType={it_str or '<none>'}",
        )
    return best_dir


def _entity_value(entities, key):
    prefix = f"{key}-"
    for item in entities or []:
        item = str(item or "").strip()
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def _format_subject(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return value if value.startswith("sub-") else f"sub-{value}"


def _format_session(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return value if value.startswith("ses-") else f"ses-{value}"


def _filter_dicoms_by_criteria(src_dir, criteria):
    """Create a temp dir with only DICOMs matching the criteria (SeriesDescription, ProtocolName, ImageType).
    
    Returns the temp directory path, or src_dir if no criteria or filtering isn't possible.
    The caller is responsible for cleaning up the temp directory.
    """
    if not criteria:
        return src_dir

    expected_sd = _first_expected(criteria, "SeriesDescription")
    expected_pn = _first_expected(criteria, "ProtocolName")
    expected_it_raw = criteria.get("ImageType")

    expected_it = None
    if expected_it_raw:
        if isinstance(expected_it_raw, (list, tuple)):
            expected_it = [_norm_text(item).upper() for item in expected_it_raw]
        elif isinstance(expected_it_raw, str):
            expected_it = [_norm_text(item).upper() for item in expected_it_raw.split(",")]
        expected_it = [item for item in expected_it if item]

    if not (expected_sd or expected_pn or expected_it):
        return src_dir

    try:
        import pydicom  # type: ignore
    except Exception:
        return None

    matching_files = []
    checked_count = 0
    dicom_image_types = {}

    for root, _, files in os.walk(src_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                ds = pydicom.dcmread(
                    fpath,
                    stop_before_pixels=True,
                    force=True,
                    specific_tags=["SeriesDescription", "ProtocolName", "ImageType"],
                )
            except Exception:
                continue

            checked_count += 1

            actual_it_raw = getattr(ds, "ImageType", None)
            if isinstance(actual_it_raw, (list, tuple)):
                actual_it_debug = ",".join(str(item) for item in actual_it_raw)
            else:
                actual_it_debug = str(actual_it_raw) if actual_it_raw else "<empty>"

            dicom_image_types[actual_it_debug] = dicom_image_types.get(actual_it_debug, 0) + 1

            sd_match = True
            if expected_sd and not _matches_text(expected_sd, getattr(ds, "SeriesDescription", "")):
                sd_match = False

            pn_match = True
            if expected_pn and not _matches_text(expected_pn, getattr(ds, "ProtocolName", "")):
                pn_match = False

            it_match = True
            if expected_it:
                try:
                    actual_it = [_norm_text(item).upper() for item in actual_it_raw]
                except TypeError:
                    actual_it = [_norm_text(str(actual_it_raw)).upper()] if actual_it_raw else []

                matches = all(exp_item in actual_it for exp_item in expected_it)
                if not matches and checked_count <= 2:
                    print(f"[DEBUG DICOM {checked_count}] expected_it={expected_it}, actual_it={actual_it}, matches={matches}")
                if not matches:
                    it_match = False

            if sd_match and pn_match and it_match:
                matching_files.append(fpath)

    if checked_count == 0:
        print(f"Warning: No DICOM files found in {src_dir}")
        return None

    if not matching_files:
        print(f"Warning: No DICOMs matched the criteria (checked {checked_count} files) in {src_dir}")
        print(f"  Criteria: SD={expected_sd}, PN={expected_pn}, IT={expected_it}")
        if dicom_image_types:
            print(f"  Found ImageTypes in DICOMs: {dicom_image_types}")
        return None

    if len(matching_files) == checked_count:
        return src_dir

    temp_dir = tempfile.mkdtemp(prefix="dcm2niix_filtered_")
    try:
        for fpath in matching_files:
            fname = os.path.basename(fpath)
            link_path = os.path.join(temp_dir, fname)
            os.symlink(fpath, link_path)
        print(f"Created temp DICOM directory with {len(matching_files)} matching files: {temp_dir}")
        return temp_dir
    except Exception as e:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
        print(f"Warning: Failed to create filtered DICOM directory ({e})")
        return None


def _build_destination(output_dir, session, matched_desc=None):
    subject = _format_subject(session.get("participant"))
    ses = _format_session(session.get("session"))
    if not subject:
        raise ValueError("Session participant is missing in conversion plan")

    dest = os.path.join(output_dir, subject)
    if ses:
        dest = os.path.join(dest, ses)
    modality_dir = "ct" if _is_ct_target(matched_desc) else "pet"
    return os.path.join(dest, modality_dir)


def _extract_kwargs(matched_desc):
    sidecar = matched_desc.get("sidecar_changes") or {}
    out = []
    for key, value in sidecar.items():
        out.append(f"{key}={json.dumps(value, ensure_ascii=True)}")
    return out


def _expected_output_basename(session, matched_desc):
    subject = _format_subject(session.get("participant"))
    ses = _format_session(session.get("session"))
    parts = [subject]
    if ses:
        parts.append(ses)

    entities = (matched_desc or {}).get("entities") or []
    for item in entities:
        token = _norm_text(item)
        if token:
            parts.append(token)

    suffix = _target_suffix(matched_desc)
    parts.append(suffix)
    return "_".join(parts)


def _find_existing_output(dest_dir, session, matched_desc):
    if not os.path.isdir(dest_dir):
        return None

    basename = _expected_output_basename(session, matched_desc)
    candidates = [
        basename + ".nii.gz",
        basename + ".nii",
        basename + ".json",
    ]
    for name in candidates:
        if os.path.isfile(os.path.join(dest_dir, name)):
            return name
    return None


def _build_command(plan, matched_desc, clobber=False):
    session = plan.get("session") or {}
    launch = plan.get("launch") or {}
    matched = matched_desc if isinstance(matched_desc, dict) else {}
    entities = matched.get("entities") or []

    src = str(session.get("folder") or "").strip()
    out_root = str(launch.get("output_dir") or "").strip()
    if not src:
        raise ValueError("Session folder missing in conversion plan")
    if not out_root:
        raise ValueError("Output directory missing in conversion plan")

    src = _resolve_source_dir(src, matched)
    if not src:
        raise ValueError("No DICOM series matched the criteria")

    destination = _build_destination(out_root, session, matched)
    os.makedirs(destination, exist_ok=True)

    if _is_ct_target(matched):
        dcm2niix_bin = os.environ.get("DCM2NIIX_PATH") or shutil.which("dcm2niix")
        if not dcm2niix_bin:
            raise RuntimeError("dcm2niix is not available; CT conversion requires dcm2niix")
        
        # Filter DICOMs to only include those matching the criteria
        criteria = matched.get("criteria") or {}
        filtered_src = _filter_dicoms_by_criteria(src, criteria)
        if not filtered_src:
            raise ValueError("No DICOM series matched the criteria")
        
        basename = _expected_output_basename(session, matched)
        cmd = [
            dcm2niix_bin,
            "-b", "y",
            "-z", "y",
        ]
        if clobber:
            cmd.extend(["-w", "1"])
        cmd.extend([
            "-o", destination,
            "-f", basename,
        ])
        dcm2niix_options = plan.get("dcm2niix_options") or []
        if dcm2niix_options:
            cmd.extend([str(item) for item in dcm2niix_options])
        cmd.append(filtered_src)
        return cmd, destination, filtered_src  # Return filtered_src so caller can clean up

    criteria = matched.get("criteria") or {}
    filtered_src = _filter_dicoms_by_criteria(src, criteria)
    if not filtered_src:
        raise ValueError("No DICOM series matched the criteria")

    cmd = [
        "python",
        "-m",
        "pypet2bids.dcm2niix4pet",
        filtered_src,
        "--destination-path",
        destination,
    ]

    trc = _entity_value(entities, "trc")
    run = _entity_value(entities, "run")
    rec = _entity_value(entities, "rec")
    if trc:
        cmd.extend(["--trc", trc])
    if run:
        cmd.extend(["--run", run])
    if rec:
        cmd.extend(["--rec", rec])

    kwargs_args = _extract_kwargs(matched)
    if kwargs_args:
        cmd.append("--kwargs")
        cmd.extend(kwargs_args)

    dcm2niix_options = plan.get("dcm2niix_options") or []
    if dcm2niix_options:
        cmd.append("--dcm2niix-options")
        cmd.extend([str(item) for item in dcm2niix_options])

    return cmd, destination, filtered_src


def main(argv=None):
    parser = argparse.ArgumentParser(description="PET BIDS conversion entrypoint")
    parser.add_argument("--plan-file", required=True, help="Path to a JSON conversion plan")
    args = parser.parse_args(argv)

    plan = _read_plan(args.plan_file)
    session = plan.get("session", {}) if isinstance(plan, dict) else {}
    selected = plan.get("matched_descriptions", []) if isinstance(plan, dict) else []

    descriptions = selected if selected else (plan.get("descriptions", []) if isinstance(plan, dict) else [])
    if not descriptions:
        label = session.get("label", "unknown session")
        print(f"[{label}] No descriptions found in the conversion plan. Skipping this session.")
        return 0

    print(f"PET plan loaded for {session.get('label', 'unknown session')}")
    print(f"Descriptions to evaluate: {len(descriptions)}")

    if os.environ.get("PET2BIDS_DRY_RUN", "").strip():
        print("PET2BIDS_DRY_RUN is set; skipping execution.")
        return 0

    if not os.environ.get("DCM2NIIX_PATH"):
        dcm2niix_path = shutil.which("dcm2niix")
        if dcm2niix_path:
            os.environ["DCM2NIIX_PATH"] = dcm2niix_path
            print(f"Using dcm2niix at {dcm2niix_path}")

    needs_pet_runtime = any(not _is_ct_target(desc) for desc in descriptions)
    if needs_pet_runtime:
        try:
            import pypet2bids  # type: ignore  # noqa: F401
        except Exception:
            allow_bootstrap = os.environ.get("PET2BIDS_ALLOW_BOOTSTRAP", "0").strip().lower() in {"1", "true", "yes"}
            if not allow_bootstrap:
                print(
                    "pypet2bids is not available inside the container. "
                    "Use a fully built image or set PET2BIDS_ALLOW_BOOTSTRAP=1 for temporary bootstrapping.",
                    file=sys.stderr,
                )
                return 1
            try:
                _ensure_runtime_packages()
                import pypet2bids  # type: ignore  # noqa: F401
            except Exception as exc:
                print(f"pypet2bids is not available inside the container: {exc}", file=sys.stderr)
                return 1

    clobber = bool((plan.get("launch") or {}).get("clobber", False))
    failures = 0
    ran_any = False

    for idx, matched in enumerate(descriptions, start=1):
        desc_index = matched.get("index") if isinstance(matched, dict) else None
        suffix = _target_suffix(matched)
        label = f"description #{desc_index}" if desc_index is not None else f"description {idx}"

        try:
            result = _build_command(plan, matched, clobber=clobber)
            if len(result) == 3:
                cmd, destination, filtered_src = result
            else:
                cmd, destination = result
                filtered_src = None
        except ValueError as exc:
            if "No DICOM series matched" in str(exc):
                print(f"{label} ({suffix}): skipped because no matching DICOM series was found")
                continue
            print(f"Failed to build PET command for {label}: {exc}", file=sys.stderr)
            failures += 1
            continue
        except Exception as exc:
            print(f"Failed to build PET command for {label}: {exc}", file=sys.stderr)
            failures += 1
            continue

        existing_output = _find_existing_output(destination, session, matched)
        if not clobber and existing_output:
            print(f"{label} ({suffix}): skipped because output {existing_output} already exists")
            continue

        print(f"{label} ({suffix}): Executing: {' '.join(cmd)}")
        proc = subprocess.run(cmd, text=True)
        ran_any = True
        
        # Clean up temporary DICOM directory if one was created
        if filtered_src and filtered_src.startswith("/tmp"):
            try:
                shutil.rmtree(filtered_src)
            except Exception as e:
                print(f"Warning: Failed to clean up temp directory {filtered_src}: {e}")
        
        if int(proc.returncode) != 0:
            failures += 1

    if failures:
        return 1
    if not ran_any:
        print("No conversions executed; all matched outputs already existed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

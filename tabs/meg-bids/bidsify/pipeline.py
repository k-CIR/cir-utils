import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from glob import glob
from typing import Dict, Any
from os.path import basename, dirname, exists, getsize, join

import mne
import pandas as pd
from bids_validator import BIDSValidator
from mne_bids import BIDSPath, write_raw_bids, get_bids_path_from_fname
from mne_bids import write_meg_calibration, write_meg_crosstalk
from tqdm import tqdm

from .constants import HEADPOS_PATTERNS
from .conversion_table import load_conversion_table, update_conversion_table, _record_processing_success, get_row_staged, _set_staged
from .parsing import bids_path_from_rawname, get_split_file_parts
from .sidecars import add_channel_parameters, copy_eeg_to_meg, update_sidecars
from .templates import create_dataset_description, create_proc_description
from .utils import setLogPath

# Import QA modules
try:
    from .qa_agent import BIDSQAAgent
    from .bids_schema import BIDSSchemaValidator
    QA_AVAILABLE = True
except ImportError:
    QA_AVAILABLE = False

mne.set_log_level('WARNING')


def _cleanup_stale_bids_output(old_bids_path, old_bids_name, new_fpath: str, bids_root: str, verbose: bool = False):
    """Remove a row's previous BIDS output file(s) when reprocessing produced a
    different output path/name (e.g. participant, session, task, run, or other
    entities were edited after the file was first converted).

    write_raw_bids(overwrite=True) only ever overwrites files at the *new*
    entity-derived path. If entities changed since the last conversion, the old
    file(s) live at a different path and are never touched, silently orphaning
    them inside the BIDS dataset. This finds every sibling file that belongs to
    the OLD entity set (any suffix/extension, including split files and
    sidecars such as .json/.tsv) and deletes them, but only ever within
    `bids_root`, and never the file that was just (re)written.

    Returns the list of removed file paths.
    """
    if not old_bids_path or not old_bids_name or pd.isna(old_bids_path) or pd.isna(old_bids_name):
        return []

    old_fpath = os.path.normpath(join(str(old_bids_path), str(old_bids_name)))
    new_fpath_norm = os.path.normpath(str(new_fpath))
    if old_fpath == new_fpath_norm:
        return []  # output path/name unchanged, nothing stale

    bids_root_norm = os.path.normpath(str(bids_root))

    def _inside_root(path):
        try:
            return os.path.commonpath([bids_root_norm, os.path.normpath(path)]) == bids_root_norm
        except ValueError:
            return False  # e.g. different drives on Windows

    if not _inside_root(old_fpath):
        print(f"Skipping stale-output cleanup: {old_fpath} is outside BIDS root {bids_root_norm}")
        return []

    try:
        old_entity_path = get_bids_path_from_fname(old_fpath, check=False)
        old_entity_path.update(suffix=None, extension=None, split=None, check=False)
        stale_candidates = [str(p.fpath) for p in old_entity_path.match(ignore_json=False)]
    except Exception as e:
        print(f"Could not resolve BIDS entities for stale output {old_fpath}, falling back to exact match: {e}")
        stale_candidates = [old_fpath] if exists(old_fpath) else []

    removed = []
    for stale in stale_candidates:
        stale_norm = os.path.normpath(stale)
        if stale_norm == new_fpath_norm or not _inside_root(stale_norm):
            continue
        try:
            os.remove(stale_norm)
            removed.append(stale_norm)
            if verbose:
                print(f"  Removed stale BIDS output: {stale_norm}")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Could not remove stale BIDS output {stale_norm}: {e}")
    return removed


def bidsify(config: dict, conversion_table=None, conversion_file=None, force_scan: bool = False, verbose: bool = False, progress_callback=None):
    """
    Main function to convert raw MEG/EEG data to BIDS format.
    """
    ts = datetime.now().strftime('%Y%m%d')
    path_project = join(config.get('Root', ''), config.get('Name', ''))
    local_path = config.get('Raw', '')
    path_BIDS = config.get('BIDS', '')
    calibration = config.get('Calibration', '')
    crosstalk = config.get('Crosstalk', '')
    logfile = config.get('Logfile', '')
    participant_mapping = join(path_project, config.get('Participants_mapping_file', ''))
    logPath = setLogPath(config)

    def _emit_progress(payload: dict):
        if callable(progress_callback):
            try:
                progress_callback(payload)
            except Exception:
                pass

    if conversion_table is None or conversion_file is None:
        df, conversion_file, _ = update_conversion_table(config, force_scan=force_scan)
    else:
        df = conversion_table

    summary = {
        'total': 0,
        'to_process': 0,
        'initial_status_counts': {},
        'processed_now': 0,
        'errors_now': 0,
        'error_details': [],
        'final_status_counts': {},
        'report_updates': 0,
        'message': ''
    }

    if df.empty or not conversion_file:
        print("Conversion table empty or not defined")
        summary['message'] = 'Conversion table empty or not defined'
        _emit_progress({'stage': 'done', 'message': summary['message'], 'summary': summary})
        return summary

    df = df.where(pd.notnull(df) & (df != ''), None)

    # Only rows explicitly staged (via the "Stage Jobs" step) are processed. Staging
    # is independent of 'status' so a staged row is always (re)processed/overwritten,
    # regardless of whether it was previously 'run', 'processed', or 'error'.
    df['_staged'] = df.apply(get_row_staged, axis=1)
    process_mask = df['_staged']

    pmap = None
    if participant_mapping:
        try:
            pmap = pd.read_csv(participant_mapping, dtype=str)
        except Exception:
            print('Participant file not found, skipping')

    unique_participants_sessions = df.loc[process_mask, ['participant_to', 'session_to', 'datatype']].drop_duplicates()
    for _, row in unique_participants_sessions.iterrows():
        participant_str = str(row['participant_to'])
        if len(participant_str) >= 4:
            subject_padded = participant_str
        else:
            num = int(participant_str.lstrip('0') or '0')
            if num < 100:
                subject_padded = f"{num:03d}"
            else:
                subject_padded = str(num)
        session_padded = str(row['session_to']).zfill(2)
        bids_path = BIDSPath(
            subject=subject_padded,
            session=session_padded,
            datatype=row['datatype'],
            root=path_BIDS
        ).mkdir()
        try:
            if row['datatype'] == 'meg':
                if not bids_path.meg_calibration_fpath:
                    write_meg_calibration(calibration, bids_path)
                if not bids_path.meg_crosstalk_fpath:
                    write_meg_crosstalk(crosstalk, bids_path)
        except Exception as e:
            print(f"Error writing calibration/crosstalk files: {e}")

    deviants = df[(df['status'] == 'check') & df['_staged']]
    if len(deviants) > 0:
        print("""
              There are staged files marked "check" that require manual review before conversion
              
              Please modify in the editor.
              
              """)
        df.drop(columns=['_staged']).to_csv(conversion_file, sep='\t', index=False)
        summary['total'] = len(df)
        summary['to_process'] = 0
        summary['initial_status_counts'] = df['status'].fillna('error').value_counts().to_dict()
        summary['final_status_counts'] = summary['initial_status_counts']
        summary['message'] = 'Conversion blocked: staged files marked as check require manual review'
        _emit_progress({'stage': 'done', 'message': summary['message'], 'summary': summary})
        return summary

    df['status'] = df['status'].fillna('error')
    status_counts = df['status'].value_counts().to_dict()
    n_files_to_process = int(process_mask.sum())
    summary['total'] = len(df)
    summary['to_process'] = n_files_to_process
    summary['initial_status_counts'] = status_counts
    _emit_progress({
        'stage': 'starting',
        'message': 'Starting BIDS conversion',
        'total': n_files_to_process,
        'processed': 0,
        'errors': 0
    })
    print(
        "Run summary: total={total} to_process={to_process} run={run} check={check} processed={processed} skip={skip} missing={missing} error={error}".format(
            total=len(df),
            to_process=n_files_to_process,
            run=status_counts.get('run', 0),
            check=status_counts.get('check', 0),
            processed=status_counts.get('processed', 0),
            skip=status_counts.get('skip', 0),
            missing=status_counts.get('missing', 0),
            error=status_counts.get('error', 0)
        )
    )
    if n_files_to_process == 0:
        print("No staged files to convert. Exiting bidsify process.")
        summary['final_status_counts'] = status_counts
        summary['message'] = "No staged files to convert"
        _emit_progress({'stage': 'done', 'message': summary['message'], 'summary': summary})
        return summary

    pbar = tqdm(
        total=n_files_to_process,
        desc="Bidsify files",
        unit=" file(s)",
        disable=not sys.stdout.isatty(),
        ncols=80,
        bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]'
    )
    pcount = 0
    processed_now = 0
    errors_now = 0
    recent_errors = []
    max_recent_errors = 25
    for i, d in df[process_mask].iterrows():
        try:
            pcount += 1
            _emit_progress({
                'stage': 'writing',
                'message': f"Writing BIDS file {pcount}/{n_files_to_process}",
                'total': n_files_to_process,
                'processed': processed_now,
                'errors': errors_now,
                'current_file': d.get('raw_name')
            })
            if verbose:
                print(f"Processing file {pcount}/{n_files_to_process} [{d['status']}]: {d['raw_name']}")
                print(f"  Raw file: {d['raw_path']}/{d['raw_name']}")
                print(f"  Participant: {d['participant_from']} -> {d['participant_to']}, Session: {d['session_from']} -> {d['session_to']}")
                print(f"  Task: {d['task']}, Acquisition: {d['acquisition']}, Processing: {d['processing']}, Description: {d['description']}, Run: {d['run']}")

            pbar.update(1)

            bids_path = None

            raw_file = f"{d['raw_path']}/{d['raw_name']}"

            bids_path, raw_info = bids_path_from_rawname(
                raw_file,
                d['session_from'],
                config,
                pmap
            )

            if verbose:
                print(f"  Initial BIDS path directory: {bids_path.directory}")
                print(f"  Initial BIDS path basename: {bids_path.basename}")

            event_id = d['event_id']
            events = None
            run = None
            if pd.notna(d['run']) and d['run'] != '':
                run = str(d['run']).zfill(2)

            if pd.notna(event_id) and event_id:
                with open(f"{path_BIDS}/../{event_id}", 'r') as f:
                    event_id = json.load(f)
                events = mne.find_events(raw)

            participant_str = str(d['participant_to'])
            if len(participant_str) >= 4:
                current_subject_padded = participant_str
            else:
                num = int(participant_str.lstrip('0') or '0')
                if num < 100:
                    current_subject_padded = f"{num:03d}"
                else:
                    current_subject_padded = str(num)

            if verbose:
                acq_val = None if pd.isna(d['acquisition']) or d['acquisition'] == '' else d['acquisition']
                proc_val = None if pd.isna(d['processing']) or d['processing'] == '' else d['processing']
                desc_val = None if pd.isna(d['description']) or d['description'] == '' else d['description']
                print(f"  Updating BIDS path with: subject={current_subject_padded}, task={d['task']}, run={run}, acq={acq_val}, proc={proc_val}, desc={desc_val}")

            bids_path.update(
                subject=current_subject_padded,
                session=str(d['session_to']).zfill(2),
                task=d['task'],
                acquisition=None if pd.isna(d['acquisition']) or d['acquisition'] == '' else d['acquisition'],
                processing=None if pd.isna(d['processing']) or d['processing'] == '' else d['processing'],
                description=None if pd.isna(d['description']) or d['description'] == '' else d['description'],
                run=run
            )

            if verbose:
                print(f"  Final BIDS path: {bids_path.fpath}")

            if bids_path.description and 'trans' in bids_path.description:
                trans = mne.read_trans(raw_file, verbose='error')
                mne.write_trans(bids_path, trans, overwrite=True)

            elif bids_path.suffix and 'headshape' in bids_path.suffix:
                headpos = mne.chpi.read_head_pos(raw_file)
                mne.chpi.write_head_pos(bids_path, headpos)

            elif bids_path.datatype in ['meg', 'eeg']:
                try:
                    raw = mne.io.read_raw_fif(raw_file, allow_maxshield=True, verbose='error')
                    write_raw_bids(
                        raw=raw,
                        bids_path=bids_path,
                        empty_room=None,
                        event_id=event_id,
                        events=events,
                        overwrite=True,
                        verbose='error'
                    )

                    if bids_path.processing:
                        json_path = bids_path.copy().update(extension='.json', split=None)
                        if not exists(json_path.fpath):
                            print(f"Creating missing JSON sidecar: {json_path.basename}")
                            sidecar_data = {
                                'TaskName': bids_path.task,
                                'SamplingFrequency': raw.info['sfreq'],
                                'PowerLineFrequency': raw.info['line_freq'],
                                'Manufacturer': 'Elekta'
                            }
                            with open(json_path.fpath, 'w') as f:
                                json.dump(sidecar_data, f, indent=4)

                except Exception as e:
                    print(f"Error writing BIDS file: {e}")
                    fname = bids_path.copy().update(suffix='meg', extension='.fif').fpath
                    try:
                        raw.save(fname, overwrite=True, verbose='error')
                    except Exception as e:
                        print(f"Error saving raw file: {e}")

                if bids_path.datatype == 'eeg':
                    copy_eeg_to_meg(raw_file, bids_path)

            elif bids_path.acquisition == 'hedscan' and not bids_path.processing:
                opm_tsv = f"{d['raw_path']}/{d['raw_name']}".replace('raw.fif', 'channels.tsv')

                bids_tsv = bids_path.copy().update(suffix='channels', extension='.tsv')
                add_channel_parameters(bids_tsv, opm_tsv)

            try:
                removed_stale = _cleanup_stale_bids_output(
                    old_bids_path=d.get('bids_path'),
                    old_bids_name=d.get('bids_name'),
                    new_fpath=str(bids_path.fpath),
                    bids_root=path_BIDS,
                    verbose=verbose,
                )
                if removed_stale:
                    print(
                        f"Reprocessed file changed output name/path; removed "
                        f"{len(removed_stale)} stale output file(s) from previous conversion: "
                        f"{', '.join(removed_stale)}"
                    )
            except Exception as e:
                # A cleanup failure should never turn an otherwise-successful
                # conversion into an error; just warn and keep going.
                print(f"Warning: failed stale-output cleanup for {d.get('raw_name')}: {e}")

            df.at[i, 'status'] = 'processed'
            df = _set_staged(df, i, False)
            processed_now += 1
            df = _record_processing_success(df, i)
            _emit_progress({
                'stage': 'file-done',
                'message': f"Completed file {pcount}/{n_files_to_process}",
                'total': n_files_to_process,
                'processed': processed_now,
                'errors': errors_now,
                'current_file': d.get('raw_name')
            })

        except Exception as e:
            print(f"Error processing file {d['raw_name']}: {e}")
            error_detail = {
                'raw_name': str(d.get('raw_name', '')),
                'reason': str(e),
                'exception_type': e.__class__.__name__,
                'status': str(d.get('status', '')),
                'task': str(d.get('task', '')),
                'run': str(d.get('run', '')),
                'acquisition': str(d.get('acquisition', '')),
                'processing': str(d.get('processing', '')),
                'description': str(d.get('description', '')),
            }
            if verbose:
                print(f"  Exception details:")
                print(f"    Task: {d['task']}, Run: {d['run']}, Acquisition: {d['acquisition']}")
                print(f"    Processing: {d['processing']}, Description: {d['description']}")
                print(f"  Full traceback:")
                traceback.print_exc()
                error_detail['traceback'] = traceback.format_exc()

            recent_errors.append(error_detail)
            if len(recent_errors) > max_recent_errors:
                recent_errors = recent_errors[-max_recent_errors:]

            df.at[i, 'status'] = 'error'
            df = _set_staged(df, i, False)
            errors_now += 1
            _emit_progress({
                'stage': 'file-error',
                'message': f"Error in file {pcount}/{n_files_to_process}",
                'total': n_files_to_process,
                'processed': processed_now,
                'errors': errors_now,
                'current_file': d.get('raw_name'),
                'last_error': error_detail,
                'recent_errors': recent_errors,
            })

        df.at[i, 'time_stamp'] = ts
        df.at[i, 'bids_path'] = dirname(bids_path)
        df.at[i, 'bids_name'] = basename(bids_path)
        df.drop(columns=['_staged']).to_csv(conversion_file, sep='\t', index=False)

    pbar.close()

    _emit_progress({
        'stage': 'reporting',
        'message': 'Updating BIDS report',
        'total': n_files_to_process,
        'processed': processed_now,
        'errors': errors_now
    })
    report_updates = update_bids_report(df, config)
    final_status_counts = df['status'].fillna('error').value_counts().to_dict()
    summary['processed_now'] = processed_now
    summary['errors_now'] = errors_now
    summary['error_details'] = recent_errors
    summary['final_status_counts'] = final_status_counts
    summary['report_updates'] = int(report_updates or 0)
    summary['message'] = 'BIDS conversion completed'
    _emit_progress({'stage': 'done', 'message': summary['message'], 'summary': summary})
    print(f"All files bidsified according to {conversion_file}")
    return summary


def update_bids_report(conversion_table: pd.DataFrame, config: dict):
    """
    Update the BIDS results report with processed entries in JSON format.
    """
    bids_root = config.get('BIDS', '')
    logPath = setLogPath(config)
    report_file = os.path.join(logPath, 'bids_results.json')

    existing_report = []
    if exists(report_file):
        try:
            with open(report_file, 'r') as f:
                existing_report = json.load(f)
                if isinstance(existing_report, dict) and 'Report Table' in existing_report:
                    existing_report = existing_report.get('Report Table', [])
        except (json.JSONDecodeError, FileNotFoundError):
            existing_report = []

    def check_bids_file_issues(destination_file):
        if not exists(destination_file):
            return "File does not exist"

        issues = []

        if destination_file.endswith('_meg.fif') or destination_file.endswith('_eeg.fif'):
            sidecar_json = destination_file.replace('.fif', '.json')
            if not exists(sidecar_json):
                issues.append("Missing required JSON sidecar")

        if destination_file.endswith('_meg.fif'):
            base = destination_file.rsplit('_meg.fif', 1)[0]
            has_quality_files = any(
                exists(base + suffix)
                for suffix in ['_cal.dat', '_ct.dat', '_crosstalk.txt']
            )
            if not has_quality_files:
                issues.append("Missing MEG calibration/quality files (_cal.dat, _ct.dat)")

        bids_file_rel = os.path.relpath(destination_file, bids_root)
        try:
            validator = BIDSValidator()
            if not validator.is_bids(bids_file_rel):
                if not issues:
                    issues.append("Does not meet BIDS specification requirements")
        except Exception as e:
            if not issues:
                issues.append(f"Validation error: {str(e)[:80]}")

        return "; ".join(issues) if issues else None

    def create_entry(source_file, destination_file, row):
        entry = {}
        bids_file = os.path.join(row['bids_path'].replace(config.get('BIDS', ''), ''), os.path.basename(destination_file))
        entry['Source File'] = source_file

        try:
            entry['Source Size'] = getsize(source_file) if exists(source_file) else 0
        except (OSError, FileNotFoundError):
            entry['Source Size'] = 0

        entry['BIDS File'] = destination_file

        try:
            if exists(destination_file):
                entry['BIDS Size'] = getsize(destination_file)
                entry['BIDS modification Date'] = datetime.fromtimestamp(os.path.getmtime(destination_file)).isoformat()
            else:
                entry['BIDS Size'] = 0
                entry['BIDS modification Date'] = 'Not yet created'
        except (OSError, FileNotFoundError):
            entry['BIDS Size'] = 0
            entry['BIDS modification Date'] = 'Not yet created'

        is_valid = exists(destination_file) and BIDSValidator().is_bids(bids_file)
        entry['Validated'] = 'True BIDS' if is_valid else 'False BIDS'

        if not is_valid:
            entry['Validation Issue'] = check_bids_file_issues(destination_file)

        entry['Participant'] = row['participant_to'] if pd.notna(row['participant_to']) else 'N/A'
        entry['Session'] = row['session_to'] if pd.notna(row['session_to']) else 'N/A'
        entry['Task'] = row['task'] if pd.notna(row['task']) else 'N/A'
        entry['Acquisition'] = row['acquisition'] if pd.notna(row['acquisition']) and row['acquisition'] else 'N/A'
        entry['Datatype'] = row['datatype'] if pd.notna(row['datatype']) else 'N/A'
        entry['Processing'] = row['processing'] if pd.notna(row['processing']) and row['processing'] else 'N/A'
        entry['Splits'] = row['split'] if pd.notna(row['split']) and row['split'] else 'N/A'
        entry['Conversion Status'] = row['status'] if pd.notna(row['status']) else 'error'
        entry['timestamp'] = datetime.now().isoformat()
        entry['BIDS action'] = ''
        entry['action_timestamp'] = None
        entry['action_user_note'] = ''

        return entry

    df = conversion_table.drop_duplicates(subset=['raw_path', 'raw_name', 'bids_path', 'bids_name'])
    df = df.where(pd.notnull(df) & (df != ''), None)
    for col in df.columns:
        if df[col].dtype != object:
            df[col] = df[col].astype(object)
    grouped_entries = []
    for i, row in df.iterrows():
        source_file = f"{row['raw_path']}/{row['raw_name']}"
        source_base = re.sub(r'_raw-\d+\.fif$', '.fif', source_file)

        bids_file_path = join(row['bids_path'], row['bids_name'])

        source_files = get_split_file_parts(source_base)
        destination_files = get_split_file_parts(bids_file_path)

        if not isinstance(source_files, list):
            source_files = [source_files]
        if not isinstance(destination_files, list):
            destination_files = [destination_files]

        for source_file, destination_file in zip(source_files, destination_files):
            grouped_entries.append(create_entry(source_file, destination_file, row))

    def _entry_key(entry):
        return (entry.get('Source File', ''), entry.get('BIDS File', ''))

    existing_keys = {_entry_key(e) for e in existing_report}
    new_entries = []
    updated_entries = []

    for entry in grouped_entries:
        key = _entry_key(entry)
        if key not in existing_keys:
            new_entries.append(entry)
        else:
            updated_entries.append(entry)

    for updated in updated_entries:
        key = _entry_key(updated)
        existing_report = [updated if _entry_key(e) == key else e for e in existing_report]

    if not new_entries and not updated_entries:
        print("[INFO] No new or updated entries to add to BIDS results report.")
        return 0

    final_report = {}
    participants_file = glob('participants.tsv', root_dir=bids_root)
    data_description_file = glob('dataset_description.json', root_dir=bids_root)
    try:
        conversion_status = 'Complete' if all(
            str(entry.get('Conversion Status', '')).lower() == 'processed' for entry in existing_report + new_entries
        ) else 'Incomplete'
    except Exception:
        conversion_status = 'Incomplete'

    all_entries = existing_report + new_entries
    total_files = len(all_entries)
    valid_bids = sum(1 for e in all_entries if e.get('Validated') == 'True BIDS')
    invalid_bids = sum(1 for e in all_entries if e.get('Validated') == 'False BIDS')

    validation_issues = {}
    for entry in all_entries:
        if entry.get('Validated') == 'False BIDS' and entry.get('Validation Issue'):
            issue = entry.get('Validation Issue')
            issue_type = issue.split(';')[0].strip()
            validation_issues[issue_type] = validation_issues.get(issue_type, 0) + 1

    compliance_summary = {
        'Conversion Status': conversion_status,
        'Total Files': total_files,
        'Valid BIDS Files': valid_bids,
        'Invalid BIDS Files': invalid_bids,
        'Compliance Rate (%)': round((valid_bids / total_files * 100) if total_files > 0 else 0, 1),
        'Participants file': participants_file[0] if participants_file else 'Not found',
        'Data description': data_description_file[0] if data_description_file else 'Not found'
    }

    if validation_issues:
        compliance_summary['Validation Issues'] = {
            'description': 'Common reasons for BIDS non-compliance. Reference: https://bids-standard.github.io/',
            'issues': validation_issues,
            'remediation': {
                'Missing required JSON sidecar': 'Ensure all MEG/EEG data files have corresponding .json metadata sidecars',
                'Missing MEG calibration/quality files': 'Check that calibration (_cal.dat), crosstalk (_ct.dat) files are present for Neuromag systems',
                'Does not meet BIDS specification': 'Check file naming conventions, required metadata fields, and dataset structure'
            }
        }

    final_report['BIDS Summary'] = compliance_summary
    final_report['Report Table'] = existing_report + new_entries

    try:
        with open(report_file, 'w') as f:
            json.dump(final_report, f, indent=4, allow_nan=False)
        print(f"[INFO] BIDS results report written to: {report_file}")
    except Exception as e:
        print(f"[ERROR] Failed to write BIDS results report to {report_file}: {e}")

    print(f"BIDS report updated: {len(new_entries)} new entries, {len(updated_entries)} updated entries (total: {len(existing_report + new_entries)})")

    return len(new_entries) + len(updated_entries)


def run_qa_analysis(config: dict, conversion_table: pd.DataFrame = None) -> Dict[str, Any]:
    """
    Run QA analysis on completed BIDS dataset.
    """
    if not QA_AVAILABLE:
        return {'error': 'QA modules not available'}

    path_BIDS = config.get('BIDS', '')
    if not exists(path_BIDS):
        return {'error': f'BIDS path not found: {path_BIDS}'}

    print("[INFO] Running post-conversion QA analysis...")

    try:
        agent = BIDSQAAgent(path_BIDS, config, enable_llm=False)
        results = agent.analyze_dataset(conversion_table)

        print(f"[INFO] QA analysis complete: {results['summary']['total_issues']} issues found")

        if results['summary']['total_issues'] > 0:
            print(f"  - Errors: {results['summary']['severity_counts'].get('error', 0)}")
            print(f"  - Warnings: {results['summary']['severity_counts'].get('warning', 0)}")
            print(f"  - Info: {results['summary']['severity_counts'].get('info', 0)}")
        else:
            print("  ✓ No issues detected")

        return results
    except Exception as e:
        print(f"[ERROR] QA analysis failed: {e}")
        return {'error': str(e)}


def args_parser():
    """
    Parse command-line arguments for bidsify script.
    """
    parser = argparse.ArgumentParser(
        description=(
            "\n"
            "BIDS Conversion Pipeline\n\n"
            "Main Operations:\n"
            "    --analyse  Only make or update the conversion table (no conversion)\n"
            "    --run      Execute the BIDS conversion pipeline (convert files)\n"
            "    --report   Generate BIDS report with QA analysis (JSON summary)\n"
            "    --qa       Run post-conversion QA analysis on completed dataset\n\n"
            "Arguments:\n"
            "    --config   Path to config file (YAML or JSON)\n"
            "    --reindex  Force full rescan of raw files (ignore cache)\n"
        ),
        add_help=True
    )
    parser.add_argument('--config', type=str, required=True, help='Path to config file (YAML or JSON)')
    parser.add_argument('--analyse', action='store_true', help='Make or update conversion table only')
    parser.add_argument('--run', action='store_true', help='Execute BIDS conversion pipeline')
    parser.add_argument('--report', action='store_true', help='Generate BIDS report with QA analysis (JSON summary)')
    parser.add_argument('--qa', action='store_true', help='Run post-conversion QA analysis on completed BIDS dataset')
    parser.add_argument('--reindex', action='store_true', help='Force full rescan of raw files (ignore cache)')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose output for debugging')
    args = parser.parse_args()
    return args


def main(config: str = None):
    """
    Main entry point for BIDS conversion pipeline.
    """
    from .utils import get_parameters

    if config is None:
        args = args_parser()

        if args.config:
            config_file = args.config
        else:
            print('Use --config to specify a configuration file')
            sys.exit(1)

        if config_file:
            config = get_parameters(config_file)
        else:
            print('No configuration file selected')
            sys.exit(1)

    if isinstance(config, str):
        config = get_parameters(config)

    logPath = setLogPath(config)

    if args.analyse:
        print("Generating conversion table only")
        conversion_table, conversion_file, run_conversion = update_conversion_table(config, force_scan=args.reindex)
        conversion_table['status'] = conversion_table['status'].fillna('error')
        conversion_table.to_csv(conversion_file, sep='\t', index=False)
        print(f"Conversion table saved to: {conversion_file}")
        status_counts = conversion_table['status'].value_counts().to_dict()
        total_rows = len(conversion_table)
        print(
            "Summary: total={total} run={run} check={check} processed={processed} skip={skip} missing={missing} error={error}".format(
                total=total_rows,
                run=status_counts.get('run', 0),
                check=status_counts.get('check', 0),
                processed=status_counts.get('processed', 0),
                skip=status_counts.get('skip', 0),
                missing=status_counts.get('missing', 0),
                error=status_counts.get('error', 0)
            )
        )

    if args.run:
        print("Running full BIDS conversion")
        create_dataset_description(config)
        create_proc_description(config)
        conversion_table, conversion_file = load_conversion_table(config, refresh_status=False)
        bidsify(config, force_scan=False, conversion_table=conversion_table, conversion_file=conversion_file, verbose=getattr(args, 'verbose', False))
        update_sidecars(config)

    if args.report:
        print("Generating BIDS conversion report")
        conversion_table, _ = load_conversion_table(config, refresh_status=False)
        update_bids_report(conversion_table, config)

        qa_results = run_qa_analysis(config, conversion_table)
        if QA_AVAILABLE and 'error' not in qa_results:
            results_file = os.path.join(logPath, 'bids_results.json')
            with open(results_file, 'r') as f:
                report = json.load(f)
            report['QA Analysis'] = {
                'timestamp': qa_results.get('timestamp', datetime.now().isoformat()),
                'summary': qa_results.get('summary', {}),
                'findings': qa_results.get('findings', []),
            }
            with open(results_file, 'w') as f:
                json.dump(report, f, indent=4, allow_nan=False)
            print(f"[INFO] QA results added to report: {results_file}")

    return True

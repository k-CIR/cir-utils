import os
import time
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from glob import glob
from os.path import dirname, join, getsize, getmtime
from typing import Optional

import pandas as pd

from .constants import OPM_EXEPCIONS_PATTERNS
from .parsing import bids_path_from_rawname, get_split_file_parts
from .utils import setLogPath

CONVERSION_COLUMNS = [
    'time_stamp',
    'status',
    'participant_from',
    'participant_to',
    'session_from',
    'session_to',
    'task',
    'split',
    'run',
    'datatype',
    'acquisition',
    'processing',
    'description',
    'suffix',
    'extension',
    'recording',
    'space',
    'tracking_system',
    'mtime',
    'size',
    'raw_path',
    'raw_name',
    'bids_path',
    'bids_name',
    'event_id',
    'metadata'
]


def _parse_metadata_object(value):
    if _is_missing_scalar(value):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_table(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=CONVERSION_COLUMNS)

    # One-time migration of legacy tracking columns into metadata.
    if 'metadata' not in df.columns:
        df['metadata'] = None
    has_legacy_tracking = any(col in df.columns for col in ['attempt_count', 'status_history', 'notes', 'last_processed'])
    if has_legacy_tracking:
        for idx, row in df.iterrows():
            metadata = _parse_metadata_object(row.get('metadata'))
            tracking = metadata.get('tracking', {}) if isinstance(metadata.get('tracking'), dict) else {}

            if _is_missing_scalar(tracking.get('attempt_count')) and 'attempt_count' in df.columns:
                tracking['attempt_count'] = _parse_int(row.get('attempt_count'), default=0)

            history = tracking.get('status_history')
            if not isinstance(history, list) or not history:
                if 'status_history' in df.columns:
                    tracking['status_history'] = _parse_status_history(row.get('status_history'))
                else:
                    tracking['status_history'] = _parse_status_history(history)

            if _is_missing_scalar(tracking.get('notes')) and 'notes' in df.columns and not _is_missing_scalar(row.get('notes')):
                tracking['notes'] = str(row.get('notes'))

            if _is_missing_scalar(tracking.get('last_processed')) and 'last_processed' in df.columns and not _is_missing_scalar(row.get('last_processed')):
                tracking['last_processed'] = str(row.get('last_processed'))

            metadata['tracking'] = tracking
            df.at[idx, 'metadata'] = json.dumps(metadata, default=str)

    for col in CONVERSION_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df = df[CONVERSION_COLUMNS].where(pd.notna(df[CONVERSION_COLUMNS]), None)

    # Fill any empty/NaN status values with 'error'
    if 'status' in df.columns:
        df['status'] = df['status'].fillna('error')

    return df[CONVERSION_COLUMNS]


def _build_event_index(path_bids: str) -> dict:
    event_index = {}
    for event_file in glob('*_event_id.json', root_dir=f'{path_bids}/..'):
        task_name = event_file.replace('_event_id.json', '')
        event_index[task_name] = event_file
    return event_index


def _load_index(index_file: str) -> dict:
    if not os.path.exists(index_file):
        return {}
    try:
        df = pd.read_csv(index_file, sep='\t', dtype=str)
    except Exception:
        return {}

    index = {}
    for _, row in df.iterrows():
        key = f"{row['raw_path']}/{row['raw_name']}"
        index[key] = (row.get('mtime', ''), row.get('size', ''))
    return index


def _write_index(index_file: str, entries: list):
    df = pd.DataFrame(entries)
    df.to_csv(index_file, sep='\t', index=False)


def _file_signature(full_path: str) -> tuple:
    try:
        return str(getmtime(full_path)), str(getsize(full_path))
    except Exception:
        return '', ''


def _is_missing_scalar(value) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return False
    try:
        if bool(pd.isna(value)):
            return True
    except Exception:
        return False
    return str(value).strip() == ''


def _backfill_signature_columns(table: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Fill missing mtime/size columns from source files for backward compatibility."""
    if table is None or table.empty:
        return table, 0

    updated_rows = 0
    for idx, row in table.iterrows():
        has_missing_mtime = _is_missing_scalar(row.get('mtime'))
        has_missing_size = _is_missing_scalar(row.get('size'))
        if not (has_missing_mtime or has_missing_size):
            continue

        raw_path = row.get('raw_path')
        raw_name = row.get('raw_name')
        if _is_missing_scalar(raw_path) or _is_missing_scalar(raw_name):
            continue

        full_path = join(str(raw_path), str(raw_name))
        mtime, size = _file_signature(full_path)
        row_updated = False

        if has_missing_mtime and mtime:
            table.at[idx, 'mtime'] = mtime
            row_updated = True
        if has_missing_size and size:
            table.at[idx, 'size'] = size
            row_updated = True

        if row_updated:
            updated_rows += 1

    return table, updated_rows


def _parse_float(value):
    if _is_missing_scalar(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value, default=0):
    if _is_missing_scalar(value):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_iso_utc(epoch_value):
    if epoch_value is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_value, timezone.utc).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def _parse_status_history(history_value):
    if _is_missing_scalar(history_value):
        return []
    if isinstance(history_value, list):
        return history_value
    if isinstance(history_value, str):
        try:
            parsed = json.loads(history_value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _build_file_metadata(full_path, fallback_mtime=None, fallback_size=None):
    metadata = {
        'path': full_path,
        'exists': False,
        'size_bytes': None,
        'mtime_epoch': None,
        'mtime_iso': None,
    }

    if full_path and os.path.exists(full_path):
        try:
            mtime = getmtime(full_path)
            size = getsize(full_path)
            metadata.update({
                'exists': True,
                'size_bytes': int(size),
                'mtime_epoch': float(mtime),
                'mtime_iso': _to_iso_utc(float(mtime)),
            })
            return metadata
        except Exception:
            pass

    fallback_mtime_num = _parse_float(fallback_mtime)
    fallback_size_num = None
    if not _is_missing_scalar(fallback_size):
        try:
            fallback_size_num = int(str(fallback_size))
        except (TypeError, ValueError):
            fallback_size_num = None
    metadata['size_bytes'] = fallback_size_num
    metadata['mtime_epoch'] = fallback_mtime_num
    metadata['mtime_iso'] = _to_iso_utc(fallback_mtime_num)
    return metadata


def _build_row_metadata(row, refreshed_at):
    raw_path = row.get('raw_path')
    raw_name = row.get('raw_name')
    source_path = None
    if not _is_missing_scalar(raw_path) and not _is_missing_scalar(raw_name):
        source_path = join(str(raw_path), str(raw_name))

    bids_path = row.get('bids_path')
    bids_name = row.get('bids_name')
    converted_path = None
    if not _is_missing_scalar(bids_path) and not _is_missing_scalar(bids_name):
        converted_path = join(str(bids_path), str(bids_name))

    metadata = _parse_metadata_object(row.get('metadata'))
    tracking_existing = metadata.get('tracking', {}) if isinstance(metadata.get('tracking'), dict) else {}
    attempt_count_source = tracking_existing.get('attempt_count')
    if _is_missing_scalar(attempt_count_source):
        attempt_count_source = row.get('attempt_count')

    history_source = tracking_existing.get('status_history')
    if not isinstance(history_source, list) or not history_source:
        history_source = row.get('status_history')

    notes_value = tracking_existing.get('notes')
    if _is_missing_scalar(notes_value) and not _is_missing_scalar(row.get('notes')):
        notes_value = str(row.get('notes'))

    last_processed_source = tracking_existing.get('last_processed')
    if _is_missing_scalar(last_processed_source):
        last_processed_source = row.get('last_processed')

    metadata.update({
        'schema_version': 1,
        'refreshed_at': refreshed_at,
        'source': _build_file_metadata(source_path, row.get('mtime'), row.get('size')),
        'converted': _build_file_metadata(converted_path),
        'tracking': {
            'status': None if _is_missing_scalar(row.get('status')) else str(row.get('status')),
            'last_processed': None if _is_missing_scalar(last_processed_source) else str(last_processed_source),
            'attempt_count': _parse_int(attempt_count_source, default=0),
            'status_history': _parse_status_history(history_source),
            'notes': None if _is_missing_scalar(notes_value) else str(notes_value),
        },
    })
    return metadata


def _refresh_metadata_column(table: pd.DataFrame) -> pd.DataFrame:
    if table is None or table.empty:
        return table

    refreshed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    for idx, row in table.iterrows():
        metadata = _build_row_metadata(row, refreshed_at)
        table.at[idx, 'metadata'] = json.dumps(metadata, default=str)
    return table


def _bids_output_exists(bids_path: Optional[str], bids_name: Optional[str]) -> bool:
    if pd.isna(bids_path) or pd.isna(bids_name):
        return False
    if not bids_path or not bids_name:
        return False
    bids_path = str(bids_path)
    bids_name = str(bids_name)
    exact = join(bids_path, bids_name)
    if os.path.exists(exact):
        return True
    base, ext = os.path.splitext(bids_name)
    if base:
        # Fallback: allow any extension or sidecar for the same base name.
        pattern = join(bids_path, f"{base}.*")
        if glob(pattern):
            return True
    return False


def _update_status_with_history(table: pd.DataFrame, row_idx, new_status: str) -> pd.DataFrame:
    """Update status and record transition in metadata.tracking.status_history."""
    old_status = table.at[row_idx, 'status']
    table.at[row_idx, 'status'] = new_status

    metadata = _parse_metadata_object(table.at[row_idx, 'metadata'])
    tracking = metadata.get('tracking', {}) if isinstance(metadata.get('tracking'), dict) else {}
    history = _parse_status_history(tracking.get('status_history'))

    if old_status != new_status:
        history.append({
            'from': str(old_status) if pd.notna(old_status) else None,
            'to': new_status,
            'timestamp': datetime.now().isoformat()
        })

    tracking['status'] = new_status
    tracking['status_history'] = history
    tracking['attempt_count'] = _parse_int(tracking.get('attempt_count'), default=0)
    if _is_missing_scalar(tracking.get('notes')):
        tracking['notes'] = None
    metadata['tracking'] = tracking
    table.at[row_idx, 'metadata'] = json.dumps(metadata, default=str)

    return table


def _record_processing_success(table: pd.DataFrame, row_idx) -> pd.DataFrame:
    """Update metadata.tracking.last_processed and increment attempt_count after success."""
    last_processed = datetime.now().isoformat()

    metadata = _parse_metadata_object(table.at[row_idx, 'metadata'])
    tracking = metadata.get('tracking', {}) if isinstance(metadata.get('tracking'), dict) else {}
    current_count = _parse_int(tracking.get('attempt_count'), default=0)
    tracking['attempt_count'] = current_count + 1
    tracking['last_processed'] = last_processed
    tracking['status'] = table.at[row_idx, 'status']
    tracking['status_history'] = _parse_status_history(tracking.get('status_history'))
    if _is_missing_scalar(tracking.get('notes')):
        tracking['notes'] = None
    metadata['tracking'] = tracking
    table.at[row_idx, 'metadata'] = json.dumps(metadata, default=str)
    return table


def _refresh_processed_status(table: pd.DataFrame) -> pd.DataFrame:
    if table.empty:
        return table

    for i, row in table.iterrows():
        raw_path = row.get('raw_path')
        raw_name = row.get('raw_name')
        raw_path = None if pd.isna(raw_path) else str(raw_path) if raw_path is not None else None
        raw_name = None if pd.isna(raw_name) else str(raw_name) if raw_name is not None else None
        if raw_path and raw_name and not os.path.exists(join(raw_path, raw_name)):
            table = _update_status_with_history(table, i, 'missing')
            continue
        if row.get('status') in ['skip', 'check']:
            continue
        bids_path = row.get('bids_path')
        bids_name = row.get('bids_name')
        bids_path = None if pd.isna(bids_path) else str(bids_path) if bids_path is not None else None
        bids_name = None if pd.isna(bids_name) else str(bids_name) if bids_name is not None else None
        if _bids_output_exists(bids_path, bids_name):
            table = _update_status_with_history(table, i, 'processed')
        elif row.get('status') == 'processed':
            table = _update_status_with_history(table, i, 'run')
    return table


def generate_new_conversion_table(config: dict, existing_table: Optional[pd.DataFrame] = None, force_scan: bool = False):
    """
    For each participant and session within MEG folder, generate conversion table entries.
    Uses parallel processing for efficiency and lightweight scans.
    """
    ts = datetime.now().strftime('%Y%m%d')
    path_project = join(config.get('Root', ''), config.get('Name', ''))
    path_raw = config.get('Raw', '')
    path_BIDS = config.get('BIDS', '')
    participant_mapping = join(path_project, config.get('Participants_mapping_file', ''))
    tasks = config.get('Tasks', []) + OPM_EXEPCIONS_PATTERNS

    processing_modalities = ['triux', 'hedscan']

    existing_table = _normalize_table(existing_table)
    processed_files = set()
    if not existing_table.empty:
        processed_files = set(
            existing_table.loc[(existing_table['status'] == 'processed') |
                                (existing_table['status'] == 'skip')]
            .apply(lambda row: f"{row['raw_path']}/{row['raw_name']}", axis=1)
        )

    pmap = None
    if participant_mapping:
        try:
            pmap = pd.read_csv(participant_mapping, dtype=str)
        except Exception:
            print('Participant mapping file not found, skipping')

    event_index = _build_event_index(path_BIDS)

    logPath = setLogPath(config)
    index_file = os.path.join(logPath, 'bids_conversion_index.tsv')
    previous_index = {} if force_scan else _load_index(index_file)
    new_index_entries = []

    def process_file_entry(job):
        participant, date_session, acquisition, file, sig, changed = job
        full_file_name = os.path.join(path_raw, participant, date_session, acquisition, file)
        if full_file_name in processed_files and not changed:
            if participant in glob('sub-*', root_dir=path_BIDS):
                return None

        bids_path, info_dict = bids_path_from_rawname(
            full_file_name,
            date_session,
            config,
            pmap,
            read_info=False
        )

        if info_dict['split']:
            return None
        split = None
        splits = get_split_file_parts(full_file_name)
        if isinstance(splits, list):
            split = str(len(splits) - 1)

        if not bids_path:
            return None

        task = bids_path.task
        run = bids_path.run
        datatype = bids_path.datatype
        proc = bids_path.processing
        desc = bids_path.description
        subj_out = bids_path.subject
        session_out = bids_path.session
        acquisition = bids_path.acquisition

        event_file = event_index.get(task) if task else None

        status = 'run'
        if task not in tasks + ['Noise']:
            status = 'check'

        if changed and status == 'processed':
            status = 'run'

        return {
            'time_stamp': ts,
            'status': status,
            'participant_from': participant,
            'participant_to': subj_out,
            'session_from': date_session,
            'session_to': session_out,
            'task': task,
            'split': split,
            'run': run,
            'datatype': datatype,
            'acquisition': acquisition,
            'processing': proc,
            'description': desc,
            'mtime': sig[0],
            'size': sig[1],
            'raw_path': dirname(full_file_name),
            'raw_name': file,
            'bids_path': bids_path.directory,
            'bids_name': bids_path.basename,
            'event_id': event_file,
            'metadata': None
        }

    jobs = []
    participants = glob('sub-*', root_dir=path_raw)
    for participant in participants:
        sessions = sorted([session for session in glob('*', root_dir=os.path.join(path_raw, participant))
                          if os.path.isdir(os.path.join(path_raw, participant, session))])
        for date_session in sessions:
            for acquisition in processing_modalities:
                all_files = sorted(
                    glob('*.fif', root_dir=os.path.join(path_raw, participant, date_session, acquisition)) +
                    glob('*.pos', root_dir=os.path.join(path_raw, participant, date_session, acquisition))
                )
                for file in all_files:
                    full_file_name = os.path.join(path_raw, participant, date_session, acquisition, file)
                    sig = _file_signature(full_file_name)
                    prev = previous_index.get(full_file_name)
                    changed = True if force_scan else prev != sig
                    new_index_entries.append({
                        'raw_path': dirname(full_file_name),
                        'raw_name': file,
                        'mtime': sig[0],
                        'size': sig[1]
                    })
                    jobs.append((participant, date_session, acquisition, file, sig, changed))

    max_workers = min(4, os.cpu_count() or 1)
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_file_entry, job): job for job in jobs}

        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
            except Exception as e:
                job = futures[future]
                print(f"Error processing {job}: {e}")
                continue

    results.sort(key=lambda x: (x['participant_from'], x['session_from'], x['acquisition'], x['task'] or '', x['raw_name']))

    if new_index_entries:
        _write_index(index_file, new_index_entries)

    for result in results:
        yield result


def load_conversion_table(config: dict, refresh_status: bool = False):
    """
    Load or generate conversion table for BIDS conversion process.
    """
    overwrite = config.get('Overwrite_conversion', False)
    logPath = setLogPath(config)
    conversion_file = config.get('Conversion_file', 'utils/meg_bids_conversion.tsv')
    if conversion_file == '':
        conversion_file = 'utils/meg_bids_conversion.tsv'

    if not os.path.exists(logPath):
        os.makedirs(logPath, exist_ok=True)
        print(f"Created new log path: {logPath}")

    if not os.path.isabs(conversion_file):
        root_path = str(config.get('Root', '') or '').strip()
        if root_path:
            conversion_file = os.path.join(root_path, conversion_file.lstrip('/'))
        else:
            conversion_file = os.path.join(logPath, conversion_file)

    if not os.path.exists(dirname(conversion_file)):
        os.makedirs(dirname(conversion_file), exist_ok=True)
        print("No conversion logs directory found. Created new")

    if conversion_file and os.path.exists(conversion_file) and os.path.isfile(conversion_file) and not overwrite:
        try:
            if os.path.getsize(conversion_file) > 0:
                print(f"Loading conversion table from {conversion_file}")
                conversion_table = pd.read_csv(conversion_file, sep='\t', dtype=str)
                conversion_table = _normalize_table(conversion_table)
                if refresh_status:
                    conversion_table = _refresh_processed_status(conversion_table)
                conversion_table, _ = _backfill_signature_columns(conversion_table)
                conversion_table = _refresh_metadata_column(conversion_table)
                return conversion_table, conversion_file
            else:
                print(f"Conversion file {conversion_file} is empty, generating new")
        except (pd.errors.EmptyDataError, ValueError):
            print(f"Conversion file {conversion_file} is corrupted or empty, generating new")
    else:
        if overwrite:
            print('Overwrite requested, generating new conversion table')
        elif not conversion_file:
            print('No conversion file specified, generating new')
        else:
            print(f'Conversion file {conversion_file} not found, generating new')

        results = list(generate_new_conversion_table(config))
        conversion_table = pd.DataFrame(results)
        conversion_table = _normalize_table(conversion_table)

        conversion_table.to_csv(conversion_file, sep='\t', index=False)
        print(f"New conversion table generated and saved to {os.path.basename(conversion_file)}")
        while not os.path.exists(conversion_file):
            time.sleep(0.5)
        try:
            if os.path.getsize(conversion_file) > 0:
                conversion_table = pd.read_csv(conversion_file, sep='\t', dtype=str)
                conversion_table = _normalize_table(conversion_table)
                if refresh_status:
                    conversion_table = _refresh_processed_status(conversion_table)
                conversion_table, _ = _backfill_signature_columns(conversion_table)
                conversion_table = _refresh_metadata_column(conversion_table)
                return conversion_table, conversion_file
            print("Warning: Generated conversion table is empty. No files found to convert.")
            return pd.DataFrame(columns=CONVERSION_COLUMNS), conversion_file
        except (pd.errors.EmptyDataError, ValueError) as e:
            print(f"Warning: Generated conversion table is corrupted or empty: {e}")
            return pd.DataFrame(columns=CONVERSION_COLUMNS), conversion_file

    return pd.DataFrame(columns=CONVERSION_COLUMNS), conversion_file


def update_conversion_table(config, conversion_file=None, force_scan: bool = False):
    """
    Update conversion table to add new files not currently tracked.
    """
    existing_conversion_table, existing_conversion_file = load_conversion_table(config, refresh_status=True)
    existing_conversion_table, _ = _backfill_signature_columns(existing_conversion_table)
    existing_conversion_table = _refresh_metadata_column(existing_conversion_table)
    if not conversion_file:
        conversion_file = existing_conversion_file

    results = list(generate_new_conversion_table(config, existing_conversion_table, force_scan=force_scan))
    new_conversion_table = pd.DataFrame(results)
    new_conversion_table = _normalize_table(new_conversion_table)

    run_conversion = True
    if new_conversion_table.empty:
        run_conversion = False
        print("No files found to add to conversion table.")
        return existing_conversion_table, conversion_file, run_conversion

    existing_conversion_table = _normalize_table(existing_conversion_table)
    existing_keys = set(
        existing_conversion_table.apply(lambda row: f"{row['raw_path']}/{row['raw_name']}", axis=1)
    )

    diff_rows = []
    for _, row in new_conversion_table.iterrows():
        key = f"{row['raw_path']}/{row['raw_name']}"
        if key not in existing_keys:
            diff_rows.append(row)

    if not diff_rows:
        run_conversion = False
        print("No new files to add to conversion table.")
        existing_conversion_table = _refresh_metadata_column(existing_conversion_table)
        return existing_conversion_table, conversion_file, run_conversion

    diff = pd.DataFrame(diff_rows)
    if 'status' in diff.columns:
        diff.loc[diff['status'].isin(['processed', 'skip']), 'status'] = 'run'

    updated_table = pd.concat([existing_conversion_table, diff], ignore_index=True)
    updated_table, _ = _backfill_signature_columns(updated_table)
    updated_table = _refresh_metadata_column(updated_table)

    print(f"Adding {len(diff)} new files to conversion table.")

    return updated_table, conversion_file, run_conversion

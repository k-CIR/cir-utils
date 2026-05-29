EXCLUDE_PATTERNS = [r'-\d+\.fif', '_trans', 'avg.fif']
NOISE_PATTERNS = ['empty', 'noise', 'Empty']
HEADPOS_PATTERNS = ['headpos', 'headshape']
OPM_EXEPCIONS_PATTERNS = ['HPIbefore', 'HPIafter', 'HPImiddle', 'HPIpre', 'HPIpost']
PROC_PATTERNS = ['tsss', 'sss', r'corr\d+', r'ds\d+', 'mc', 'avgHead']

# Conversion table field descriptions for user guidance
CONVERSION_TABLE_FIELDS = {
    'time_stamp': 'Date when entry was created (YYYYMMDD)',
    'status': 'Processing status: check=needs review, run=ready to convert, processed=converted, skip=ignore, missing=raw file missing on disk',
    'participant_from': 'Original participant identifier from filename',
    'participant_to': 'Target BIDS participant ID (zero-padded)',
    'session_from': 'Original session identifier from filename',
    'session_to': 'Target BIDS session ID (zero-padded)',
    'task': 'BIDS task name (EDITABLE - main field for manual changes)',
    'acquisition': 'MEG acquisition type (triux/hedscan)',
    'processing': 'Processing pipeline applied (hpi, sss, etc.)',
    'description': 'Additional BIDS description field',
    'datatype': 'BIDS datatype (meg/eeg)',
    'split': 'Split file indicator for large files (auto-managed)',
    'run': 'BIDS run number for repeated acquisitions',
    'raw_path': 'Full path to source raw file directory',
    'raw_name': 'Source raw filename',
    'bids_path': 'Target BIDS directory path',
    'bids_name': 'Target BIDS filename',
    'event_id': 'Associated event file for task',
    'metadata': 'JSON metadata with source/converted file stats and tracking fields (last_processed, attempt_count, status_history, notes)'
}

DERIVATIVES_SUBFOLDER = 'derivatives/preprocessed-meg'

# Fixed calibration/crosstalk paths for TRIUX systems at CIR/NatMEG
CALIBRATION_PATH = '/neuro/local/mne/share/mne-python/mne/data/SSS/sss_cal_3080.dat'
CROSSTALK_PATH = '/neuro/local/mne/share/mne-python/mne/data/SSS/ct_sparse_TRIUX.fif'

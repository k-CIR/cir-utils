
"""Centralized constants for CIR utils.

This module is intentionally declarative. Constants here are not yet wired into
all implementations, but are defined to keep naming and defaults consistent.
"""

# ----------------------------------------------------------------------------
# Project / filesystem roots
# ----------------------------------------------------------------------------
PROJECTS_ROOT = '/data/projects'

# Common relative directories under a project
DEFAULT_RAW_DIR = 'raw'
DEFAULT_BIDS_DIR = 'BIDS'
DEFAULT_UTILS_DIR = 'utils'
DEFAULT_LOG_DIR = 'logs'
DEFAULT_DERIVATIVES_DIR = 'derivatives'

# Common sub-paths used by tabs
RAW_MRI_SUBDIR = 'raw/mri'
RAW_PET_SUBDIR = 'raw/pet'
RAW_MEG_SUBDIR = 'raw/natmeg'
RAW_MEG_LEGACY_SUBDIR = 'raw/meg'

# ----------------------------------------------------------------------------
# Server defaults
# ----------------------------------------------------------------------------
DEFAULT_SERVER_HOST = 'localhost'
DEFAULT_SERVER_PORT = 8080
DEFAULT_PORT_SCAN_ATTEMPTS = 10
DEFAULT_RATE_LIMIT_PER_MINUTE = 75
AUTH_TOKEN_BYTES_URLSAFE = 16

# Public paths that do not require token auth
AUTH_EXEMPT_PATHS = ('/', '/index.html')

# ----------------------------------------------------------------------------
# Tab metadata defaults
# ----------------------------------------------------------------------------
TAB_ID_MR = 'mr-bids'
TAB_ID_PET = 'pet-bids'
TAB_ID_MEG = 'meg-bids'

TAB_LABEL_MR = 'MR BIDS'
TAB_LABEL_PET = 'PET BIDS'
TAB_LABEL_MEG = 'MEG BIDS'

TAB_ORDER_MR = 0
TAB_ORDER_PET = 1
TAB_ORDER_MEG = 2

TAB_REQUIRES_PATH_MR = RAW_MRI_SUBDIR
TAB_REQUIRES_PATH_PET = RAW_PET_SUBDIR
TAB_REQUIRES_PATH_MEG = RAW_MEG_SUBDIR

# ----------------------------------------------------------------------------
# Configuration and conversion file defaults
# ----------------------------------------------------------------------------
# MR
MR_DEFAULT_CONFIG_FILE = 'dcm2bids_config_mr.json'

# PET
PET_DEFAULT_CONFIG_FILE = 'dcm2bids_config_pet.json'

# MEG
MEG_DEFAULT_CONFIG_FILE = 'meg_bids_config.json'
MEG_DEFAULT_CONVERSION_FILE = 'utils/meg_bids_conversion.tsv'
MEG_DEFAULT_REPORT_FILE = 'logs/meg_bids_report.json'

# ----------------------------------------------------------------------------
# API endpoints (as string constants for consistency)
# ----------------------------------------------------------------------------
API_TABS = '/api/tabs'
API_TAB_CONTENT = '/tab-content'

# MEG endpoints
MEG_API_GET_CONFIG = '/meg-get-config'
MEG_API_GET_PROJECT_ROOT = '/meg-get-project-root'
MEG_API_VALIDATE_PATHS = '/meg-validate-paths'
MEG_API_LOAD_CONFIG = '/meg-load-config'
MEG_API_SAVE_CONFIG = '/meg-save-config'
MEG_API_RUN_ANALYSIS = '/meg-run-analysis'
MEG_API_LOAD_CONVERSION_TABLE = '/meg-load-conversion-table'
MEG_API_SAVE_CONVERSION_TABLE = '/meg-save-conversion-table'
MEG_API_RUN_BIDSIFY = '/meg-run-bidsify'
MEG_API_BIDSIFY_PROGRESS = '/meg-bidsify-progress'


# PET BIDSification: Config Handler Investigation Plan

## Goal

Make PET BIDSification behavior as close as possible to MR BIDSification:

- Keep a config-driven workflow from the PET UI.
- Keep per-session recoding for participant/session IDs.
- Replace the PET execution backend from dcm2bids to pypet2bids.

## Findings

### 1) Current MR model in this app

- MR uses a config JSON file and a recode table.
- The recode table is persisted and applied just before conversion.
- Conversion executes in a background job and streams logs to the UI.

### 2) Current PET model in this app

- PET already has recode persistence helpers and PET-side recode endpoints.
- PET currently has a disabled run button in the Make BIDS tab.

### 3) pypet2bids behavior (upstream)

- `dcm2niix4pet` has no native dcm2bids-style config-file parser.
- It does support:
  - `--kwargs` for metadata override/injection.
  - `--dcm2niix-options` for pass-through dcm2niix options.
  - defaults from `PET2BIDS_DCM2NIIX_OPTIONS` in env or `.pet2bidsconfig`.
- It internally controls `-f` (file format) behavior and writes into a temp directory before post-processing.

## Recommended approach

Implement a local adapter layer that consumes `dcm2bids_config_pet.json` and translates each selected config row into a pypet2bids invocation.

### Adapter responsibilities

1. Load `dcm2bids_config_pet.json`.
2. Discover PET sessions from input root (`raw/pet` in UI requirements).
3. Apply recode table (`sessions_recode_mr.csv` / `sessions_recode_pet.csv`) exactly as MR does.
4. For each selected session:
   - Evaluate config criteria against helper metadata.
   - Build target BIDS destination path using recoded IDs and entities (`trc`, `rec`, `run`).
   - Build `--kwargs` payload from mapped metadata fields.
   - Call `dcm2niix4pet`.
5. Stream per-session logs and status back to UI.

## Practical mapping from dcm2bids config to pypet2bids

### Use directly

- `custom_entities`:
  - `trc-*` -> `--trc`
  - `rec-*` -> `--rec`
  - `run-*` -> `--run`

### Keep as internal filter-only

- `criteria` fields should control whether a config row applies to a session/series.

### Not currently supported by pypet2bids naming

- `desc-*` entity is not currently part of dcm2niix4pet destination naming.
- Recommended short-term behavior: ignore `desc` for filename creation and log a warning.

## Proposed implementation in this repository

### New backend module

- Add `tabs/pet-bids/pet2bids_runner.py`.
- Responsibilities:
  - Parse PET config + helper metadata.
  - Build container-ready PET conversion plans.
  - Run worker pool + stream logs.

### Container entrypoint

- Add `tabs/pet-bids/pet2bids_container_entrypoint.py`.
- Responsibilities:
  - Consume the JSON plan emitted by the host runner.
  - Execute the pinned PET runtime inside the container image.

### Routes update

- In `tabs/pet-bids/routes.py`:
  - Keep existing recode endpoints.
  - Add PET run endpoint variant for pypet2bids, e.g. `/pet-run-pypet2bids`.
  - Add stream endpoint, e.g. `/pet-stream-pypet2bids-job`.

### UI update

- In `tabs/pet-bids/tab.html`:
  - Re-enable run button when backend endpoint is ready.
  - Keep MR-parity session selection + recoding UX.
  - Keep "coming soon" text until route is implemented.

## Apptainer / Singularity plan

Target location for shared container asset:

- `/scratch/singularityContainers/pet2bids.sif`

### Container contents

1. Python runtime + pypet2bids.
2. dcm2niix binary (pinned version).
3. Entry script that accepts:
   - input session path
   - destination path
   - translated kwargs
   - dcm2niix options

### Execution model

- App route launches:
  - `apptainer exec ... pet2bids-runner ...`
- Bind mounts:
  - project root (read/write as needed)
  - temp workdir

### Why containerize this step

- Decouples pypet2bids dependency stack from host/server env.
- Makes behavior reproducible across users and nodes.
- Simplifies pinning dcm2niix and pypet2bids versions.

## Incremental delivery order

1. Implement the host-side adapter and wire the UI run button.
2. Validate parity with MR recode and session selection behavior.
3. Add Apptainer definition and switch execution path to containerized command.
4. Add smoke tests for:
   - recode application
   - config row matching
   - expected destination naming/entities

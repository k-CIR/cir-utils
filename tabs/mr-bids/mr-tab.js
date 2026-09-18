/* ── MR BIDS tab namespace ─────────────────────────────────────────────────── */
const mrBids = (() => {
  /* shared token reference — set by tabInit_mr_bids */
  let _token = null;
  let _mrConfigFile = null;

  function _addToken(url) {
    if (!_token) return url;
    return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(_token);
  }

  /* ── Utility ── */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function escAttr(s) {
    return esc(s).replace(/"/g, '&quot;');
  }

  /* ── Config builder state ── */
  let tableRows  = [];
  let tableState = [];
  const stateMap = new Map();

  function _stateKey(row) {
    return String(row.series_number) + '|' + row.series_description
      + '|' + (row.pulse_sequence_name || '') + '|' + (row.image_type || '');
  }

  const SUFFIX_OPTS = {
    anat: ['T1w','T2w','T2starw','PDw','FLAIR','T1map','T2map','FLASH','PD','PDmap','PDT2','inplaneT1','inplaneT2','angio'],
    func: ['bold','sbref','cbv'],
    dwi:  ['dwi','sbref'],
    fmap: ['phasediff','phase1','phase2','magnitude1','magnitude2','fieldmap','epi','TB1*'],
    perf: ['asl','m0scan','cbf'],
  };

  /* ── SSE helper output ── */
  let _activeSource = null;

  function runHelper() {
    const btn     = document.getElementById('analyze-btn');
    const out     = document.getElementById('helper-output');
    const relPath = document.getElementById('helper-path').value.trim();

    if (!relPath) {
      out.innerHTML = '<span class="t-err">Please enter a path.</span>';
      return;
    }
    if (_activeSource) { _activeSource.close(); _activeSource = null; }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Analyzing\u2026';
    out.innerHTML = '';

    const force = document.getElementById('force-check').checked ? '1' : '0';
    const url   = '/mr-run-dcm2bids-helper?path=' + encodeURIComponent(relPath) + '&force=' + force;
    _activeSource = new EventSource(_addToken(url));

    _activeSource.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.error === 'invalid_path') {
        _activeSource.close(); _activeSource = null;
        btn.disabled = false;
        btn.textContent = 'Analyze DICOM fields';
        out.innerHTML = '<span class="t-err">Invalid path.</span>';
        return;
      }
      if (msg.done) {
        _activeSource.close(); _activeSource = null;
        btn.disabled = false;
        btn.textContent = 'Analyze DICOM fields';
        const ok = msg.returncode === 0;
        out.innerHTML += '<span class="' + (ok ? 't-ok' : 't-err') + '">'
          + esc(ok ? '\n\u2713 Done.' : '\n\u2717 Exited with code ' + msg.returncode + '.') + '</span>';
        out.scrollTop = out.scrollHeight;
        if (ok) loadHelperSummary(false);
      } else {
        out.innerHTML += esc(msg.line) + '\n';
        out.scrollTop = out.scrollHeight;
      }
    };

    _activeSource.onerror = () => {
      _activeSource.close(); _activeSource = null;
      btn.disabled = false;
      btn.textContent = 'Analyze DICOM fields';
      out.innerHTML += '<span class="t-err">\nConnection error.</span>';
    };
  }

  /* ── Config builder ── */
  async function loadHelperSummary(prePopulate) {
    tableRows.forEach((row, i) =>
      stateMap.set(_stateKey(row), JSON.parse(JSON.stringify(tableState[i])))
    );
    try {
      const resp = await fetch(_addToken('/mr-get-helper-summary'));
      if (resp.status === 401) { window._handleAuthError(); return; }
      const data = await resp.json();
      tableRows  = data.rows || [];
      tableState = tableRows.map(row => {
        const saved = stateMap.get(_stateKey(row));
        return saved || {
          selected: { series_number: false, series_description: false, pulse_sequence_name: false, image_type: false },
          datatype: '', task_name: '', acq: '', run: '', desc: '', suffix: '', other_suffix: ''
        };
      });
      if (prePopulate) {
        const cfgResp = await fetch(_addToken('/mr-get-bids-config'));
        if (cfgResp.ok) {
          const cfgData = await cfgResp.json();
          if (cfgData.config) _prePopulateFromConfig(cfgData.config);
        }
      }
      renderTable();
    } catch (e) {
      console.error('Failed to load helper summary:', e);
    }
  }

  function _prePopulateFromConfig(configData) {
    if (!configData || !Array.isArray(configData.descriptions)) return;
    for (const desc of configData.descriptions) {
      const criteria = desc.criteria || {};
      if (!Object.keys(criteria).length) continue;
      const idx = tableRows.findIndex(row => {
        if ('SeriesDescription' in criteria && criteria.SeriesDescription !== row.series_description) return false;
        if ('SeriesNumber'      in criteria && Number(criteria.SeriesNumber) !== row.series_number)   return false;
        if ('PulseSequenceName' in criteria && criteria.PulseSequenceName !== row.pulse_sequence_name) return false;
        if ('ImageType' in criteria) {
          const rowList = (row.image_type || '').split(',').map(x => x.trim()).filter(Boolean);
          const criList = Array.isArray(criteria.ImageType) ? criteria.ImageType : [];
          if (criList.some(v => !rowList.includes(v))) return false;
        }
        return true;
      });
      if (idx < 0) continue;
      const s  = tableState[idx];
      const ce = Array.isArray(desc.custom_entities) ? desc.custom_entities : [];
      const sc = desc.sidecar_changes || {};
      if ('SeriesDescription' in criteria) s.selected.series_description  = true;
      if ('SeriesNumber'      in criteria) s.selected.series_number       = true;
      if ('PulseSequenceName' in criteria) s.selected.pulse_sequence_name = true;
      if ('ImageType'         in criteria) s.selected.image_type          = true;
      s.datatype = desc.datatype || '';
      const knownSuffixes = SUFFIX_OPTS[s.datatype] || [];
      if (knownSuffixes.includes(desc.suffix)) {
        s.suffix = desc.suffix || ''; s.other_suffix = '';
      } else if (desc.suffix) {
        s.suffix = ''; s.other_suffix = desc.suffix;
      } else {
        s.suffix = ''; s.other_suffix = '';
      }
      s.task_name    = sc.TaskName    || '';
      const acqEnt  = ce.find(e => e.startsWith('acq-'));
      const runEnt  = ce.find(e => e.startsWith('run-'));
      const descEnt = ce.find(e => e.startsWith('desc-'));
      s.acq  = acqEnt  ? acqEnt.slice(4)  : '';
      s.run  = runEnt  ? runEnt.slice(4)  : '';
      s.desc = descEnt ? descEnt.slice(5) : '';
    }
  }

  function renderTable() {
    const section   = document.getElementById('config-builder-section');
    const container = document.getElementById('config-table-container');
    const warnEl    = document.getElementById('config-dup-warn');
    if (!tableRows.length) { section.style.display = 'none'; return; }
    section.style.display = '';

    const hasDups = tableRows.some(r => r.duplicate_count > 0);
    warnEl.textContent = hasDups
      ? '\u26a0\ufe0f Some series appear multiple times (e.g. echo variants or magnitude/phase image pairs). '
        + 'One representative row is shown per unique combination of input fields.'
      : '';
    warnEl.style.display = hasDups ? '' : 'none';

    const dtypes = ['anat', 'func', 'dwi', 'perf', 'fmap'];
    let html = '<table class="config-table"><thead><tr>'
      + '<th colspan="4">Input</th>'
      + '<th colspan="7" class="col-output"><strong>Output</strong></th>'
      + '</tr><tr>'
      + '<th>Series number</th>'
      + '<th>Series description</th>'
      + '<th>Pulse sequence name</th>'
      + '<th>Image type</th>'
      + '<th class="col-output"><strong>Data type <span style="color:#f48771">*</span></strong></th>'
      + '<th>Task name</th>'
      + '<th>Acquisition</th>'
      + '<th>Run</th>'
      + '<th>Description</th>'
      + '<th><strong>Suffix <span style="color:#f48771">*</span></strong></th>'
      + '<th>Other suffix</th>'
      + '</tr></thead><tbody>';

    tableRows.forEach((row, i) => {
      const s       = tableState[i];
      const dupMark = row.duplicate_count > 0 ? ' \u26a0\ufe0f' : '';
      const dtypeOpts = dtypes.map(v =>
        '<option value="' + v + '"' + (s.datatype === v ? ' selected' : '') + '>' + v + '</option>'
      ).join('');
      const suffixOpts = (SUFFIX_OPTS[s.datatype] || []).map(v =>
        '<option value="' + v + '"' + (s.suffix === v ? ' selected' : '') + '>' + v + '</option>'
      ).join('');
      html += '<tr>'
        + '<td class="input-cell' + (s.selected.series_number       ? ' cell-selected':'')
          + '" onclick="mrBids.toggleCell(' + i + ',\'series_number\')">'
          + esc(String(row.series_number ?? '')) + '</td>'
        + '<td class="input-cell' + (s.selected.series_description  ? ' cell-selected':'')
          + '" onclick="mrBids.toggleCell(' + i + ',\'series_description\')">'
          + esc(row.series_description) + dupMark + '</td>'
        + '<td class="input-cell' + (s.selected.pulse_sequence_name ? ' cell-selected':'')
          + '" onclick="mrBids.toggleCell(' + i + ',\'pulse_sequence_name\')">'
          + esc(row.pulse_sequence_name || '') + '</td>'
        + '<td class="input-cell' + (s.selected.image_type          ? ' cell-selected':'')
          + '" onclick="mrBids.toggleCell(' + i + ',\'image_type\')">'
          + esc(row.image_type || '') + '</td>'
        + '<td class="col-output"><select class="tbl-select" onchange="mrBids.updateDatatype(' + i + ',this.value)">'
          + '<option value="">--</option>' + dtypeOpts + '</select></td>'
        + '<td><input class="tbl-input" type="text" value="' + escAttr(s.task_name) + '" placeholder="(optional)" '
          + 'oninput="mrBids.updateState(' + i + ',\'task_name\',this.value)"></td>'
        + '<td><input class="tbl-input" type="text" value="' + escAttr(s.acq) + '" placeholder="(optional)" '
          + 'oninput="mrBids.updateState(' + i + ',\'acq\',this.value)"></td>'
        + '<td><input class="tbl-input" type="text" value="' + escAttr(s.run) + '" placeholder="(optional)" '
          + 'oninput="mrBids.updateState(' + i + ',\'run\',this.value)"></td>'
        + '<td><input class="tbl-input" type="text" value="' + escAttr(s.desc || '') + '" placeholder="(optional)" '
          + 'oninput="mrBids.updateState(' + i + ',\'desc\',this.value)"></td>'
        + '<td><select class="tbl-select" onchange="mrBids.updateSuffix(' + i + ',this.value)">'
          + '<option value="">--</option>' + suffixOpts + '</select></td>'
        + '<td><input class="tbl-input" type="text" value="' + escAttr(s.other_suffix || '') + '" placeholder="(optional)" '
          + 'oninput="mrBids.updateOtherSuffix(' + i + ',this.value)"></td>'
        + '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function toggleCell(rowIdx, field) {
    tableState[rowIdx].selected[field] = !tableState[rowIdx].selected[field];
    const tbody = document.querySelector('.config-table tbody');
    if (!tbody) return;
    const colIdx = { series_number: 0, series_description: 1, pulse_sequence_name: 2, image_type: 3 }[field] ?? 0;
    tbody.rows[rowIdx].cells[colIdx].classList.toggle('cell-selected', tableState[rowIdx].selected[field]);
  }

  function updateState(rowIdx, field, value) { tableState[rowIdx][field] = value; }

  function updateDatatype(rowIdx, value) {
    tableState[rowIdx].datatype = value;
    tableState[rowIdx].suffix   = '';
    tableState[rowIdx].other_suffix = '';
    const tbody = document.querySelector('.config-table tbody');
    if (!tbody) return;
    const row  = tbody.rows[rowIdx];
    const opts = (SUFFIX_OPTS[value] || []).map(v => '<option value="' + v + '">' + v + '</option>').join('');
    const sel  = row.cells[9].querySelector('select');
    if (sel) sel.innerHTML = '<option value="">--</option>' + opts;
    const other = row.cells[10].querySelector('input');
    if (other) other.value = '';
  }

  function updateSuffix(rowIdx, value) {
    tableState[rowIdx].suffix       = value;
    tableState[rowIdx].other_suffix = '';
    const tbody = document.querySelector('.config-table tbody');
    if (!tbody) return;
    const other = tbody.rows[rowIdx].cells[10].querySelector('input');
    if (other) other.value = '';
  }

  function updateOtherSuffix(rowIdx, value) {
    tableState[rowIdx].other_suffix = value;
    if (value.trim()) {
      tableState[rowIdx].suffix = '';
      const tbody = document.querySelector('.config-table tbody');
      if (!tbody) return;
      const sel = tbody.rows[rowIdx].cells[9].querySelector('select');
      if (sel) sel.value = '';
    }
  }

  function _buildDescriptions() {
    const descriptions = [];
    tableRows.forEach((row, i) => {
      const s = tableState[i];
      if (!(s.selected.series_number || s.selected.series_description || s.selected.pulse_sequence_name || s.selected.image_type)) return;
      const effectiveSuffix = (s.other_suffix || '').trim() || s.suffix;
      if (!s.datatype || !effectiveSuffix) return;

      const d = { datatype: s.datatype, suffix: effectiveSuffix };
      const ce = [];
      if (s.task_name.trim()) ce.push('task-' + s.task_name.trim());
      if (s.acq.trim())       ce.push('acq-'  + s.acq.trim());
      if (s.run.trim())       ce.push('run-'  + s.run.trim());
      if (s.desc.trim())      ce.push('desc-' + s.desc.trim());
      if (ce.length) d.custom_entities = ce;

      d.criteria = {};
      if (s.selected.series_description)  d.criteria.SeriesDescription = row.series_description;
      if (s.selected.series_number)       d.criteria.SeriesNumber      = row.series_number;
      if (s.selected.pulse_sequence_name) d.criteria.PulseSequenceName = row.pulse_sequence_name;
      if (s.selected.image_type)          d.criteria.ImageType         = (row.image_type || '').split(',').map(v => v.trim()).filter(Boolean);

      const sc = {};
      if (s.task_name.trim())    sc.TaskName    = s.task_name.trim();
      if (Object.keys(sc).length) d.sidecar_changes = sc;

      descriptions.push(d);
    });
    return descriptions;
  }

  async function _doSaveConfig(payload) {
    const resp = await fetch(_addToken('/mr-save-bids-config'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return !!(await resp.json()).ok;
  }

  async function generateConfig() {
    const statusEl = document.getElementById('generate-status');
    const descriptions = _buildDescriptions();
    if (!descriptions.length) {
      statusEl.innerHTML = '<span class="t-err">No rows ready: each needs at least one selected input cell, a data type, and a suffix.</span>';
      return;
    }
    try {
      const check = await fetch(_addToken('/mr-get-bids-config'));
      if (check.ok) {
        const cd = await check.json();
        if (cd.config !== null) {
          const confirmed = confirm('dcm2bids_config_mr.json already exists.\n\nOverwrite it with '
            + descriptions.length + ' description' + (descriptions.length !== 1 ? 's' : '') + '?');
          if (!confirmed) { statusEl.textContent = ''; return; }
        }
      }
    } catch { /* fall through */ }
    try {
      const ok = await _doSaveConfig({ descriptions });
      statusEl.innerHTML = ok
        ? '<span class="t-ok">\u2713 Saved dcm2bids_config_mr.json (' + descriptions.length + ' description' + (descriptions.length !== 1 ? 's' : '') + ')</span>'
        : '<span class="t-err">\u2717 Server error saving config.</span>';
    } catch {
      statusEl.innerHTML = '<span class="t-err">\u2717 Failed to reach server.</span>';
    }
  }

  async function appendToConfig() {
    const statusEl = document.getElementById('generate-status');
    const newDescs = _buildDescriptions();
    if (!newDescs.length) {
      statusEl.innerHTML = '<span class="t-err">No rows ready: each needs at least one selected input cell, a data type, and a suffix.</span>';
      return;
    }
    let existing = [];
    try {
      const resp = await fetch(_addToken('/mr-get-bids-config'));
      if (resp.ok) {
        const d = await resp.json();
        if (d.config && Array.isArray(d.config.descriptions)) existing = d.config.descriptions;
      }
    } catch { /* start fresh */ }
    const merged = existing.concat(newDescs);
    try {
      const ok = await _doSaveConfig({ descriptions: merged });
      statusEl.innerHTML = ok
        ? '<span class="t-ok">\u2713 Appended ' + newDescs.length + ' description' + (newDescs.length !== 1 ? 's' : '') + ' (total: ' + merged.length + ')</span>'
        : '<span class="t-err">\u2717 Server error saving config.</span>';
    } catch {
      statusEl.innerHTML = '<span class="t-err">\u2717 Failed to reach server.</span>';
    }
  }

  async function loadConfigToTable() {
    const statusEl = document.getElementById('generate-status');
    try {
      const resp = await fetch(_addToken('/mr-get-bids-config'));
      if (resp.status === 401) { window._handleAuthError(); return; }
      const data = await resp.json();
      if (!data.config) {
        statusEl.innerHTML = '<span class="t-err">No config file found.</span>';
        return;
      }
      tableState = tableRows.map(() => ({
        selected: { series_number: false, series_description: false, pulse_sequence_name: false, image_type: false },
        datatype: '', task_name: '', acq: '', run: '', desc: '', suffix: '', other_suffix: ''
      }));
      _prePopulateFromConfig(data.config);
      renderTable();
      statusEl.innerHTML = '<span class="t-ok">\u2713 Config loaded into table</span>';
    } catch {
      statusEl.innerHTML = '<span class="t-err">\u2717 Failed to load config.</span>';
    }
  }

  /* ── Config editor ── */
  function validateEditorJson() {
    const ta   = document.getElementById('config-editor-text');
    const warn = document.getElementById('editor-json-warn');
    if (!ta.value.trim()) { warn.style.display = 'none'; return; }
    try {
      JSON.parse(ta.value);
      warn.style.display = 'none';
    } catch (e) {
      warn.textContent = '\u26a0 Invalid JSON: ' + e.message;
      warn.style.display = '';
    }
  }

  function updateLineNumbers() {
    const ta     = document.getElementById('config-editor-text');
    const gutter = document.getElementById('editor-lines');
    const count  = ta.value.split('\n').length;
    gutter.textContent = Array.from({length: count}, (_, i) => i + 1).join('\n');
  }

  function syncLineScroll() {
    const ta     = document.getElementById('config-editor-text');
    const gutter = document.getElementById('editor-lines');
    gutter.scrollTop = ta.scrollTop;
  }

  async function loadConfigEditor() {
    const ta     = document.getElementById('config-editor-text');
    const status = document.getElementById('editor-status');
    try {
      const resp = await fetch(_addToken('/mr-get-bids-config'));
      if (resp.status === 401) { window._handleAuthError(); return; }
      const data = await resp.json();
      ta.value = data.config ? JSON.stringify(data.config, null, 4) : '';
      status.textContent = '';
      updateLineNumbers();
    } catch {
      status.innerHTML = '<span class="t-err">Failed to load config.</span>';
    }
  }

  async function saveConfigEditor() {
    const ta     = document.getElementById('config-editor-text');
    const status = document.getElementById('editor-status');
    let parsed;
    try { parsed = JSON.parse(ta.value); }
    catch (e) {
      status.innerHTML = '<span class="t-err">\u2717 Invalid JSON: ' + esc(e.message) + '</span>';
      return;
    }
    try {
      const resp = await fetch(_addToken('/mr-save-bids-config'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      });
      const data = await resp.json();
      status.innerHTML = data.ok
        ? '<span class="t-ok">\u2713 Saved</span>'
        : '<span class="t-err">\u2717 Server error.</span>';
    } catch {
      status.innerHTML = '<span class="t-err">\u2717 Failed to reach server.</span>';
    }
  }

  /* ── Make BIDS ── */
  let _bidsDiscoveredSessions = [];
  const _SESSION_COLORS = ['#9cdcfe','#4ec9b0','#dcdcaa','#ce9178','#c586c0','#569cd6','#4fc1ff','#b5cea8'];
  function _sessionColor(idx) { return _SESSION_COLORS[idx % _SESSION_COLORS.length]; }

  async function discoverSessions() {
    const dicomRel = document.getElementById('bids-dicom-path').value.trim();
    if (!dicomRel) { alert('Enter a DICOM input path first.'); return; }
    const btn = document.getElementById('discover-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Discovering\u2026';
    document.getElementById('bids-run-status').textContent = '';
    document.getElementById('bids-sessions-section').style.display = 'none';
    if (document.querySelectorAll('.bids-recode-input').length > 0) await _saveRecodeTable();
    try {
      const [sesRes, recRes] = await Promise.all([
        fetch(_addToken('/mr-discover-sessions?dicom_root=' + encodeURIComponent(dicomRel))),
        fetch(_addToken('/mr-get-recode-table')),
      ]);
      if (!sesRes.ok) throw new Error('HTTP ' + sesRes.status);
      const sesData = await sesRes.json();
      if (sesData.error) throw new Error(sesData.error);
      const recData = recRes.ok ? await recRes.json() : {};
      _bidsDiscoveredSessions = sesData.sessions || [];
      renderSessionList(_bidsDiscoveredSessions, recData.recode || {});
    } catch (e) {
      document.getElementById('bids-session-count').textContent = 'Error: ' + e.message;
      document.getElementById('bids-session-table-wrap').innerHTML = '';
      document.getElementById('bids-sessions-section').style.display = '';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Discover sessions';
    }
  }

  function renderSessionList(sessions, recodeMap) {
    recodeMap = recodeMap || {};
    const countEl   = document.getElementById('bids-session-count');
    const tableWrap = document.getElementById('bids-session-table-wrap');
    const section   = document.getElementById('bids-sessions-section');
    countEl.textContent = sessions.length + ' session' + (sessions.length !== 1 ? 's' : '') + ' found';
    if (sessions.length === 0) {
      tableWrap.innerHTML = '<p style="padding:0.6rem 1rem; color:#9e9e9e; font-size:0.87rem;">No sessions found in that directory.</p>';
      section.style.display = '';
      return;
    }
    let html = '<table class="config-table" style="font-size:0.85rem;">';
    html += '<thead><tr><th>Label</th><th>Participant</th><th>Recoded participant ID</th>'
          + '<th>Session</th><th>Recoded session ID</th><th style="text-align:center;">Include</th></tr></thead><tbody>';
    sessions.forEach((s, i) => {
      const color  = _sessionColor(i);
      const rec    = recodeMap[s.label] || {};
      const rpVal  = rec.recoded_participant || '';
      const rsVal  = rec.recoded_session     || '';
      const pPad   = s.participant ? s.participant.padStart(3, '0') : '';
      const sesPad = s.session     ? s.session.padStart(2, '0')     : '';
      html += '<tr>';
      html += '<td style="color:' + color + ';">' + esc(s.label) + '</td>';
      html += '<td>' + esc(s.participant || '') + '</td>';
      html += '<td style="min-width:10rem;"><input class="tbl-input bids-recode-input" type="text"'
           +  ' data-label="' + escAttr(s.label) + '" data-field="recoded_participant" data-pad="3"'
           +  ' value="' + escAttr(rpVal) + '" placeholder="' + escAttr(pPad ? 'keep ' + pPad : 'keep as is') + '"'
           +  ' oninput="mrBids.validateRecodeInput(this)" onblur="mrBids.debouncedSaveRecode()" />'
           +  '<div class="recode-hint"></div></td>';
      html += '<td>' + esc(s.session || '\u2014') + '</td>';
      html += '<td style="min-width:10rem;"><input class="tbl-input bids-recode-input" type="text"'
           +  ' data-label="' + escAttr(s.label) + '" data-field="recoded_session" data-pad="2"'
           +  ' value="' + escAttr(rsVal) + '" placeholder="' + escAttr(sesPad ? 'keep ' + sesPad : (s.session != null ? 'keep as is' : 'none \u2014 add?')) + '"'
           +  ' oninput="mrBids.validateRecodeInput(this)" onblur="mrBids.debouncedSaveRecode()" />'
           +  '<div class="recode-hint"></div></td>';
      html += '<td style="text-align:center;"><input type="checkbox" class="bids-ses-check" data-label="'
           +  escAttr(s.label) + '" checked style="accent-color:#0e639c;"></td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
    tableWrap.innerHTML = html;
    document.querySelectorAll('.bids-recode-input').forEach(inp => { if (inp.value) validateRecodeInput(inp); });
    section.style.display = '';
  }

  function bidsSelectAll(checked) {
    document.querySelectorAll('.bids-ses-check').forEach(cb => { cb.checked = checked; });
  }

  function validateRecodeInput(input) {
    const val    = input.value.trim();
    const padLen = parseInt(input.dataset.pad, 10) || 3;
    const hint   = input.nextElementSibling;
    input.value  = val;
    if (!val) { input.style.borderColor = ''; if (hint) { hint.textContent=''; hint.style.color=''; } return true; }
    if (!/^\d+$/.test(val)) {
      input.style.borderColor = '#f48771';
      if (hint) { hint.textContent='Digits only'; hint.style.color='#f48771'; }
      return false;
    }
    const padded = val.padStart(padLen, '0');
    if (val !== padded) {
      input.style.borderColor = '#d7ba7d';
      if (hint) { hint.textContent='Recommended: '+padded; hint.style.color='#d7ba7d'; }
    } else {
      input.style.borderColor = '#4ec9b0';
      if (hint) { hint.textContent=''; hint.style.color=''; }
    }
    return true;
  }

  let _recodeTimer = null;
  function debouncedSaveRecode() {
    clearTimeout(_recodeTimer);
    _recodeTimer = setTimeout(_saveRecodeTable, 600);
  }

  async function _saveRecodeTable() {
    const recode = {};
    document.querySelectorAll('.bids-recode-input').forEach(inp => {
      const label = inp.dataset.label;
      const field = inp.dataset.field;
      if (!recode[label]) recode[label] = {};
      recode[label][field] = inp.value.trim();
    });
    try {
      await fetch(_addToken('/mr-save-recode-table'), {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({recode}),
      });
      const el = document.getElementById('bids-recode-status');
      if (el) { el.innerHTML='<span class="t-ok">\u2713 Recode saved</span>'; setTimeout(()=>{el.innerHTML='';},2000); }
    } catch { /* silent */ }
  }

  async function runDcm2bids() {
    const dicomRel  = document.getElementById('bids-dicom-path').value.trim();
    const outputRel = document.getElementById('bids-output-path').value.trim();
    const configRel = _mrConfigFile;
    if (!configRel) {
      document.getElementById('bids-run-status').innerHTML = '<span class="t-err">Config file path not loaded. Please reload the page.</span>';
      return;
    }
    const workers   = parseInt(document.getElementById('bids-workers').value, 10) || 8;
    const clobber   = document.getElementById('bids-clobber').checked;
    const btn       = document.getElementById('run-bids-btn');
    const statusEl  = document.getElementById('bids-run-status');
    const selectedLabels = Array.from(document.querySelectorAll('.bids-ses-check:checked')).map(cb => cb.dataset.label);
    if (selectedLabels.length === 0) { statusEl.innerHTML='<span class="t-err">No sessions selected.</span>'; return; }
    const recode = {};
    let hasErr = false;
    document.querySelectorAll('.bids-recode-input').forEach(inp => {
      if (!validateRecodeInput(inp)) hasErr = true;
      if (!recode[inp.dataset.label]) recode[inp.dataset.label] = {};
      recode[inp.dataset.label][inp.dataset.field] = inp.value.trim();
    });
    if (hasErr) { statusEl.innerHTML='<span class="t-err">Fix recode errors before running.</span>'; return; }
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Starting\u2026';
    statusEl.textContent = '';
    const logEl = document.getElementById('bids-log');
    logEl.style.display = ''; logEl.innerHTML = '';
    try {
      const resp = await fetch(_addToken('/mr-run-dcm2bids'), {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ dicom_root:dicomRel, output_dir:outputRel, config_file:configRel,
          sessions:selectedLabels, max_workers:workers, clobber, recode }),
      });
      const data = await resp.json();
      if (data.error) {
        statusEl.innerHTML='<span class="t-err">\u2717 '+esc(data.error)+'</span>';
        btn.disabled=false; btn.textContent='Run dcm2bids'; return;
      }
      _streamBidsJob(data.job_id, btn, statusEl);
    } catch (e) {
      statusEl.innerHTML='<span class="t-err">\u2717 '+esc(e.message)+'</span>';
      btn.disabled=false; btn.textContent='Run dcm2bids';
    }
  }

  function _streamBidsJob(jobId, btn, statusEl) {
    btn.innerHTML = '<span class="spinner"></span> Running\u2026';
    const logEl    = document.getElementById('bids-log');
    const colorMap = {};
    _bidsDiscoveredSessions.forEach((s, i) => { colorMap[s.label] = _sessionColor(i); });
    const src = new EventSource(_addToken('/mr-stream-dcm2bids-job?job_id=' + encodeURIComponent(jobId)));
    src.onmessage = (ev) => {
      const msg   = JSON.parse(ev.data);
      const label = msg.label || '';
      const color = colorMap[label] || '#c8c8c8';
      const lbl   = label ? '<span style="color:'+color+';">['+esc(label)+']</span> ' : '';
      if      (msg.type === 'start') logEl.innerHTML += '<span style="color:'+color+';">['+esc(label)+'] Starting\u2026</span>\n';
      else if (msg.type === 'line')  logEl.innerHTML += lbl + esc(msg.text) + '\n';
      else if (msg.type === 'exit')  logEl.innerHTML += lbl + '<span class="'+(msg.returncode===0?'t-ok':'t-err')+'">'+(msg.returncode===0?'\u2713 Done (exit 0)':'\u2717 Exited with code '+msg.returncode)+'</span>\n';
      else if (msg.type === 'error') logEl.innerHTML += '<span class="t-err">'+lbl+esc(msg.text||'Unknown error')+'</span>\n';
      else if (msg.type === 'done')  {
        logEl.innerHTML += '<span class="t-ok">\n\u2713 All sessions complete.</span>\n';
        src.close(); btn.disabled=false; btn.textContent='Run dcm2bids';
        statusEl.innerHTML='<span class="t-ok">\u2713 Batch complete</span>';
      }
      logEl.scrollTop = logEl.scrollHeight;
    };
    src.onerror = () => {
      src.close(); btn.disabled=false; btn.textContent='Run dcm2bids';
      if (!logEl.textContent.includes('All sessions complete')) {
        logEl.innerHTML += '<span class="t-err">Connection lost.</span>\n';
        statusEl.innerHTML = '<span class="t-err">\u2717 Stream disconnected</span>';
      }
      logEl.scrollTop = logEl.scrollHeight;
    };
  }

  /* ── Public API ── */
  return {
    async init(token) {
      _token = token;
      
      function _normalizeDisplayPath(value) {
        let path = String(value || '').trim().replace(/\\/g, '/');
        while (path.startsWith('../')) {
          path = path.slice(3);
        }
        while (path.startsWith('./')) {
          path = path.slice(2);
        }
        return path;
      }
      
      try {
        const cfg = await fetch(_addToken('/mr-get-config')).then(r => r.json());
        if (cfg.project_root) {
          document.getElementById('path-static').textContent          = cfg.project_root;
          document.getElementById('bids-root-static').textContent     = cfg.project_root;
          document.getElementById('bids-out-static').textContent      = cfg.project_root;
          document.getElementById('bids-cfg-root-static').textContent = cfg.project_root;
        }
        if (cfg.config_file) {
          _mrConfigFile = cfg.config_file;
        }
        if (cfg.default_path) {
          const displayPath = _normalizeDisplayPath(cfg.default_path);
          document.getElementById('helper-path').value = displayPath;
        }
        if (cfg.warning) {
          const w = document.getElementById('path-warn');
          w.textContent = '⚠ ' + cfg.warning;
          w.style.display = '';
        }
      } catch (_) {}
      loadHelperSummary(true);
    },
    switchSubTab(name) {
      ['config', 'editor', 'bids'].forEach(n => {
        document.getElementById('tab-' + n).classList.toggle('active', n === name);
        document.getElementById('mr-subtab-' + n).classList.toggle('active', n === name);
      });
      if (name === 'editor') loadConfigEditor();
    },
    runHelper,
    toggleCell, updateState, updateDatatype, updateSuffix, updateOtherSuffix,
    generateConfig, appendToConfig, loadConfigToTable,
    validateEditorJson, updateLineNumbers, syncLineScroll, loadConfigEditor, saveConfigEditor,
    discoverSessions, bidsSelectAll, validateRecodeInput, debouncedSaveRecode, runDcm2bids,
    /* Called by shell when the outer MR BIDS tab is re-activated */
    onTabSwitch(_name) {
      const editorPane = document.getElementById('tab-editor');
      if (editorPane && editorPane.classList.contains('active')) loadConfigEditor();
    },
  };
})();

function tabInit_mr_bids(token) { mrBids.init(token); }

// Register with the shell so tab-switch notifications reach this module.
if (typeof registerTabModule === 'function') registerTabModule('mr-bids', mrBids);

/* App wiring: camera + voice + agent API.
 *
 * The one piece of real logic here is that when the user says something that
 * sounds like leaving, we attach the current camera frame to the turn. The
 * agent cannot tell you what you forgot if it cannot see what you have.
 */
(function () {
  'use strict';

  var els = {
    status: document.getElementById('status'),
    dot: document.querySelector('.dot'),
    video: document.getElementById('video'),
    canvas: document.getElementById('canvas'),
    viewportEmpty: document.getElementById('viewportEmpty'),
    scanFlash: document.getElementById('scanFlash'),
    cameraToggle: document.getElementById('cameraToggle'),
    flipCamera: document.getElementById('flipCamera'),
    cameraHint: document.getElementById('cameraHint'),
    composer: document.getElementById('composer'),
    input: document.getElementById('input'),
    sendBtn: document.getElementById('sendBtn'),
    micBtn: document.getElementById('micBtn'),
    listenHint: document.getElementById('listenHint'),
    transcript: document.getElementById('transcript'),
    speakToggle: document.getElementById('speakToggle'),
    refreshState: document.getElementById('refreshState'),
    panels: {
      observations: document.getElementById('panel-observations'),
      routines: document.getElementById('panel-routines')
    }
  };

  var camera = new Camera(els.video, els.canvas);
  var state = { sessionId: null, userId: null, busy: false };

  /* Utterances that should carry a camera frame. Mirrors the server-side
     leave-detection rules; a false positive only costs one extra frame. */
  var LEAVING = /\b(leav|head(ing)?\s*out|going to|off to|on my way|about to leave|going out)\b/i;

  /* ---------------- API ---------------- */
  async function api(path, options) {
    var res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
    var body = null;
    try { body = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) {
      throw new Error((body && (body.detail || body.message)) || ('Request failed (' + res.status + ')'));
    }
    return body;
  }

  /* ---------------- boot ---------------- */
  async function boot() {
    try {
      var cfg = await api('/api/config');
      state.sessionId = cfg.session_id;
      state.userId = cfg.user_id;
      renderStatus(cfg.subsystems);
    } catch (err) {
      renderStatus(null);
      addMessage('agent', 'Could not reach the agent service: ' + err.message, { error: true });
    }
    if (!Speaker.isSupported()) { els.speakToggle.disabled = true; els.speakToggle.checked = false; }
    var listener = setupVoice();
    if (!listener) { els.micBtn.disabled = true; els.micBtn.title = 'Voice input is not supported in this browser'; }
    await refreshKnowledge();
  }

  function renderStatus(subsystems) {
    var map = {
      gemini: subsystems && subsystems.gemini,
      postgres: subsystems && subsystems.postgres,
      redis: subsystems && subsystems.redis
    };
    els.status.querySelectorAll('.pill').forEach(function (pill) {
      var value = map[pill.dataset.key];
      pill.classList.remove('ok', 'warn', 'bad');
      if (!value) { pill.classList.add('bad'); pill.textContent = pill.dataset.key + ': ?'; return; }
      var healthy = /live/.test(value);
      pill.classList.add(healthy ? 'ok' : 'warn');
      pill.textContent = pill.dataset.key + (healthy ? '' : ': fallback');
    });
    els.dot.classList.toggle('live', !!subsystems);
  }

  /* ---------------- camera ---------------- */
  els.cameraToggle.addEventListener('click', async function () {
    if (camera.isRunning()) {
      camera.stop();
      els.cameraToggle.textContent = 'Start camera';
      els.cameraHint.textContent = 'off';
      els.viewportEmpty.hidden = false;
      els.flipCamera.hidden = true;
      return;
    }
    els.cameraToggle.disabled = true;
    els.cameraHint.textContent = 'starting…';
    try {
      await camera.start();
      els.cameraToggle.textContent = 'Stop camera';
      els.cameraHint.textContent = 'live';
      els.viewportEmpty.hidden = true;
      els.flipCamera.hidden = false;
    } catch (err) {
      els.cameraHint.textContent = 'unavailable';
      addMessage('agent', err.message, { error: true });
    } finally {
      els.cameraToggle.disabled = false;
    }
  });

  els.flipCamera.addEventListener('click', function () { camera.flip(); });

  function captureFrame() {
    var frame = camera.capture();
    if (frame) {
      els.scanFlash.classList.remove('fire');
      void els.scanFlash.offsetWidth;   // restart the CSS animation
      els.scanFlash.classList.add('fire');
    }
    return frame;
  }

  /* ---------------- voice ---------------- */
  function setupVoice() {
    var listener = new Listener({
      onPartial: function (text) { els.input.value = text; },
      onResult: function (text) { els.input.value = text; send(text); },
      onStateChange: function (listening) {
        els.micBtn.classList.toggle('listening', listening);
        els.listenHint.textContent = listening ? 'listening…' : '';
      },
      /* Voice failures used to show only as small grey hint text, which reads
         as the button silently doing nothing. Put them in the transcript where
         the user is already looking. */
      onError: function (message) {
        els.listenHint.textContent = '';
        addMessage('agent', message, { error: true });
      }
    });
    if (!listener.isSupported()) { return null; }
    // Deliberately NOT Speaker.unlock() here: unlocking speaks a silent
    // utterance, and synthesis starting alongside recognition kills the mic.
    // Unlock happens on the send/suggestion gestures instead.
    els.micBtn.addEventListener('click', function () { listener.toggle(); });
    return listener;
  }

  /* ---------------- sending ---------------- */
  els.composer.addEventListener('submit', function (event) {
    event.preventDefault();
    Speaker.unlock();
    send(els.input.value);
  });

  document.querySelectorAll('.chip').forEach(function (chip) {
    chip.addEventListener('click', function () { Speaker.unlock(); send(chip.dataset.say); });
  });

  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) { return; }

    state.busy = true;
    els.sendBtn.disabled = true;
    els.dot.classList.add('busy');
    els.input.value = '';
    addMessage('user', text);
    var thinking = addMessage('agent', 'thinking…', { thinking: true });

    var payload = { text: text, session_id: state.sessionId, user_id: state.userId };
    if (LEAVING.test(text) && camera.isRunning()) { payload.frame = captureFrame(); }

    try {
      var reply = await api('/api/chat', { method: 'POST', body: JSON.stringify(payload) });
      thinking.remove();
      state.sessionId = reply.session_id || state.sessionId;
      var node = addMessage('agent', reply.reply || '(no reply)', { meta: reply });
      renderWorkflow(node, reply.workflow_results);
      if (els.speakToggle.checked) { Speaker.speak(reply.reply); }
      await refreshKnowledge();
    } catch (err) {
      thinking.remove();
      addMessage('agent', err.message, { error: true });
    } finally {
      state.busy = false;
      els.sendBtn.disabled = false;
      els.dot.classList.remove('busy');
    }
  }

  /* ---------------- rendering ---------------- */
  function addMessage(role, text, opts) {
    opts = opts || {};
    var placeholder = els.transcript.querySelector('.placeholder');
    if (placeholder) { placeholder.remove(); }

    var node = document.createElement('div');
    node.className = 'msg ' + role + (opts.thinking ? ' thinking' : '') + (opts.error ? ' error' : '');

    var body = document.createElement('div');
    body.textContent = text;
    node.appendChild(body);

    if (opts.meta && (opts.meta.intent || (opts.meta.tool_calls || []).length)) {
      var meta = document.createElement('div');
      meta.className = 'meta';
      if (opts.meta.intent && opts.meta.intent !== 'unknown') {
        meta.appendChild(tag(opts.meta.intent));
      }
      (opts.meta.tool_calls || []).forEach(function (call) { meta.appendChild(tag(call.name + '()')); });
      if (opts.meta.degraded) { meta.appendChild(tag('no-model fallback')); }
      node.appendChild(meta);
    }

    els.transcript.appendChild(node);
    els.transcript.scrollTop = els.transcript.scrollHeight;
    return node;
  }

  function tag(text) {
    var el = document.createElement('span');
    el.className = 'tag';
    el.textContent = text;
    return el;
  }

  function renderWorkflow(node, results) {
    (results || []).forEach(function (result) {
      if (result.workflow === 'leave_detection') { node.appendChild(leaveResult(result)); }
      else if (result.workflow === 'item_recall') { node.appendChild(recallResult(result)); }
    });
  }

  function section(title) {
    var wrap = document.createElement('div');
    wrap.className = 'result';
    var h = document.createElement('h4');
    h.textContent = title;
    wrap.appendChild(h);
    return wrap;
  }

  function itemRow(names, cls) {
    var row = document.createElement('div');
    row.className = 'items';
    names.forEach(function (name) {
      var chip = document.createElement('span');
      chip.className = 'item ' + cls;
      chip.textContent = name;
      row.appendChild(chip);
    });
    return row;
  }

  function leaveResult(result) {
    var wrap = section('Scan · ' + result.routine + (result.scene ? ' · ' + result.scene : ''));
    var missing = (result.missing_items || []).map(function (m) { return m.item; });
    var found = (result.found_items || []).map(function (f) { return f.name; });
    if (missing.length) { wrap.appendChild(itemRow(missing, 'missing')); }
    if (found.length) { wrap.appendChild(itemRow(found, 'found')); }
    (result.missing_items || []).forEach(function (m) {
      if (!m.hint) { return; }
      var p = document.createElement('div');
      p.className = 'obs-detail';
      p.style.marginTop = '6px';
      p.textContent = m.item + ' — ' + m.hint;
      wrap.appendChild(p);
    });
    return wrap;
  }

  function recallResult(result) {
    var wrap = section('Recall · confidence ' + result.confidence_label + ' (' + result.confidence + ')');
    (result.sightings || []).slice(0, 4).forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'obs';
      var t = document.createElement('time');
      t.textContent = s.time;
      var body = document.createElement('div');
      body.className = 'obs-body';
      var where = document.createElement('div');
      where.className = 'obs-subject';
      /* Keep the specific half ("on the kitchen counter") alongside the coarse
         one ("home") — the specific half is what saves a search. */
      where.textContent = [s.location, s.detail].filter(Boolean).join(' · ') || 'seen';
      var how = document.createElement('div');
      how.className = 'obs-detail';
      how.textContent = s.method + ' · confidence ' + s.confidence;
      body.appendChild(where);
      body.appendChild(how);
      row.appendChild(t);
      row.appendChild(body);
      wrap.appendChild(row);
    });
    return wrap;
  }

  /* ---------------- knowledge panel ---------------- */
  document.querySelectorAll('.tab').forEach(function (tabBtn) {
    tabBtn.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
      tabBtn.classList.add('active');
      Object.keys(els.panels).forEach(function (key) {
        els.panels[key].hidden = key !== tabBtn.dataset.tab;
      });
    });
  });

  els.refreshState.addEventListener('click', refreshKnowledge);

  async function refreshKnowledge() {
    try {
      var obs = await api('/api/observations?hours=24&user_id=' + encodeURIComponent(state.userId || ''));
      renderObservations(obs.observations || []);
      var routines = await api('/api/routines?user_id=' + encodeURIComponent(state.userId || ''));
      renderRoutines(routines.routines || []);
    } catch (err) {
      /* The panel is a nicety; a failure here must not break the conversation. */
      console.warn('knowledge refresh failed', err);
    }
  }

  function renderObservations(rows) {
    var panel = els.panels.observations;
    panel.textContent = '';
    if (!rows.length) {
      panel.innerHTML = '<p class="placeholder">No observations in the last 24 hours.</p>';
      return;
    }
    rows.forEach(function (o) {
      var row = document.createElement('div');
      row.className = 'obs';
      var t = document.createElement('time');
      t.textContent = o.time;
      var body = document.createElement('div');
      body.className = 'obs-body';
      var subject = document.createElement('div');
      subject.className = 'obs-subject';
      subject.textContent = o.subject;
      var detail = document.createElement('div');
      detail.className = 'obs-detail';
      detail.textContent = [o.location, o.method, 'conf ' + o.confidence].filter(Boolean).join(' · ');
      body.appendChild(subject);
      body.appendChild(detail);
      row.appendChild(t);
      row.appendChild(body);
      panel.appendChild(row);
    });
  }

  function renderRoutines(rows) {
    var panel = els.panels.routines;
    panel.textContent = '';
    if (!rows.length) {
      panel.innerHTML = '<p class="placeholder">No routines learned yet. Tell it where you\'re going.</p>';
      return;
    }
    rows.forEach(function (r) {
      var wrap = document.createElement('div');
      wrap.className = 'routine';
      var name = document.createElement('div');
      name.className = 'routine-name';
      name.textContent = r.routine_name + ' ';
      var count = document.createElement('span');
      count.textContent = '· seen on ' + r.times_observed + ' trip' + (r.times_observed === 1 ? '' : 's');
      name.appendChild(count);
      wrap.appendChild(name);
      wrap.appendChild(itemRow(r.expected_items || [], 'extra'));
      panel.appendChild(wrap);
    });
  }

  boot();
})();

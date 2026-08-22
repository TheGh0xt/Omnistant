/* Omnistant — app wiring.
 *
 * The important behaviour here is the watch loop. The old build was
 * request/response: you asked, it looked once, it forgot. This one keeps the
 * camera open and ticks continuously, so putting your AirPods in a bag is
 * something the agent *witnesses* rather than something you have to report.
 *
 * The loop is deliberately not a fixed-rate poll. Every tick compares the frame
 * locally against the last one actually sent (Camera.hasChanged); a scene nobody
 * has touched never reaches the network. That keeps a continuously-watching
 * agent affordable, and it is also just correct — an unchanged frame carries no
 * new information.
 */
(function () {
  'use strict';

  var TICK_MS = 1500;          // how often we *look* — local, free
  var MIN_API_GAP_MS = 6000;   // floor between vision calls, even if things move
  var DIFF_THRESHOLD = 6;      // mean luma delta that counts as "something changed"
  var REVEAL_MS = 150;         // word-by-word reveal cadence

  var el = function (id) { return document.getElementById(id); };
  var els = {
    agentStatus: el('agentStatus'),
    globe: el('globe'), globeTooltip: el('globeTooltip'), globeEmpty: el('globeEmpty'),
    expandGlobe: el('expandGlobe'), globeDetails: el('globeDetails'), obsList: el('obsList'),
    video: el('video'), canvas: el('canvas'),
    viewportPlaceholder: el('viewportPlaceholder'), watchBadge: el('watchBadge'),
    watchToggle: el('watchToggle'), flipCamera: el('flipCamera'),
    watchState: el('watchState'), seenLine: el('seenLine'),
    workflow: el('workflow'), workflowTitle: el('workflowTitle'), workflowSub: el('workflowSub'),
    response: el('response'), answer: el('answer'), details: el('details'),
    gotIt: el('gotIt'), toggleDetails: el('toggleDetails'),
    micBtn: el('micBtn'), micHalo: el('micHalo'),
    voiceStatus: el('voiceStatus'), voiceTranscript: el('voiceTranscript'), speakBars: el('speakBars'),
    composer: el('composer'), input: el('input'), sendBtn: el('sendBtn')
  };

  var camera = new Camera(els.video, els.canvas);
  var globe = new Globe(els.globe, els.globeTooltip, { onSelect: onGlobeSelect });

  var state = {
    sessionId: null, userId: null,
    screen: 'home', busy: false,
    watching: false, lastApiAt: 0, tickTimer: null, pausedUntil: 0,
    observations: [], filter: 'all',
    revealTimer: null, detailRows: []
  };

  var RESTING_HINT = 'Tap to talk, or just type below.';

  /* ───────── api ───────── */
  async function api(path, options) {
    var res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
    var body = null;
    try { body = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) {
      var err = new Error((body && (body.detail || body.message)) || ('Request failed (' + res.status + ')'));
      err.status = res.status;
      err.retryAfter = body && body.retry_after;
      throw err;
    }
    return body;
  }

  function setAgentStatus(text) { els.agentStatus.textContent = text; }

  /* ───────── boot ───────── */
  async function boot() {
    try {
      var cfg = await api('/api/config');
      state.sessionId = cfg.session_id;
      state.userId = cfg.user_id;
    } catch (err) {
      say('I can’t reach my own service right now. ' + err.message);
    }
    if (!Speaker.isSupported()) { /* replies simply will not be spoken */ }
    setupVoice();
    await refreshObservations();
  }

  /* ───────── observations → globe ───────── */
  async function refreshObservations() {
    try {
      var data = await api('/api/observations?hours=24&user_id=' + encodeURIComponent(state.userId || ''));
      state.observations = (data.observations || []).map(function (o) {
        return {
          id: o.id, label: o.subject, place: o.location || '', time: o.time,
          type: o.type, confidence: Math.round((o.confidence || 0) * 100),
          detail: o.detail || ''
        };
      });
      globe.setObservations(state.observations);
      els.globeEmpty.hidden = state.observations.length > 0;
      renderObsList();
    } catch (err) {
      /* The globe is a view of memory, not memory itself — never break the app. */
      console.warn('observation refresh failed', err);
    }
  }

  function renderObsList() {
    els.obsList.textContent = '';
    var rows = state.observations.filter(function (o) {
      return state.filter === 'all' || o.type === state.filter;
    });
    if (!rows.length) {
      var p = document.createElement('p');
      p.className = 'empty-note';
      p.textContent = 'Nothing of that kind yet.';
      els.obsList.appendChild(p);
      return;
    }
    rows.forEach(function (o) {
      var row = document.createElement('button');
      row.className = 'obs-row';
      row.type = 'button';

      var dot = document.createElement('span');
      dot.className = 'obs-dot';
      dot.style.background = Globe.TYPE_COLOR[o.type] || '#4A90E2';

      var body = document.createElement('span');
      body.className = 'obs-body';
      var label = document.createElement('span');
      label.className = 'obs-label';
      label.textContent = o.label;
      var meta = document.createElement('span');
      meta.className = 'obs-meta';
      // Type repeats in text: colour is never the only signal.
      meta.textContent = [o.type, o.place, o.time].filter(Boolean).join(' · ');
      body.appendChild(label); body.appendChild(meta);

      var conf = document.createElement('span');
      conf.className = 'obs-conf';
      conf.textContent = o.confidence + '%';

      row.appendChild(dot); row.appendChild(body); row.appendChild(conf);
      row.addEventListener('click', function () { globe.select(o.id); });
      els.obsList.appendChild(row);
    });
  }

  function onGlobeSelect() { /* tooltip is drawn by the globe itself */ }

  els.expandGlobe.addEventListener('click', function () {
    var opening = els.globeDetails.hidden;
    els.globeDetails.hidden = !opening;
    els.workflow.hidden = opening;
    els.expandGlobe.textContent = opening ? 'Close' : 'Expand';
    els.expandGlobe.setAttribute('aria-expanded', String(opening));
  });

  document.querySelectorAll('.pill').forEach(function (pill) {
    pill.addEventListener('click', function () {
      document.querySelectorAll('.pill').forEach(function (p) { p.classList.remove('selected'); });
      pill.classList.add('selected');
      state.filter = pill.dataset.filter;
      globe.setFilter(state.filter);
      renderObsList();
    });
  });

  /* ───────── the watch loop ───────── */
  els.watchToggle.addEventListener('click', function () {
    if (state.watching) { stopWatching(); } else { startWatching(); }
  });
  els.flipCamera.addEventListener('click', function () { camera.flip(); });

  async function startWatching() {
    els.watchToggle.disabled = true;
    els.watchState.textContent = 'starting…';
    try {
      await camera.start();
    } catch (err) {
      els.watchState.textContent = 'unavailable';
      els.watchToggle.disabled = false;
      say(err.message);
      return;
    }
    state.watching = true;
    state.lastApiAt = 0;
    els.watchToggle.disabled = false;
    els.watchToggle.textContent = 'Stop watching';
    els.watchToggle.classList.add('stop');
    els.viewportPlaceholder.hidden = true;
    els.watchBadge.hidden = false;
    els.flipCamera.hidden = false;
    els.watchState.textContent = 'Watching';
    els.watchState.classList.add('live');
    els.seenLine.textContent = 'Looking…';
    setAgentStatus('Watching');
    state.tickTimer = setInterval(tick, TICK_MS);
    tick();
  }

  function stopWatching() {
    state.watching = false;
    clearInterval(state.tickTimer);
    state.tickTimer = null;
    camera.stop();
    els.watchToggle.textContent = 'Start watching';
    els.watchToggle.classList.remove('stop');
    els.viewportPlaceholder.hidden = false;
    els.watchBadge.hidden = true;
    els.flipCamera.hidden = true;
    els.watchState.textContent = 'Off';
    els.watchState.classList.remove('live');
    els.seenLine.textContent = '';
    setAgentStatus('Ready');
  }

  async function tick() {
    if (!state.watching || state.busy) { return; }
    var now = Date.now();
    if (now < state.pausedUntil) {
      var wait = Math.ceil((state.pausedUntil - now) / 1000);
      els.seenLine.textContent = 'Paused — model is rate-limited (' + wait + 's)';
      return;
    }
    if (now - state.lastApiAt < MIN_API_GAP_MS) { return; }
    if (!camera.hasChanged(DIFF_THRESHOLD)) { return; }   // nothing moved; costs nothing

    var frame = camera.capture();
    if (!frame) { return; }
    camera.commitFrame();
    state.lastApiAt = now;

    try {
      var result = await api('/api/observe', {
        method: 'POST',
        body: JSON.stringify({
          session_id: state.sessionId, user_id: state.userId,
          frame: frame, spoken: els.input.value.trim()
        })
      });
      handleTick(result);
    } catch (err) {
      if (err.status === 429) {
        state.pausedUntil = Date.now() + ((err.retryAfter || 30) * 1000);
        els.seenLine.textContent = 'Paused — model is rate-limited';
      } else {
        els.seenLine.textContent = 'Could not look just now.';
      }
    }
  }

  function handleTick(result) {
    if (!result.available) { els.seenLine.textContent = result.note || 'Cannot see right now.'; return; }

    var names = (result.seen || []).map(function (i) { return i.name; });
    els.seenLine.textContent = names.length
      ? 'I can see: ' + names.join(', ')
      : 'Nothing I recognise yet.';

    // Only speak up when something actually changed — narrating a static scene
    // every few seconds is precisely the nagging this is meant to replace.
    if (result.narration) {
      say(result.narration, { transient: true });
      if (Speaker.isSupported()) { Speaker.speak(result.narration); }
    }
    if (result.logged) { refreshObservations(); }
  }

  /* ───────── conversation ───────── */
  els.composer.addEventListener('submit', function (e) {
    e.preventDefault();
    Speaker.unlock();
    send(els.input.value);
  });

  document.querySelectorAll('.quick-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      Speaker.unlock();
      document.querySelectorAll('.quick-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var phrases = {
        leave: "I'm heading out",
        recall: 'Where are my keys?',
        timeline: 'What did I do today?'
      };
      var titles = {
        leave: ["I'm heading out", 'Point the camera at your things and I’ll check what’s missing.'],
        recall: ['Where is it?', 'I’ll look through what I’ve noticed.'],
        timeline: ['What did I do today?', 'I’ll piece it together from what I saw.']
      };
      els.workflowTitle.textContent = titles[btn.dataset.action][0];
      els.workflowSub.textContent = titles[btn.dataset.action][1];
      send(phrases[btn.dataset.action]);
    });
  });

  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) { return; }
    state.busy = true;
    els.sendBtn.disabled = true;
    els.input.value = '';
    setAgentStatus('Thinking');
    setVoice('busy', 'Thinking…', '“' + text + '”');

    var payload = { text: text, session_id: state.sessionId, user_id: state.userId };
    // If the camera is already open, the current view is part of the question.
    if (camera.isRunning()) { payload.frame = camera.capture(); }

    try {
      var reply = await api('/api/chat', { method: 'POST', body: JSON.stringify(payload) });
      state.sessionId = reply.session_id || state.sessionId;
      say(reply.reply || 'I don’t have an answer for that.', { rows: detailRowsFor(reply) });
      if (Speaker.isSupported()) { Speaker.speak(reply.reply); }
      highlightAnswer(reply);
      await refreshObservations();
    } catch (err) {
      say(err.status === 429
        ? 'I’m rate-limited right now. Try again in about ' + Math.round(err.retryAfter || 30) + ' seconds.'
        : err.message);
    } finally {
      state.busy = false;
      els.sendBtn.disabled = false;
      setAgentStatus(state.watching ? 'Watching' : 'Ready');
      setVoice('idle', 'Voice', RESTING_HINT);
    }
  }

  function detailRowsFor(reply) {
    var result = (reply.workflow_results || [])[0];
    if (!result) { return []; }
    if (result.workflow === 'item_recall' && result.sightings && result.sightings.length) {
      var s = result.sightings[0];
      return [
        { k: 'Last seen', v: [s.location, s.detail].filter(Boolean).join(', ') || '—' },
        { k: 'Time', v: s.time },
        { k: 'How', v: s.method },
        { k: 'Confidence', v: Math.round((result.confidence || 0) * 100) + '% (' + result.confidence_label + ')' }
      ];
    }
    if (result.workflow === 'leave_detection') {
      return [
        { k: 'Routine', v: result.routine },
        { k: 'Missing', v: (result.missing_items || []).map(function (m) { return m.item; }).join(', ') || 'nothing' },
        { k: 'Found', v: (result.found_items || []).map(function (f) { return f.name; }).join(', ') || 'nothing' }
      ];
    }
    if (result.workflow === 'daily_timeline') {
      return [{ k: 'Moments', v: String((result.entries || []).length) }, { k: 'Day', v: result.day }];
    }
    return [];
  }

  function highlightAnswer(reply) {
    var result = (reply.workflow_results || [])[0];
    if (!result) { return; }
    var wanted = result.workflow === 'item_recall' ? String(result.item || '').toLowerCase() : null;
    if (!wanted) { globe.highlight(null); return; }
    var match = state.observations.find(function (o) { return o.label.toLowerCase() === wanted; });
    globe.highlight(match ? match.id : null);
  }

  /* ───────── the answer card ───────── */
  function say(text, opts) {
    opts = opts || {};
    clearInterval(state.revealTimer);
    els.response.hidden = false;
    els.answer.textContent = '';
    state.detailRows = opts.rows || [];
    els.toggleDetails.hidden = state.detailRows.length === 0;
    els.details.hidden = true;
    els.toggleDetails.textContent = 'Details';

    // Word by word: the whole sentence appearing at once is a wall to re-read.
    var words = String(text).split(/\s+/);
    var shown = 0;
    var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) { els.answer.textContent = text; return; }

    els.speakBars.hidden = false;
    state.revealTimer = setInterval(function () {
      shown += 1;
      els.answer.textContent = words.slice(0, shown).join(' ');
      if (shown >= words.length) {
        clearInterval(state.revealTimer);
        els.speakBars.hidden = true;
      }
    }, REVEAL_MS);
  }

  els.gotIt.addEventListener('click', function () {
    clearInterval(state.revealTimer);
    els.response.hidden = true;
    els.speakBars.hidden = true;
    Speaker.stop();
    globe.highlight(null);
    document.querySelectorAll('.quick-btn').forEach(function (b) { b.classList.remove('active'); });
    els.workflowTitle.textContent = 'Ready to listen';
    els.workflowSub.textContent = 'Tap one thing below. I’ll handle the rest.';
  });

  els.toggleDetails.addEventListener('click', function () {
    var opening = els.details.hidden;
    els.details.textContent = '';
    state.detailRows.forEach(function (row) {
      var line = document.createElement('div');
      line.className = 'details-row';
      var k = document.createElement('span'); k.className = 'k'; k.textContent = row.k;
      var v = document.createElement('span'); v.className = 'v'; v.textContent = row.v;
      line.appendChild(k); line.appendChild(v);
      els.details.appendChild(line);
    });
    els.details.hidden = !opening;
    els.toggleDetails.textContent = opening ? 'Hide details' : 'Details';
  });

  /* ───────── voice ───────── */
  function setVoice(mode, status, transcript) {
    els.voiceStatus.textContent = status;
    els.voiceStatus.className = 'voice-status' + (mode === 'listening' ? ' listening' : mode === 'busy' ? ' busy' : '');
    els.voiceTranscript.textContent = transcript;
    els.micBtn.classList.toggle('listening', mode === 'listening');
    els.micBtn.classList.toggle('busy', mode === 'busy');
    els.micHalo.classList.toggle('on', mode === 'listening');
    els.micHalo.classList.toggle('listening', mode === 'listening');
    els.micBtn.setAttribute('aria-pressed', String(mode === 'listening'));
    els.micBtn.setAttribute('aria-label', mode === 'listening' ? 'Stop listening' : 'Talk to Omnistant');
  }

  function setupVoice() {
    var listener = new Listener({
      onPartial: function (t) { setVoice('listening', 'Listening — tap to stop', t); },
      onResult: function (t) { send(t); },
      onStateChange: function (listening) {
        if (listening) { setVoice('listening', 'Listening — tap to stop', 'Go ahead…'); setAgentStatus('Listening'); }
        else if (!state.busy) { setVoice('idle', 'Voice', RESTING_HINT); setAgentStatus(state.watching ? 'Watching' : 'Ready'); }
      },
      onError: function (message) { setVoice('idle', 'Voice', RESTING_HINT); say(message); }
    });
    if (!listener.isSupported()) {
      els.micBtn.disabled = true;
      els.micBtn.title = 'Voice input is not supported in this browser';
      els.voiceTranscript.textContent = 'Voice isn’t supported here — type below.';
      return;
    }
    els.micBtn.addEventListener('click', function () { listener.toggle(); });
  }

  boot();
})();

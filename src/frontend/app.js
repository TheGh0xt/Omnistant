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
  var FRAME_WARM_MS = 60000;   // refresh the server's cached frame at most this often

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
    composer: el('composer'), input: el('input'), sendBtn: el('sendBtn'),
    timelineCard: el('timelineCard'), tlTrack: el('tlTrack'), tlDetail: el('tlDetail'), tlCount: el('tlCount'),
    wakePill: el('wakePill'), wakeLabel: el('wakeLabel')
  };

  var camera = new Camera(els.video, els.canvas);
  var globe = new Globe(els.globe, els.globeTooltip, { onSelect: onGlobeSelect });

  var state = {
    sessionId: null, userId: null,
    screen: 'home', busy: false,
    watching: false, lastApiAt: 0, lastFrameWarmAt: 0, tickTimer: null, pausedUntil: 0,
    observations: [], filter: 'all',
    revealTimer: null, detailRows: [],
    timeline: [], timelineTotal: 0, tlSelected: 0, wake: false, heardTimer: null
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

  /* Keep the server's cached frame fresh while the scene sits still.
   *
   * The watch loop only spends a vision call when something moves, which is the
   * whole reason it is affordable — but it means a motionless desk stops
   * refreshing the cached frame, and the cache expires after FRAME_TTL_SECONDS.
   * A leave scan arriving after that has nothing to look at. `/api/frame` only
   * stores the image; no model is involved, so this stays free. */
  async function keepFrameWarm(now) {
    if (now - state.lastFrameWarmAt < FRAME_WARM_MS) { return; }
    var frame = camera.capture();
    if (!frame) { return; }
    state.lastFrameWarmAt = now;
    try {
      await api('/api/frame', {
        method: 'POST',
        body: JSON.stringify({ session_id: state.sessionId, image: frame })
      });
    } catch (err) {
      /* The cache is an optimisation. Never let it break the watch loop. */
    }
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
    if (!camera.hasChanged(DIFF_THRESHOLD)) {   // nothing moved; costs no vision call
      keepFrameWarm(now);
      return;
    }

    var frame = camera.capture();
    if (!frame) { return; }
    camera.commitFrame();
    state.lastApiAt = now;
    state.lastFrameWarmAt = now;   // an observe caches the frame too

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


  /* ───────── daily timeline ─────────
   *
   * A horizontal bar rather than a list: the point of the redesign was that a
   * day should be glanceable. The handoff spaces five events at 6% + i·22%;
   * real days have any number, so spacing is derived and lands exactly on those
   * positions when there are five.
   */
  // The bar is 375px wide and the labels are ~52px, so more than six timestamps
  // collide into an unreadable smear. That is not only a layout limit — a day
  // worth glancing at is a handful of moments, not every sighting. Prefer the
  // things that give a day its shape (where you were, what you did) and spread
  // the rest evenly across the clock.
  var MAX_MOMENTS = 6;

  function condense(entries) {
    if (entries.length <= MAX_MOMENTS) { return entries; }
    var shape = entries.filter(function (e) { return e.kind !== 'item'; });
    var pool = shape.length >= 2 ? shape : entries;
    if (pool.length <= MAX_MOMENTS) { return pool; }
    // Keep the first and last, sample the middle at even intervals.
    var picked = [];
    var step = (pool.length - 1) / (MAX_MOMENTS - 1);
    for (var i = 0; i < MAX_MOMENTS; i++) { picked.push(pool[Math.round(i * step)]); }
    return picked;
  }

  async function showTimeline() {
    try {
      var data = await api('/api/timeline?user_id=' + encodeURIComponent(state.userId || ''));
      state.timelineTotal = (data.entries || []).length;
      state.timeline = condense(data.entries || []).map(function (e, i) {
        return { id: 'tl' + i, time: e.time, title: e.subject, type: e.kind,
                 meta: [e.location, e.detail].filter(Boolean).join(' · ') };
      });
    } catch (err) {
      state.timeline = [];
      state.timelineTotal = 0;
    }
    state.tlSelected = Math.max(0, state.timeline.length - 1);   // most recent first
    renderTimeline();
    els.timelineCard.hidden = false;
  }

  function renderTimeline() {
    var track = els.tlTrack;
    // Keep the rail; replace the events.
    Array.prototype.slice.call(track.querySelectorAll('.tl-event')).forEach(function (n) { n.remove(); });

    var n = state.timeline.length;
    var total = state.timelineTotal || n;
    els.tlCount.textContent = n
      ? (total > n ? n + ' of ' + total + ' moments' : n + (n === 1 ? ' moment' : ' moments'))
      : '';
    if (!n) {
      els.tlDetail.textContent = '';
      var empty = document.createElement('p');
      empty.className = 'tl-meta';
      empty.textContent = 'Nothing logged yet today. Turn the camera on and I’ll start filling this in.';
      els.tlDetail.appendChild(empty);
      return;
    }

    var span = 88;                       // 6%..94%, matching the handoff
    state.timeline.forEach(function (event, i) {
      var pct = n === 1 ? 50 : 6 + (i * (span / (n - 1)));
      var btn = document.createElement('button');
      btn.className = 'tl-event' + (i === state.tlSelected ? ' selected' : '');
      btn.type = 'button';
      btn.style.left = pct + '%';
      btn.setAttribute('aria-label', event.time + ' ' + event.title);

      var dot = document.createElement('span');
      dot.className = 'tl-dot';
      dot.style.background = Globe.TYPE_COLOR[event.type] || '#4A90E2';
      var time = document.createElement('span');
      time.className = 'tl-time';
      time.textContent = event.time;
      btn.appendChild(dot); btn.appendChild(time);
      btn.addEventListener('click', function () { state.tlSelected = i; renderTimeline(); });
      track.appendChild(btn);
    });

    var selected = state.timeline[state.tlSelected];
    els.tlDetail.textContent = '';
    var title = document.createElement('div');
    title.className = 'tl-title';
    title.textContent = selected.title;
    var meta = document.createElement('div');
    meta.className = 'tl-meta';
    meta.textContent = [selected.type, selected.meta, selected.time].filter(Boolean).join(' · ');
    els.tlDetail.appendChild(title); els.tlDetail.appendChild(meta);
  }

  /* One-shot: any first interaction unlocks speech synthesis. A missed unlock
     is silent, unrecoverable for the session, and indistinguishable from an
     agent that decided not to answer — so catch it on whatever the user
     happens to touch first. */
  ['pointerdown', 'touchend', 'keydown'].forEach(function (evt) {
    document.addEventListener(evt, function once() {
      Speaker.unlock();
      ['pointerdown', 'touchend', 'keydown'].forEach(function (e2) {
        document.removeEventListener(e2, once);
      });
    }, { once: false, passive: true });
  });

  /* ───────── wake word ───────── */
  var wake = new WakeWord({
    // Show what the recogniser actually heard while in standby. Without this,
    // a wake word that isn't firing is indistinguishable from a microphone that
    // isn't working, and there is no way to tell whether you said it wrong.
    onHeard: function (text) {
      if (state.busy || !state.wake) { return; }
      // Labelled, because an unlabelled transcript here reads as dictation and
      // is not: the standby stream is listening for a trigger and nothing said
      // to it is ever sent. Watching your own words appear and go nowhere is
      // how "the wake word is broken" gets reported when it is working.
      els.voiceTranscript.textContent = 'heard while waiting: “' + text + '”';
      clearTimeout(state.heardTimer);
      state.heardTimer = setTimeout(function () {
        if (state.wake && !state.busy) {
          els.voiceTranscript.textContent = 'Say “Hey Omni” to talk to me.';
        }
      }, 2500);
    },
    onWake: function () {
      // The trigger stream is tuned for two words; hand the floor to the real
      // recogniser for the actual command.
      setAgentStatus('Listening');
      if (state.listener) { state.listener.start(); }
    },
    onStateChange: function (standby) {
      els.micHalo.classList.toggle('standby', standby && !state.busy);
      if (standby && !state.busy) { setAgentStatus('Standing by'); }
    },
    onError: function (message) {
      setWake(false);
      say(message);
    }
  });

  function setWake(on) {
    state.wake = on;
    els.wakePill.setAttribute('aria-pressed', String(on));
    els.wakeLabel.textContent = on
      ? 'Wake word on — always listening'
      : 'Wake word off — tap to talk';
    if (on) {
      wake.enable();
      els.voiceTranscript.textContent = 'Say “Hey Omni”, or tap to talk.';
    } else {
      wake.disable();
      els.micHalo.classList.remove('standby');
      els.voiceTranscript.textContent = RESTING_HINT;
    }
    setAgentStatus(on ? 'Standing by' : (state.watching ? 'Watching' : 'Ready'));
  }

  els.wakePill.addEventListener('click', function () {
    // Turning the wake word on means everything after it is hands-free, so this
    // is the last user gesture we are guaranteed to get. Unlock here or the
    // agent answers every wake-word command in silence.
    Speaker.unlock();
    if (!wake.isSupported()) { say('This browser can’t listen for a wake word. Tap the mic instead.'); return; }
    if (!state.wake) {
      // Said plainly, once, before it is switched on: this is not local.
      say('Wake word on. I’ll listen for “Hey Omni”. This streams microphone audio to your browser’s speech service while it’s on — turn it off any time.');
    }
    setWake(!state.wake);
  });

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
      if (btn.dataset.action === 'timeline') { showTimeline(); } else { els.timelineCard.hidden = true; }
      send(phrases[btn.dataset.action]);
    });
  });

  async function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) { return; }
    state.busy = true;
    wake.suspend();          // don't let it hear its own reply and wake itself
    els.sendBtn.disabled = true;
    els.input.value = '';
    setAgentStatus('Thinking');
    setVoice('busy', 'Thinking…', '“' + text + '”');

    var payload = { text: text, session_id: state.sessionId, user_id: state.userId };
    // If the camera is open, the current view is part of the question.
    //
    // Waited for rather than sampled: `isRunning()` is false for the second or
    // two between the camera starting and the video becoming decodable, and
    // "I'm heading out" lands inside that gap constantly — you tap start and
    // speak. A scan with no frame cannot see, so it queues no reminder, and the
    // notification simply never arrives.
    if (camera.hasStream()) { payload.frame = await camera.captureWhenReady(2500); }

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
      setAgentStatus(state.wake ? 'Standing by' : (state.watching ? 'Watching' : 'Ready'));
      setVoice('idle', 'Voice', RESTING_HINT);
      // Give speech synthesis time to finish before opening the mic again.
      setTimeout(function () { wake.resume(); }, 1200);
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
      var rows = [
        { k: 'Routine', v: result.routine },
        { k: 'Missing', v: (result.missing_items || []).map(function (m) { return m.item; }).join(', ') || 'nothing' },
        { k: 'Found', v: (result.found_items || []).map(function (f) { return f.name; }).join(', ') || 'nothing' }
      ];
      // Carried items are in neither list. Without a row of their own they
      // simply vanish, which reads as the agent having forgotten them.
      var carried = (result.carried_items || []).map(function (c) { return c.item; });
      if (carried.length) { rows.push({ k: 'On you', v: carried.join(', ') }); }
      return rows;
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
    els.timelineCard.hidden = true;
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
    state.listener = listener;
    els.micBtn.addEventListener('click', function () {
      // Unlock BEFORE starting the mic. iOS will not speak until synthesis has
      // been touched inside a user gesture, and for someone who only ever talks
      // to it — never typing, never tapping a suggestion — this tap is the only
      // gesture there is. Without it every reply came back silently, which is
      // the whole multimodal promise quietly not happening.
      //
      // Safe in this order despite the synthesis/recognition conflict: unlock
      // speaks a zero-volume utterance and cancels it, and Listener.start calls
      // Speaker.stop() before touching the recogniser.
      Speaker.unlock();
      wake.suspend();
      listener.toggle();
    });
  }

  boot();
})();

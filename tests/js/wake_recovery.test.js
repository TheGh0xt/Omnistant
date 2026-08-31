/* Regression tests for the wake word's suspend/resume wiring.
 *
 * These are app.js tests, which is new: speech.js and camera.js were already
 * covered, but the *wiring between them* was not, and that is precisely where
 * the "Hey Omni works once and then never again" bug lived. WakeWord and
 * Listener were each correct in isolation; app.js suspended the wake word on
 * every path and resumed it on exactly one.
 *
 * app.js is an IIFE over browser globals, so the doubles below are a small
 * fake DOM — enough for it to boot, not a browser. Everything the tests assert
 * is observed through the WakeWord/Listener doubles it is handed.
 */
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'src', 'frontend', 'app.js'),
  'utf8'
);

/* ---------- a DOM that is just deep enough ---------- */

function makeEl(id) {
  const listeners = {};
  return {
    id,
    hidden: false,
    disabled: false,
    textContent: '',
    title: '',
    value: '',
    className: '',
    dataset: {},
    style: {},
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    setAttribute() {},
    getAttribute: () => null,
    removeAttribute() {},
    appendChild() {},
    addEventListener(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
    removeEventListener() {},
    /* Test-only: fire what a user would have clicked. */
    __fire(evt, arg) { (listeners[evt] || []).forEach((fn) => fn(arg || { preventDefault() {} })); }
  };
}

function loadApp() {
  const els = {};
  const timers = [];
  const calls = { wake: [], listenerStart: 0 };

  const doc = {
    getElementById(id) { return (els[id] = els[id] || makeEl(id)); },
    createElement: () => makeEl('created'),
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {}
  };

  /* The two objects under observation. */
  class FakeWakeWord {
    constructor(opts) { FakeWakeWord.opts = opts; }
    isSupported() { return true; }
    enable() { calls.wake.push('enable'); }
    disable() { calls.wake.push('disable'); }
    suspend() { calls.wake.push('suspend'); }
    resume() { calls.wake.push('resume'); }
  }

  class FakeListener {
    constructor(opts) { FakeListener.opts = opts; }
    isSupported() { return true; }
    /* Async, exactly like the real one: a refusal is a resolved `false`. */
    async start() { calls.listenerStart += 1; return FakeListener.startResult; }
    stop() {}
    toggle() {}
  }
  FakeListener.startResult = true;

  const win = {
    matchMedia: () => ({ matches: true, addEventListener() {}, addListener() {} }),
    isSecureContext: true,
    navigator: { language: 'en-GB' }
  };

  const Speaker = {
    isSupported: () => true,
    unlock() {},
    stop() {},
    isSpeaking: () => false,
    /* Mirrors the real signature: the done callback always fires. */
    speak(text, onDone) { if (typeof onDone === 'function') { onDone(); } }
  };

  class FakeCamera {
    hasStream() { return false; }
    hasChanged() { return false; }
    capture() { return null; }
    commitFrame() {}
    isRunning() { return false; }
    async captureWhenReady() { return null; }
    async flip() {}
  }
  class FakeGlobe {
    setObservations() {} setFilter() {} select() {} highlight() {}
  }
  FakeGlobe.TYPE_COLOR = {};

  const fetchStub = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ session_id: 's-1', user_id: 'u-1', observations: [], entries: [] })
  });

  const args = {
    window: win,
    document: doc,
    Camera: FakeCamera,
    Globe: FakeGlobe,
    WakeWord: FakeWakeWord,
    Listener: FakeListener,
    Speaker,
    console: { warn() {}, info() {}, error() {} },
    fetch: fetchStub,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) { timers[id - 1].fn = () => {}; } },
    setInterval: () => 0,
    clearInterval: () => {},
    navigator: win.navigator,
    Promise
  };

  new Function(...Object.keys(args), SRC)(...Object.values(args));

  return {
    els,
    calls,
    /* Getters, not snapshots: `setupVoice()` runs inside the async `boot()`,
       so the Listener does not exist until the config fetch has settled. */
    get wakeOpts() { return FakeWakeWord.opts; },
    get listenerOpts() { return FakeListener.opts; },
    setStartResult(v) { FakeListener.startResult = v; },
    runTimers() { timers.splice(0).forEach((t) => t.fn()); },
    /* Let boot()'s awaits and the async `start()` settle. */
    async settle() {
      for (let i = 0; i < 8; i++) { await new Promise((r) => setImmediate(r)); }
    }
  };
}

/* Turning the pill on is the only way into the state the bug needs. */
function enableWake(app) {
  app.els.wakePill.__fire('click');
}

/* Boot is async; nothing below exists until it has run. */
async function boot(app) {
  await app.settle();
  assert.ok(app.listenerOpts, 'setupVoice should have constructed the listener');
}

test('a wake followed by silence returns to standby', async () => {
  const app = loadApp();
  await boot(app);
  enableWake(app);
  assert.ok(app.calls.wake.includes('enable'), 'the pill should have enabled the wake word');

  // The wake phrase lands: WakeWord suspends itself, app.js hands over to Listener.
  app.wakeOpts.onWake();
  await app.settle();
  assert.strictEqual(app.calls.listenerStart, 1, 'onWake should start the command listener');

  // Now the user says nothing. The listener stops with no transcript, so
  // `send()` never runs — this is the path that used to strand the recogniser.
  app.listenerOpts.onStateChange(false);
  app.runTimers();

  assert.ok(
    app.calls.wake.includes('resume'),
    'standby must come back after a triggered listen that produced nothing'
  );
});

test('a wake followed by a mic error returns to standby', async () => {
  const app = loadApp();
  await boot(app);
  enableWake(app);

  app.wakeOpts.onWake();
  await app.settle();
  app.listenerOpts.onError('I did not hear anything.');
  app.runTimers();

  assert.ok(app.calls.wake.includes('resume'), 'an error must not leave the mic shut for good');
});

test('a listener that refuses to start does not strand the wake word', async () => {
  const app = loadApp();
  await boot(app);
  enableWake(app);

  // `start()` resolves false when a push-to-talk turn is already in flight.
  // Nothing else will report this: no state change, no error.
  app.setStartResult(false);
  app.wakeOpts.onWake();
  await app.settle();
  app.runTimers();

  assert.ok(app.calls.wake.includes('resume'), 'a refused start must hand standby back');
});

test('standby is not reopened while the wake word is switched off', async () => {
  const app = loadApp();
  await boot(app);
  // Never enabled: state.wake is false.
  app.listenerOpts.onStateChange(false);
  app.runTimers();

  assert.ok(
    !app.calls.wake.includes('resume'),
    'a resume here would reopen a microphone the user deliberately closed'
  );
});

/* Regression tests for the wake word.
 *
 * There is no build step and no framework here on purpose: `node --test` ships
 * with Node, and adding a package.json to a Python repo to test 470 lines of
 * vanilla JS is a worse trade than this loader. speech.js is an IIFE that takes
 * `window` as its only argument, so a plain object is a complete test double
 * for the browser it needs.
 */
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'src', 'frontend', 'speech.js'),
  'utf8'
);

/* A SpeechRecognition the test drives by hand. The real one is an event source
 * we cannot provoke; every failure mode below is one of its documented events. */
class FakeRecognition {
  constructor() {
    FakeRecognition.instances.push(this);
    this.started = false;
  }
  start() {
    if (FakeRecognition.startThrows) { throw new Error('already started'); }
    this.started = true;
    if (this.onstart) { this.onstart(); }
  }
  abort() { this.started = false; }
  stop() { this.started = false; }

  /* Helpers named for what the browser would be doing. */
  emitError(error) { if (this.onerror) { this.onerror({ error }); } }
  emitResult(transcript) {
    if (!this.onresult) { return; }
    this.onresult({ resultIndex: 0, results: [[{ transcript }]] });
  }
  emitEnd() { this.started = false; if (this.onend) { this.onend(); } }
}
FakeRecognition.instances = [];
FakeRecognition.startThrows = false;

function loadWakeWord() {
  FakeRecognition.instances = [];
  FakeRecognition.startThrows = false;
  const timers = [];
  const win = {
    SpeechRecognition: FakeRecognition,
    navigator: { language: 'en-GB' },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
  };
  new Function('window', SRC)(win);
  // Restart timers fire manually so a test controls the whole lifecycle.
  win.__runTimers = () => { const due = timers.splice(0); due.forEach((fn) => fn()); };
  return win;
}

/* Date.now drives the "was this session healthy?" check. */
function withClock(fn) {
  const real = Date.now;
  let t = 1_000_000;
  Date.now = () => t;
  try {
    return fn({ advance: (ms) => { t += ms; } });
  } finally {
    Date.now = real;
  }
}

function newWakeWord(win, opts) {
  const errors = [];
  const ww = new win.WakeWord(Object.assign({ onError: (m) => errors.push(m) }, opts || {}));
  return { ww, errors };
}

function latest() { return FakeRecognition.instances[FakeRecognition.instances.length - 1]; }

test('a transient error inside a healthy session does not accumulate', () => {
  withClock((clock) => {
    const win = loadWakeWord();
    const { ww, errors } = newWakeWord(win);
    ww.enable();

    // Six sessions, each one running fine for a minute and each hit by a single
    // `network` blip — far more than MAX_FAILURES, spread over an hour.
    for (let i = 0; i < 6; i++) {
      const rec = latest();
      clock.advance(60_000);
      rec.emitError('network');
      rec.emitResult('the weather today is fine');  // engine still transcribing
      rec.emitEnd();
      win.__runTimers();
    }

    assert.deepStrictEqual(errors, [], 'wake word disabled itself despite working');
    assert.strictEqual(ww.enabled, true);
  });
});

test('a clean healthy session clears an existing failure streak', () => {
  withClock((clock) => {
    const win = loadWakeWord();
    const { ww, errors } = newWakeWord(win);
    ww.enable();

    // Four back-to-back instant failures — one short of giving up.
    for (let i = 0; i < 4; i++) {
      const rec = latest();
      rec.emitError('network');
      rec.emitEnd();
      win.__runTimers();
    }
    assert.strictEqual(ww.failures, 4);

    // Then one session that simply worked.
    const good = latest();
    clock.advance(30_000);
    good.emitEnd();
    assert.strictEqual(ww.failures, 0, 'a healthy session must reset the streak');
    win.__runTimers();

    assert.deepStrictEqual(errors, []);
    assert.strictEqual(ww.enabled, true);
  });
});

test('genuinely consecutive failures still give up', () => {
  withClock(() => {
    const win = loadWakeWord();
    const { ww, errors } = newWakeWord(win);
    ww.enable();

    // No time advances: every session dies immediately, which is a real fault.
    for (let i = 0; i < 5; i++) {
      const rec = latest();
      rec.emitError('network');
      rec.emitEnd();
      win.__runTimers();
    }

    assert.strictEqual(errors.length, 1, 'expected exactly one give-up message');
    assert.match(errors[0], /kept failing/);
    assert.strictEqual(ww.enabled, false);
  });
});

test('a blocked microphone gives up immediately, without a streak', () => {
  withClock(() => {
    const win = loadWakeWord();
    const { ww, errors } = newWakeWord(win);
    ww.enable();
    latest().emitError('not-allowed');

    assert.strictEqual(ww.enabled, false);
    assert.match(errors[0], /Microphone access is blocked/);
  });
});

test('no-speech is the engine working, not failing', () => {
  withClock((clock) => {
    const win = loadWakeWord();
    const { ww } = newWakeWord(win);
    ww.enable();

    const rec = latest();
    clock.advance(2_000);          // too short to count as healthy
    rec.emitError('no-speech');
    rec.emitEnd();

    assert.strictEqual(ww.failures, 0, 'silence must never count as a failure');
  });
});

test('"Hey, Omni!" matches — punctuation must not leave a double space', () => {
  const win = loadWakeWord();
  const { ww } = newWakeWord(win);
  const heard = [];
  const wakes = [];
  const ww2 = new win.WakeWord({ onWake: () => wakes.push(1), onHeard: (t) => heard.push(t) });

  assert.ok(ww2._matches('Hey, Omni!'), 'the comma form is the most likely phrasing there is');
  assert.ok(ww2._matches('hey omni'));
  assert.ok(ww2._matches('Hey Omny'), 'recognition never spells an invented word twice the same way');
  assert.ok(ww2._matches('omnistant'));
  assert.ok(!ww2._matches('the omelette is ready'));
  assert.ok(!ww2.constructor.DEFAULT_PATTERN.test('hey there'));
  assert.strictEqual(ww.enabled, false);
});

test('lifecycle tracing reports the events needed to diagnose a failure', () => {
  withClock((clock) => {
    const win = loadWakeWord();
    const seen = [];
    const { ww } = newWakeWord(win, { onLog: (event, detail) => seen.push([event, detail]) });
    ww.enable();

    const rec = latest();
    clock.advance(12_000);
    rec.emitError('network');
    rec.emitEnd();

    const events = seen.map(([e]) => e);
    assert.ok(events.includes('start'));
    assert.ok(events.includes('error'));
    assert.ok(events.includes('end'));
    const end = seen.find(([e]) => e === 'end')[1];
    assert.strictEqual(end.ranForMs, 12_000);
    assert.strictEqual(end.errored, true);
  });
});

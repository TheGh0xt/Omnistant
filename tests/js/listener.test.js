/* Regression tests for dictation and speech output.
 *
 * Both bugs here were reported the same way — "the voice doesn't work" — and
 * had nothing to do with each other. One was Safari never marking a result
 * final; the other was iOS refusing to speak without a prior user gesture.
 * Neither raised an error anywhere.
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

class FakeRecognition {
  constructor() { FakeRecognition.instances.push(this); }
  start() { if (this.onstart) { this.onstart(); } }
  abort() {}
  stop() {}
  /* A SpeechRecognitionResult is array-like with an isFinal flag on it. */
  _emit(text, isFinal) {
    if (!this.onresult) { return; }
    const result = [{ transcript: text }];
    result.isFinal = isFinal;
    this.onresult({ resultIndex: 0, results: [result] });
  }
  /* Safari's actual behaviour: interim results, then end, never isFinal. */
  emitInterim(text) { this._emit(text, false); }
  emitFinal(text) { this._emit(text, true); }
  emitEnd() { if (this.onend) { this.onend(); } }
}
FakeRecognition.instances = [];

class FakeUtterance {
  constructor(text) { this.text = text; FakeUtterance.spoken.push(text); }
}
FakeUtterance.spoken = [];

function load() {
  FakeRecognition.instances = [];
  FakeUtterance.spoken = [];
  const synth = {
    speaking: false, pending: false,
    cancels: 0, queue: [],
    speak(u) { this.queue.push(u.text); },
    cancel() { this.cancels += 1; },
  };
  // Timers are recorded rather than run, so a test can fire one deliberately.
  // Nothing fires on its own, which is what the rest of these tests assume.
  const timers = [];
  const win = {
    SpeechRecognition: FakeRecognition,
    SpeechSynthesisUtterance: FakeUtterance,
    speechSynthesis: synth,
    navigator: { language: 'en-GB' },
    setTimeout: (fn, ms) => timers.push({ fn, ms, cancelled: false }),
    clearTimeout: (id) => { if (timers[id - 1]) { timers[id - 1].cancelled = true; } },
    console: { warn() {} },
  };
  // `'speechSynthesis' in global` must be true for Speaker.isSupported().
  new Function('window', SRC)(win);
  win.__synth = synth;
  win.__timers = timers;
  return win;
}

/* Run the pending timer scheduled for `ms`, the way the browser eventually would. */
function fireTimer(win, ms) {
  const timer = win.__timers.find((t) => t.ms === ms && !t.cancelled);
  assert.ok(timer, `expected a timer scheduled for ${ms}ms`);
  timer.cancelled = true;
  timer.fn();
}

function latest() { return FakeRecognition.instances[FakeRecognition.instances.length - 1]; }

test('an interim transcript with no final is still sent', async () => {
  const win = load();
  const sent = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t) });
  await l.start();

  const rec = latest();
  rec.emitInterim('I am stepping out to work');
  rec.emitEnd();   // Safari: ends without ever setting isFinal

  assert.deepStrictEqual(sent, ['I am stepping out to work'],
    'the user watched their words appear and nothing was sent');
});

test('a real final result is sent exactly once', async () => {
  const win = load();
  const sent = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t) });
  await l.start();

  const rec = latest();
  rec.emitInterim('I am stepping');
  rec.emitFinal('I am stepping out');
  rec.emitEnd();   // must not re-send the stale interim

  assert.deepStrictEqual(sent, ['I am stepping out']);
});

test('silence sends nothing', async () => {
  const win = load();
  const sent = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t), onError: () => {} });
  await l.start();
  latest().emitEnd();
  assert.deepStrictEqual(sent, []);
});

test('a fresh turn does not resend the previous utterance', async () => {
  const win = load();
  const sent = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t), onError: () => {} });

  await l.start();
  latest().emitInterim('first thing');
  latest().emitEnd();

  await l.start();
  latest().emitEnd();          // said nothing this time

  assert.deepStrictEqual(sent, ['first thing']);
});

test('speech output speaks the reply', () => {
  const win = load();
  win.Speaker.speak('I last saw your keys yesterday, 11:09 AM');
  assert.deepStrictEqual(win.__synth.queue, ['I last saw your keys yesterday, 11:09 AM']);
});

test('speak does not cancel when nothing is speaking', () => {
  // cancel() immediately before speak() in the same tick can swallow the
  // utterance on iOS, which is silence with no error anywhere.
  const win = load();
  win.Speaker.speak('hello');
  assert.strictEqual(win.__synth.cancels, 0);
});

test('speak does cancel when something is already speaking', () => {
  const win = load();
  win.__synth.speaking = true;
  win.Speaker.speak('interrupting');
  assert.strictEqual(win.__synth.cancels, 1);
});

test('unlock is idempotent and reports itself', () => {
  const win = load();
  assert.strictEqual(win.Speaker.isUnlocked(), false, 'must start locked');
  win.Speaker.unlock();
  assert.strictEqual(win.Speaker.isUnlocked(), true);
  const after = win.__synth.queue.length;
  win.Speaker.unlock();
  assert.strictEqual(win.__synth.queue.length, after, 'unlock must not re-speak');
});

test('a transcript captured when the deadline stops the mic is still sent', async () => {
  // The 20s deadline exists precisely because "some browsers never fire onend".
  // Its own cleanup then called abandon(), which aborts the recogniser and
  // throws the captured transcript away -- so on exactly the browsers the
  // deadline was written for, the user watched their words appear and nothing
  // was ever sent. abort() firing onend is not something we get to rely on.
  const win = load();
  const sent = [];
  const errors = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t), onError: (m) => errors.push(m) });
  await l.start();

  latest().emitInterim('I am heading out to work');
  fireTimer(win, 20000);       // the hard deadline, with no final ever arriving

  assert.deepStrictEqual(sent, ['I am heading out to work']);
  assert.deepStrictEqual(errors, [], 'it heard something, so it must not claim otherwise');
});

test('stopping a recogniser that never started still sends what was heard', async () => {
  // stop() falls through to abandon() whenever the engine is not in its
  // listening state -- same silent drop, reached by tapping the mic to stop.
  const win = load();
  const sent = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t), onError: () => {} });
  await l.start();

  latest().emitInterim('where are my keys');
  l.listening = false;         // engine never confirmed it was running
  l.stop();

  assert.deepStrictEqual(sent, ['where are my keys']);
});

test('the deadline still reports silence when nothing was heard', async () => {
  const win = load();
  const sent = [];
  const errors = [];
  const l = new win.Listener({ onResult: (t) => sent.push(t), onError: (m) => errors.push(m) });
  await l.start();

  fireTimer(win, 20000);

  assert.deepStrictEqual(sent, []);
  assert.strictEqual(errors.length, 1, 'twenty seconds of silence deserves a word');
});

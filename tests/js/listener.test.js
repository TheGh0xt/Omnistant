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
  const win = {
    SpeechRecognition: FakeRecognition,
    SpeechSynthesisUtterance: FakeUtterance,
    speechSynthesis: synth,
    navigator: { language: 'en-GB' },
    setTimeout: () => 0,
    clearTimeout: () => {},
    console: { warn() {} },
  };
  // `'speechSynthesis' in global` must be true for Speaker.isSupported().
  new Function('window', SRC)(win);
  win.__synth = synth;
  return win;
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

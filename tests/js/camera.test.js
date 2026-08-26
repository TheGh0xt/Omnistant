/* Regression tests for getting a frame out of a camera that is still opening.
 *
 * getUserMedia resolves well before the video element can be drawn from. For
 * the second or two in between, capture() returns null and isRunning() is
 * false — and "I'm heading out" lands in that gap constantly, because you tap
 * start and then speak. A leave scan with no frame cannot see, so it queues no
 * reminder and the notification never arrives. Nothing errors anywhere.
 */
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'src', 'frontend', 'camera.js'),
  'utf8'
);

/* Minimal stand-ins for the two DOM nodes Camera draws between. */
function fakeVideo() {
  return { readyState: 0, videoWidth: 0, videoHeight: 0 };
}

function fakeCanvas() {
  return {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage() {}, getImageData: () => ({ data: new Uint8Array(0) }) }),
    toDataURL: () => 'data:image/jpeg;base64,FRAME',
  };
}

function load() {
  const win = {
    navigator: {},
    isSecureContext: true,
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };
  // Camera builds its diff canvas from a bare `document`, so the sandbox needs one.
  const doc = { createElement: () => fakeCanvas() };
  new Function('window', 'document', SRC)(win, doc);
  return win;
}

/* A camera whose video becomes decodable after `afterMs`. */
function opening(win, afterMs) {
  const video = fakeVideo();
  const cam = new win.Camera(video, fakeCanvas());
  cam.stream = { getTracks: () => [] };      // getUserMedia has resolved
  setTimeout(() => {
    video.readyState = 4;
    video.videoWidth = 1280;
    video.videoHeight = 960;
  }, afterMs);
  return cam;
}

test('capture() returns nothing while the video is still opening', () => {
  const win = load();
  const cam = opening(win, 10_000);      // never becomes ready during this test

  assert.strictEqual(cam.isRunning(), false);
  assert.strictEqual(cam.capture(), null,
    'this null is what silently cost the scan its eyes');
});

test('captureWhenReady waits for a camera that is still opening', async () => {
  const win = load();
  const cam = opening(win, 250);

  const frame = await cam.captureWhenReady(2500);

  assert.strictEqual(frame, 'data:image/jpeg;base64,FRAME');
});

test('captureWhenReady returns immediately once the camera is live', async () => {
  const win = load();
  const cam = opening(win, 0);
  await new Promise((r) => setTimeout(r, 20));

  const started = Date.now();
  const frame = await cam.captureWhenReady(2500);

  assert.strictEqual(frame, 'data:image/jpeg;base64,FRAME');
  assert.ok(Date.now() - started < 100, 'a live camera must not be waited on');
});

test('captureWhenReady gives up rather than hanging on a camera that never opens', async () => {
  const win = load();
  const cam = opening(win, 10_000);

  const frame = await cam.captureWhenReady(300);

  assert.strictEqual(frame, null);
});

test('captureWhenReady does not wait at all when there is no camera', async () => {
  const win = load();
  const cam = new win.Camera(fakeVideo(), fakeCanvas());   // never started

  const started = Date.now();
  assert.strictEqual(cam.hasStream(), false);
  assert.strictEqual(await cam.captureWhenReady(2500), null);
  assert.ok(Date.now() - started < 100, 'no stream is an answer, not a wait');
});

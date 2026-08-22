/* Camera capture and change detection.
 *
 * getUserMedia needs a secure context: https:// or localhost. Cloud Run gives
 * you https; a plain LAN IP will not work, which is the most common demo-day
 * surprise, so we say so explicitly.
 *
 * The important part here is `hasChanged`. The watch loop ticks every couple of
 * seconds, but sending every frame to a vision model would be both slow and
 * ruinously expensive — and pointless, because a desk nobody has touched
 * contains no new information. So each tick is compared locally against the last
 * frame we actually sent, and only a real change earns an API call.
 */
(function (global) {
  'use strict';

  var MAX_EDGE = 1024;          // plenty for object identification
  var JPEG_QUALITY = 0.8;

  // Difference detection runs on a tiny greyscale thumbnail — enough to notice
  // "a hand moved something", cheap enough to run every tick on a phone.
  var DIFF_W = 32;
  var DIFF_H = 24;

  function Camera(videoEl, canvasEl) {
    this.video = videoEl;
    this.canvas = canvasEl;
    this.stream = null;
    this.facingMode = 'environment';
    this._diffCanvas = document.createElement('canvas');
    this._diffCanvas.width = DIFF_W;
    this._diffCanvas.height = DIFF_H;
    this._lastSignature = null;
  }

  Camera.prototype.isSupported = function () {
    return !!(global.navigator.mediaDevices && global.navigator.mediaDevices.getUserMedia);
  };

  Camera.prototype.isRunning = function () {
    return !!(this.stream && this.video.readyState >= 2);
  };

  Camera.prototype.start = async function () {
    if (!this.isSupported()) {
      throw new Error(
        global.isSecureContext
          ? 'This browser does not support camera access.'
          : 'The camera needs a secure page. Open this over https:// or on localhost.'
      );
    }
    this.stop();
    try {
      this.stream = await global.navigator.mediaDevices.getUserMedia({
        video: { facingMode: this.facingMode, width: { ideal: 1280 }, height: { ideal: 960 } },
        audio: false
      });
    } catch (err) {
      throw new Error(describeCameraError(err));
    }
    this.video.srcObject = this.stream;
    // iOS Safari needs both of these set before play() will succeed inline.
    this.video.setAttribute('playsinline', '');
    this.video.muted = true;
    await this.video.play();
    await this._waitForFrames();
    this._lastSignature = null;
    return this.stream;
  };

  /* play() resolves before the first frame is decoded; capturing too early
     yields a blank image and videoWidth of 0. */
  Camera.prototype._waitForFrames = function () {
    var video = this.video;
    if (video.videoWidth > 0) { return Promise.resolve(); }
    return new Promise(function (resolve) {
      var done = false;
      function finish() { if (!done) { done = true; resolve(); } }
      video.addEventListener('loadeddata', finish, { once: true });
      global.setTimeout(finish, 2000);   // never hang the UI on this
    });
  };

  Camera.prototype.stop = function () {
    if (this.stream) {
      this.stream.getTracks().forEach(function (t) { t.stop(); });
      this.stream = null;
    }
    this.video.srcObject = null;
    this._lastSignature = null;
  };

  Camera.prototype.flip = async function () {
    this.facingMode = this.facingMode === 'environment' ? 'user' : 'environment';
    if (this.stream) { await this.start(); }
  };

  /* Current frame as a `data:image/jpeg;base64,...` URL, or null. */
  Camera.prototype.capture = function () {
    if (!this.isRunning()) { return null; }
    var w = this.video.videoWidth;
    var h = this.video.videoHeight;
    if (!w || !h) { return null; }

    var scale = Math.min(1, MAX_EDGE / Math.max(w, h));
    this.canvas.width = Math.round(w * scale);
    this.canvas.height = Math.round(h * scale);
    this.canvas.getContext('2d').drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);
    return this.canvas.toDataURL('image/jpeg', JPEG_QUALITY);
  };

  /* A coarse greyscale fingerprint of the current frame. */
  Camera.prototype._signature = function () {
    if (!this.isRunning()) { return null; }
    if (!this.video.videoWidth) { return null; }
    var ctx = this._diffCanvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(this.video, 0, 0, DIFF_W, DIFF_H);
    var data = ctx.getImageData(0, 0, DIFF_W, DIFF_H).data;
    var out = new Uint8Array(DIFF_W * DIFF_H);
    for (var i = 0, p = 0; i < data.length; i += 4, p++) {
      // Rec. 601 luma — cheaper than a colour comparison and less twitchy about
      // auto-white-balance drift.
      out[p] = (data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114) | 0;
    }
    return out;
  };

  /**
   * Has the scene changed enough since the last frame we sent?
   *
   * @param {number} threshold mean per-pixel luma difference, 0-255. ~6 ignores
   *        sensor noise and light flicker but catches a hand moving an object.
   * @returns {boolean} true if this frame is worth spending an API call on.
   */
  Camera.prototype.hasChanged = function (threshold) {
    var signature = this._signature();
    if (!signature) { return false; }
    if (!this._lastSignature) { return true; }     // first frame is always new

    var total = 0;
    for (var i = 0; i < signature.length; i++) {
      total += Math.abs(signature[i] - this._lastSignature[i]);
    }
    return (total / signature.length) >= (threshold || 6);
  };

  /* Call once a frame has actually been sent, so the next comparison is against
     what the agent has already seen rather than against every passing frame. */
  Camera.prototype.commitFrame = function () {
    var signature = this._signature();
    if (signature) { this._lastSignature = signature; }
  };

  function describeCameraError(err) {
    switch (err && err.name) {
      case 'NotAllowedError':
        return 'Camera permission was denied. Allow it in your browser settings and try again.';
      case 'NotFoundError':
        return 'No camera found on this device.';
      case 'NotReadableError':
        return 'The camera is already in use by another app.';
      case 'OverconstrainedError':
        return 'This camera does not support the requested mode. Try Flip.';
      default:
        return 'Could not start the camera: ' + ((err && err.message) || 'unknown error');
    }
  }

  global.Camera = Camera;
})(window);

/* Camera capture.
 *
 * getUserMedia needs a secure context: https:// or localhost. On Cloud Run you
 * get https for free; opening the page over a plain LAN IP will fail, which is
 * the single most common demo-day surprise, so we say so explicitly.
 */
(function (global) {
  'use strict';

  // Frames are posted as base64 inside JSON, so keep them small. 1024px on the
  // long edge is plenty for Gemini to identify objects.
  var MAX_EDGE = 1024;
  var JPEG_QUALITY = 0.8;

  function Camera(videoEl, canvasEl) {
    this.video = videoEl;
    this.canvas = canvasEl;
    this.stream = null;
    this.facingMode = 'environment';
  }

  Camera.prototype.isSupported = function () {
    return !!(global.navigator.mediaDevices && global.navigator.mediaDevices.getUserMedia);
  };

  Camera.prototype.isRunning = function () {
    return !!this.stream;
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
    await this.video.play();
    return this.stream;
  };

  Camera.prototype.stop = function () {
    if (this.stream) {
      this.stream.getTracks().forEach(function (t) { t.stop(); });
      this.stream = null;
    }
    this.video.srcObject = null;
  };

  Camera.prototype.flip = async function () {
    this.facingMode = this.facingMode === 'environment' ? 'user' : 'environment';
    if (this.isRunning()) { await this.start(); }
  };

  /* Grab the current frame as a `data:image/jpeg;base64,...` URL. */
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

  function describeCameraError(err) {
    switch (err && err.name) {
      case 'NotAllowedError':
        return 'Camera permission was denied. Allow it in your browser settings and try again.';
      case 'NotFoundError':
        return 'No camera found on this device.';
      case 'NotReadableError':
        return 'The camera is already in use by another app.';
      default:
        return 'Could not start the camera: ' + ((err && err.message) || 'unknown error');
    }
  }

  global.Camera = Camera;
})(window);

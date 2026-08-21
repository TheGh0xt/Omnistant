/* Voice in (Web Speech API) and voice out (speechSynthesis).
 *
 * Four things make naive implementations of this fail, and all four bit here:
 *
 *  1. Synthesis and recognition compete for the audio device. Starting
 *     recognition while the page is speaking ends it instantly, with no error.
 *     That is fatal for a *conversation*: the agent answers aloud, you tap the
 *     mic to reply, and the mic dies. `Listener.start` cancels speech first.
 *
 *  2. Microphone permission. SpeechRecognition reports a denied mic as a
 *     generic error long after the fact. Asking via getUserMedia up front
 *     produces a real browser prompt and a specific reason when it fails.
 *
 *  3. Chrome ends recognition after roughly a second of silence. Any hesitation
 *     before you speak looks like the button "dropping". We restart it while
 *     the user still intends to listen, bounded so it can never spin.
 *
 *  4. iOS refuses to speak until synthesis has been touched inside a user
 *     gesture — but that unlock must not be the same gesture that starts the
 *     mic, or it triggers (1).
 *
 * Support is uneven: recognition is webkit-prefixed in Safari and absent in
 * Firefox. Everything degrades to "typing still works".
 */
(function (global) {
  'use strict';

  var Recognition = global.SpeechRecognition || global.webkitSpeechRecognition;

  // Chrome hangs up on silence; restart this many times before giving up.
  var MAX_RESTARTS = 4;
  // Stop listening entirely after this long with nothing said.
  var LISTEN_DEADLINE_MS = 20000;
  // If the engine has not reported that it started within this long, it is not
  // going to. Some builds (Chromium without the speech service, locked-down
  // enterprise browsers) accept start() and then do nothing at all — leaving the
  // UI showing "listening…" forever unless we time it out ourselves.
  var START_TIMEOUT_MS = 2500;

  /* ---------------- speaking ---------------- */
  var Speaker = {
    _unlocked: false,

    isSupported: function () { return 'speechSynthesis' in global; },

    /* Call from a user gesture that is NOT the mic button — sending a message,
       tapping a suggestion. Speaks a silent utterance to satisfy iOS, then
       cancels it so the audio device is free again. */
    unlock: function () {
      if (!this.isSupported() || this._unlocked) { return; }
      try {
        var u = new global.SpeechSynthesisUtterance('');
        u.volume = 0;
        global.speechSynthesis.speak(u);
        global.speechSynthesis.cancel();
        this._unlocked = true;
      } catch (err) { /* not fatal: replies simply will not be read aloud */ }
    },

    isSpeaking: function () {
      return this.isSupported() && (global.speechSynthesis.speaking || global.speechSynthesis.pending);
    },

    speak: function (text) {
      if (!this.isSupported() || !text) { return; }
      global.speechSynthesis.cancel();
      var u = new global.SpeechSynthesisUtterance(text);
      u.rate = 1.02;
      u.pitch = 1.0;
      u.lang = global.navigator.language || 'en-US';
      global.speechSynthesis.speak(u);
    },

    stop: function () {
      if (this.isSupported()) { global.speechSynthesis.cancel(); }
    }
  };

  /* ---------------- listening ---------------- */
  function Listener(opts) {
    opts = opts || {};
    this.onResult = opts.onResult || function () {};
    this.onPartial = opts.onPartial || function () {};
    this.onStateChange = opts.onStateChange || function () {};
    this.onError = opts.onError || function () {};

    this.wanted = false;      // does the user currently want to be heard?
    this.listening = false;   // is the engine actually running?
    this.restarts = 0;
    this.gotSpeech = false;
    this.recognition = null;
    this.deadline = null;
    this.permission = null;   // null = unknown, true/false once resolved
    this._announced = false;  // last state handed to onStateChange
  }

  /* Aborting a recogniser also fires its onend, so a single failure can try to
     report "stopped" twice. Only surface real transitions. */
  Listener.prototype._setState = function (active) {
    if (this._announced === active) { return; }
    this._announced = active;
    this.onStateChange(active);
  };

  Listener.prototype.isSupported = function () { return !!Recognition; };

  /* Ask for the microphone explicitly so the browser shows its prompt and we
     get a reason we can put in front of the user. */
  Listener.prototype.ensureMicPermission = async function () {
    if (this.permission === true) { return true; }
    if (!(global.navigator.mediaDevices && global.navigator.mediaDevices.getUserMedia)) {
      // No getUserMedia: let recognition try on its own rather than blocking.
      this.permission = true;
      return true;
    }
    try {
      var stream = await global.navigator.mediaDevices.getUserMedia({ audio: true });
      // We only wanted the grant; recognition opens its own stream.
      stream.getTracks().forEach(function (t) { t.stop(); });
      this.permission = true;
      return true;
    } catch (err) {
      this.permission = false;
      this.onError(describeMicError(err));
      return false;
    }
  };

  Listener.prototype._build = function () {
    var self = this;
    var rec = new Recognition();
    rec.lang = global.navigator.language || 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;

    rec.onstart = function () {
      self.listening = true;
      self._clearTimer('startTimer');
      self._setState(true);
    };

    rec.onresult = function (event) {
      var finalText = '';
      var partial = '';
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var result = event.results[i];
        if (result.isFinal) { finalText += result[0].transcript; }
        else { partial += result[0].transcript; }
      }
      if (partial) { self.gotSpeech = true; self.onPartial(partial.trim()); }
      if (finalText.trim()) {
        self.gotSpeech = true;
        self.wanted = false;          // a complete utterance ends the turn
        self.onResult(finalText.trim());
      }
    };

    rec.onerror = function (event) {
      switch (event.error) {
        case 'no-speech':
          // Expected: the engine gave up waiting. onend will restart us.
          return;
        case 'aborted':
          return;                     // we stopped it deliberately
        case 'not-allowed':
        case 'service-not-allowed':
          self.wanted = false;
          self.permission = false;
          self.onError('Microphone access is blocked. Allow it in your browser settings, then try again.');
          return;
        case 'audio-capture':
          self.wanted = false;
          self.onError('No microphone found. Check your input device and try again.');
          return;
        case 'network':
          self.wanted = false;
          self.onError('Speech recognition needs a network connection and could not reach the service.');
          return;
        default:
          self.wanted = false;
          self.onError('Speech recognition failed: ' + event.error);
      }
    };

    rec.onend = function () {
      self.listening = false;
      // Chrome ends on brief silence. If the user still wants to talk and we
      // have not heard anything yet, start it again.
      if (self.wanted && self.restarts < MAX_RESTARTS && Date.now() < self.deadline) {
        self.restarts += 1;
        self._spin();
        return;
      }
      if (self.wanted && !self.gotSpeech) {
        self.onError("I didn't hear anything. Tap the mic and speak, or just type.");
      }
      self.wanted = false;
      self._clearTimer('startTimer');
      self._clearTimer('deadlineTimer');
      self._setState(false);
    };

    return rec;
  };

  Listener.prototype._clearTimer = function (name) {
    if (this[name]) { global.clearTimeout(this[name]); this[name] = null; }
  };

  Listener.prototype._spin = function () {
    var self = this;
    this.recognition = this._build();

    // Watchdog: the engine must tell us it started, or we assume it never will.
    this._clearTimer('startTimer');
    this.startTimer = global.setTimeout(function () {
      if (!self.listening && self.wanted) {
        self.wanted = false;
        self.abandon();
        self.onError(
          'Speech recognition did not start. Your browser may not support it — ' +
          'Chrome and Safari do, Firefox does not. You can type instead.'
        );
        self._setState(false);
      }
    }, START_TIMEOUT_MS);

    try {
      this.recognition.start();
    } catch (err) {
      this._clearTimer('startTimer');
      // start() throws if a previous instance has not fully released yet.
      this.wanted = false;
      this.listening = false;
      this._setState(false);
      this.onError('Could not start listening: ' + err.message);
    }
  };

  Listener.prototype.start = async function () {
    if (!this.isSupported() || this.wanted) { return false; }

    // (1) Never start the mic while the page is talking — recognition would end
    // immediately and silently.
    Speaker.stop();

    // (2) Get a real permission decision before touching the recogniser.
    if (!(await this.ensureMicPermission())) { return false; }

    this.wanted = true;
    this.restarts = 0;
    this.gotSpeech = false;
    this.deadline = Date.now() + LISTEN_DEADLINE_MS;
    this._setState(true);

    // Hard stop, independent of engine events: some browsers never fire onend.
    var self = this;
    this._clearTimer('deadlineTimer');
    this.deadlineTimer = global.setTimeout(function () {
      if (self.wanted) {
        self.wanted = false;
        self.abandon();
        if (!self.gotSpeech) {
          self.onError("I stopped listening after 20 seconds without hearing anything.");
        }
        self._setState(false);
      }
    }, LISTEN_DEADLINE_MS);

    this._spin();
    return true;
  };

  /* Release the recogniser without relying on it to call us back. */
  Listener.prototype.abandon = function () {
    this._clearTimer('startTimer');
    this._clearTimer('deadlineTimer');
    if (this.recognition) {
      try { this.recognition.abort(); } catch (err) { /* already gone */ }
      this.recognition = null;
    }
    this.listening = false;
  };

  Listener.prototype.stop = function () {
    this.wanted = false;
    this._clearTimer('startTimer');
    this._clearTimer('deadlineTimer');
    if (this.recognition && this.listening) {
      try { this.recognition.stop(); } catch (err) { /* already stopped */ }
    } else {
      this.abandon();
    }
    this._setState(false);
  };

  Listener.prototype.toggle = function () {
    if (this.wanted) { this.stop(); return Promise.resolve(false); }
    return this.start();
  };

  function describeMicError(err) {
    switch (err && err.name) {
      case 'NotAllowedError':
        return global.isSecureContext
          ? 'Microphone permission was denied. Allow it in your browser settings and tap the mic again.'
          : 'The microphone needs a secure page. Open this over https:// or on localhost.';
      case 'NotFoundError':
        return 'No microphone found on this device.';
      case 'NotReadableError':
        return 'The microphone is already in use by another app.';
      default:
        return 'Could not access the microphone: ' + ((err && err.message) || 'unknown error');
    }
  }

  global.Listener = Listener;
  global.Speaker = Speaker;
})(window);

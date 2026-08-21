/* Voice in (Web Speech API) and voice out (speechSynthesis).
 *
 * Support is uneven: recognition is webkit-prefixed in Safari and absent in
 * Firefox. Everything here degrades to "typing still works" rather than
 * throwing, because the text box is always the fallback.
 */
(function (global) {
  'use strict';

  var Recognition = global.SpeechRecognition || global.webkitSpeechRecognition;

  function Listener(opts) {
    opts = opts || {};
    this.onResult = opts.onResult || function () {};
    this.onPartial = opts.onPartial || function () {};
    this.onStateChange = opts.onStateChange || function () {};
    this.onError = opts.onError || function () {};
    this.listening = false;
    this.recognition = null;
  }

  Listener.prototype.isSupported = function () { return !!Recognition; };

  Listener.prototype._build = function () {
    var self = this;
    var rec = new Recognition();
    rec.lang = global.navigator.language || 'en-US';
    rec.interimResults = true;
    rec.continuous = false;      // one utterance per press: this is push-to-talk
    rec.maxAlternatives = 1;

    rec.onresult = function (event) {
      var finalText = '';
      var partial = '';
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var result = event.results[i];
        if (result.isFinal) { finalText += result[0].transcript; }
        else { partial += result[0].transcript; }
      }
      if (partial) { self.onPartial(partial.trim()); }
      if (finalText.trim()) { self.onResult(finalText.trim()); }
    };
    rec.onerror = function (event) {
      self.listening = false;
      self.onStateChange(false);
      if (event.error === 'no-speech') { self.onError("I didn't catch that."); }
      else if (event.error === 'not-allowed') { self.onError('Microphone permission was denied.'); }
      else if (event.error !== 'aborted') { self.onError('Speech recognition failed: ' + event.error); }
    };
    rec.onend = function () { self.listening = false; self.onStateChange(false); };
    return rec;
  };

  Listener.prototype.start = function () {
    if (!this.isSupported() || this.listening) { return false; }
    this.recognition = this._build();
    try {
      this.recognition.start();
    } catch (err) {
      this.onError('Could not start listening: ' + err.message);
      return false;
    }
    this.listening = true;
    this.onStateChange(true);
    return true;
  };

  Listener.prototype.stop = function () {
    if (this.recognition && this.listening) { this.recognition.stop(); }
  };

  Listener.prototype.toggle = function () {
    return this.listening ? (this.stop(), false) : this.start();
  };

  /* ---- speaking ---- */
  var Speaker = {
    isSupported: function () { return 'speechSynthesis' in global; },

    /* iOS will not speak until synthesis has been touched inside a user
       gesture; call this from the first tap to unlock it. */
    unlock: function () {
      if (!this.isSupported() || this._unlocked) { return; }
      var u = new global.SpeechSynthesisUtterance('');
      u.volume = 0;
      global.speechSynthesis.speak(u);
      this._unlocked = true;
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

  global.Listener = Listener;
  global.Speaker = Speaker;
})(window);

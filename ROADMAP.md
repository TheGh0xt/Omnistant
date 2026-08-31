# Roadmap and known limitations

Everything below was found while building, not imagined afterwards. Each item
says *why* it matters, because a roadmap without the reasoning gets pruned by
whoever reads it next and doesn't remember.

Status of the three build-spec workflows: all shipped and live. This is what is
**not** done.

---

## Before submission

- [ ] **Enable billing on the GCP project.** Free tier is 5 vision requests per
      minute and **20 per day**. Continuous watching exhausts that within
      minutes of real movement, and the demo visibly dies mid-recording. This is
      the single highest-risk item.
- [x] **Record the demo** (3–5 min, unedited).
- [ ] **Rotate the Slack webhook.** The current URL was pasted into a chat
      transcript. A webhook URL *is* the credential — anyone holding it can post
      to the channel.
- [x] **Merge the open PR** so `main` is what a judge clones.
- [x] **Redeploy at the production cadence.** Done: the live service runs a
      2-minute nudge delay, drains every 5 minutes, and narrates the recap
      again. A reminder is therefore *due* 2 minutes after you leave and
      *delivered* within 5 more, so budget 2-7 minutes end to end when testing.
- [x] **Re-seed the learned routines after the demo.** Evening leave-scans during
      recording had dragged the learned `work` departure to 19:44 — real learning
      behaving correctly on unrepresentative input, but it left the brief
      describing a day nobody has. Reset to 08:45, which the endpoint now reports
      as a window of 08:20-09:00. It will drift again if the next leave-scans are
      unrepresentative; that is the feature working, not a regression.

---

## The one that matters: departure detection

**Today the agent only knows you are leaving because you told it.**

If you walk out without opening the app, nothing fires — which is the same
forgetting the product exists to fix. Everything else is downstream of this.

The web cannot close it:

| What's needed | Why the browser can't |
|---|---|
| Notice you left the house | No background geolocation. A tab must be open and focused. |
| Keep watching once the screen sleeps | No background execution. The eyes close with the tab. |
| Wake on a geofence crossing | No geofence API at all. |

**The fix is a native app** (React Native / Expo is the obvious fit). That turns
"I'm heading out" from something you have to *say* into something the agent
*notices* — at which point the whole product works the way it was described.

**Mitigation shipped in the meantime:** the pre-departure brief fires in the 25
minutes before the time this routine has actually been observed to happen,
learned from your own departures. It is a decent guess, not a trigger.

---

## Post-hackathon

### 1 · Native mobile app — *the unblocker*
Geofencing and background execution, per above. Everything else on this list is
easier once it exists.

### 2 · Meta Ray-Ban smart glasses — *funding and traction dependent*
The form factor the product actually wants. A phone you have to hold up is a
worse version of glasses you already wear, and head-mounted capture removes the
last friction we could not design away: remembering to point something at your
desk.

Not idle: the vision layer is a single module behind an interface —
`scan_frame(data_url) -> VisionResult`, two call sites — so the camera source is
swappable without touching a workflow. Keep it that way.

### 3 · A local wake-word engine
`webkitSpeechRecognition` is **server-based**: always-listening streams
microphone audio to the browser vendor. The UI says so plainly, which is the
right thing to do about it — but an on-device engine (Porcupine or similar) is
the right thing to *fix* it. Also removes the restart loop the current
implementation needs.

### 4 · Real authentication
Cloud Run is deployed `--allow-unauthenticated`, and the app is single-user via
`DEFAULT_USER_ID`. **Anyone with the URL can read the observation log** — which,
by design, is a record of your belongings and movements. Fine for a demo, not
for a second user. Identity-Aware Proxy in front, or real auth and a per-user id.

### 5 · Correcting the agent
There is no way to tell it that it got something wrong — "that's not my laptop",
"those are someone else's keys". An append-only log makes this a new
*correcting* observation rather than an edit, which is the right shape; the tool
and the UI are missing.

### 6 · Item identity
Two pairs of AirPods are one subject called `airpods`. Recall will confidently
merge them. Needs per-item identity, not just a normalised name.

### 7 · Per-item confidence decay
The half-life is global (6h). Keys move constantly; a laptop on a desk does not.
Decay should be learned per item from how often that thing is actually observed
to move.

### 8 · Multi-device sync and day-of-week routines
A Tuesday gym bag is not a Saturday one. Routines are currently per-destination
only.

---

## Smaller known gaps

- **Timeline condenses to six moments.** Necessary — ten timestamps collide on a
  375px rail — but the selection heuristic (prefer locations and activities,
  then sample evenly) is a first guess, not a studied one.
- **Redis is opt-in and currently unprovisioned.** Camera frames and watch state
  live in an in-process cache, so they do not survive a cold start or a second
  instance. This is no longer harmless: `--max-instances` is now 4, and
  `/health` on the deployed service reports `redis: fallback (memory)`, so a
  leave scan can land on an instance that never saw the frame the watch loop
  cached. Sessions are unaffected — those are Postgres-backed. Fix is
  `USE_MEMORYSTORE=1` or an external `rediss://` URL.
- ~~**No CI.**~~ Shipped: `.github/workflows/tests.yml` runs the suite on every
  push and PR, under both `pytest` and `python -m pytest`, because the suite was
  once uncollectable under one of them while passing under the other. No secrets
  needed — the suite is hermetic.
- **No rate limiting** on the public endpoints.
- **`/health` reports Gemini as configured, not reachable.** It checks that
  credentials exist, not that a model answers — which is how a Vertex region
  misconfiguration passed the health check while 404ing every call. A real
  `unreachable` state needs a live model call, which costs quota on every Cloud
  Run probe, so it needs a cached result rather than a naive check.
- **Location labels are stated intent, not position.** Saying "I'm heading to
  work" sets the label until the next announcement; if you then don't go, the
  observations are mislabelled. Real positioning needs the native app above.
  `DEFAULT_LOCATION_LABEL` sets the resting label ("Home"), which is wrong for
  anyone whose day does not start there.
- **The wake word is verified by unit test, not on real devices.** The failure
  counter and the suspend/resume wiring are pinned by `tests/js/speech.test.js`
  and `tests/js/wake_recovery.test.js` against a stubbed engine. That cannot tell
  you how Safari behaves — its continuous recognition is known-flaky, and Firefox
  has no recognition at all. The pattern is also deliberately forgiving of
  mis-transcription, so a false wake is possible; it now costs a returned
  "listening" state rather than a dead recogniser.
- **The evening recap has no delivery trigger tied to behaviour** — it is still a
  fixed 21:00, unlike the brief, which now learns.
- **The brief's two gates have to be kept in agreement by hand.** It fires only
  inside a window around the learned departure time, *and* only when the
  scheduler ticks. Nothing in the running service notices when those stop
  overlapping — a learned time the cron never reaches produces no brief and no
  error, only "outside the departure window" in the logs. The cron now ticks all
  day and a test pins the invariant, but the real fix is for the job to own its
  own schedule rather than splitting it across code and `deploy.sh`.

---

## Deliberate non-goals

Worth writing down so they don't get "fixed" by accident:

- **The model never sources a fact.** Gemini does the seeing and the wording;
  every claim is computed by a deterministic workflow over the log. Do not let a
  model's guess become a stored fact.
- **No dark mode.** High contrast is fatiguing for the target user. This is a
  design decision, not an omission.
- **Silence when nothing changed.** The watch loop says nothing on an unchanged
  scene. Narrating a static world is the nagging this replaces.
- **Reminders cancel themselves.** If the item has been seen since, nothing is
  sent. Telling someone they forgot the keys they are holding is what gets an
  assistant muted.

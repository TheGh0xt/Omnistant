# Personal Context Agent — Engineering Log

**Date Started:** August 20, 2026
**Deadline:** September 1, 2026 @ 1:00am GMT+1
**Target Track:** The Taskmaster

---

## Decisions Log

### Decision 1: PostgreSQL + Redis vs Firestore
**Date:** Aug 20
**Status:** DECIDED
**Choice:** PostgreSQL + Redis hybrid

**Reasoning:**
- Observation log needs complex temporal queries
- State needs sub-100ms cache hits
- Google Cloud SQL + Memorystore are managed services
- Avoids Firestore's document-model complexity

**Trade-offs:**
- More operational overhead
- Cleaner separation of concerns
- Better for judges (shows systems thinking)

**Outcome:** ✅ WORKING [Update after build]

---

### Decision 2: Frontend Tech (Native vs Web)
**Date:** Aug 20
**Status:** DECIDED
**Choice:** Web frontend (vanilla HTML/JS + Camera API)

**Reasoning:**
- Avoids App Store review cycle
- Works in Safari on demo device
- Faster iteration
- Judges care about UX + logic, not iOS polish

**Trade-offs:**
- Limited by browser security sandbox
- No background processing (use Cloud Scheduler instead)
- Smaller attack surface ✅

**Outcome:** ✅ WORKING [Update after build]

---

## Mistakes & Learnings

### Aug 25 — four fixes from real use, not from a checklist

All four came out of actually using the thing on Aug 24, which is the only way
any of them would have been found. Grouped because three share a shape: the
output was *correct* and still wrong.

---

**1. The wake word disabled itself after working fine for an hour.**

*Symptom:* "Wake word kept failing, so I turned it off."

*Root cause:* not the restart loop. `WakeWord.MAX_FAILURES` and exponential
backoff were already there and already correct. The bug was that `failures` was
a **lifetime counter**: it was cleared on a wake match, on `enable()` and on
`resume()`, and nowhere else. The Web Speech API is server-based, so a `network`
error every so often is normal weather. Five of them — spread across an hour of
otherwise perfect listening — tripped the give-up threshold. The message arrived
after nothing had failed for forty minutes.

*Fix:* make the threshold mean *consecutive* failures. A session that produced a
transcript, or that ran ≥8s and ended without error, clears the streak. Added
lifecycle tracing (`window.OMNISTANT_DEBUG = true`) so the next one of these is
diagnosable from a console rather than by inference.

*Trade-off:* a genuinely intermittent engine — failing just under the reset
threshold forever — will now retry indefinitely instead of giving up. That is the
right side to err on: the failure mode of retrying is a background reconnect the
user never sees, and the failure mode of giving up is silently losing the
feature. Backoff caps the cost at one attempt per 8s.

*Verified:* the two behavioural tests in `tests/js/speech.test.js` fail against
the pre-fix file and pass against the fixed one — checked by running them against
`git show HEAD:src/frontend/speech.js`. Browser verification is still outstanding:
these are unit tests against a stubbed `SpeechRecognition`, which pins the counter
logic and cannot tell you how Safari behaves. Chrome/Edge are the reliable
engines; Safari's continuous recognition is known-flaky and Firefox has none at
all. **Needs a real-device pass before the demo.**

---

**2. Every observation was logged at "here".**

*Root cause:* the frontend never sent a location to `/api/observe`, so
`watch.py` fell back to the string `"here"`. Meanwhile the leave-detection
workflow was *being told* the destination in words — "I'm heading to work" — and
throwing it away. The evening recap rendered the result as "Places: here", a line
of a notification that answers nothing.

*Fix:* `utils/location.py`. An announced destination becomes the session's
current label and holds until the next announcement. Resting state is
`DEFAULT_LOCATION_LABEL` (default "Home").

*Trade-offs and limits, stated plainly:*
- "Home" is only right for someone whose day starts at home. Configurable, and
  `DEFAULT_LOCATION_LABEL=""` makes it say "Unknown location" instead of guessing.
- The label is **stated intent, not position**. If you say you're heading to work
  and then don't go, observations are mislabelled until you say otherwise. Real
  geofencing needs the native app — see ROADMAP.
- It is session-scoped. A new session starts back at the default.

---

**3. The daily recap was a paragraph.**

*Not a data bug* — every fact in it was correct. It opened by apologising
("extremely sparse"), chained four clauses into one sentence, and buried the
timestamps mid-clause. For someone who is being reminded of things precisely
because holding them in working memory is hard, that is the shape most likely to
go unread.

*Fix:* `utils/notify_format.py` — one shared formatter for all three
notification types. Emoji anchor, `📍 Where`, time-first bullets capped at ~8
words, italic footer. Shared rather than per-workflow so the three cannot drift.

*Design note worth keeping:* the bullets are built from the observation log
directly, not narrated by a model. That follows the standing rule (the model does
the seeing and the wording of speech, never the sourcing of a fact) and has a
second benefit — no model call means the recap compiles in milliseconds, which is
what makes fix 4 possible.

---

**4. The autonomous loop could not be filmed.**

Production cadence is correct and unfilmable: nobody can record themselves
waiting six hours. The proof that the agent acts *on its own* has to fit in one
unedited take.

*Fix:* the cadence is configuration, not code. Same trigger, same compilation,
same delivery.

| Lever | Production | Demo recording |
|---|---|---|
| `LEAVE_NUDGE_DELAY_MINUTES` | `2` | `0.5` (30s) |
| `DRAIN_CRON` (deploy.sh) | `*/5 * * * *` | `* * * * *` (Scheduler's floor) |
| `RECAP_NARRATE` | `true` | `false` — skips the model round trip |

All three scheduled endpoints now log `scheduler job start` / `scheduler job
done` with `duration_ms`, so the loop can be timed *before* recording:

```bash
gcloud run services logs read omnistant --region us-central1 | grep "scheduler job"
```

*Trade-off, and the honest framing if a judge asks:* this is the same code path
on a different cadence, not a fake trigger. The production defaults are the
realistic ones and a test asserts they stay that way
(`test_the_shipped_defaults_are_the_realistic_ones`). Worst case on the demo
cadence is ~90s from scan to Slack (30s nudge delay + up to 60s until the next
drain tick), so budget the take accordingly rather than assuming 45s.

---

## Build Status

- [x] Day 1-2: Agent engine + workflow setup
- [x] Day 3: Leave detection workflow
- [x] Day 4: Item recall workflow
- [x] Day 5: Timeline reconstruction
- [x] Day 6: Frontend integration — rebuilt to the mobile handoff (memory globe,
      voice bar, timeline bar, wake word)
- [x] Day 7: Cloud Run deployment — live at https://omnistant-3oe5odab6a-uc.a.run.app
- [ ] Day 8: Demo recording  ← **blocked on enabling billing**
- [ ] Day 9: Devpost submission

Beyond the original plan:

- [x] Continuous observation — the agent watches rather than answering once
- [x] Delayed leave reminders that re-check and cancel themselves
- [x] Departure time learned from behaviour instead of a fixed 08:00
- [x] Slack delivery for the autonomous jobs
- [x] Postgres-backed sessions (conversations survive restarts)
- [x] CI on every push and PR, Python and JS
- [x] Location labels from stated intent, replacing "here"

Known gaps and everything post-hackathon: [ROADMAP.md](ROADMAP.md)

---

## Key Insights (for next project)

### What Worked
- [To be filled]

### What Would Change
- [To be filled]

### Lessons Learned
- [To be filled]
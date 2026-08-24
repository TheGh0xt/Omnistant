# Devpost submission — Omnistant

**Category:** Taskmaster
**Live:** https://omnistant-3oe5odab6a-uc.a.run.app
**Repo:** https://github.com/TheGh0xt/Omnistant

---

## Inspiration

People with ADHD don't forget that they own things. They forget what *happened*
to them. The wallet isn't lost — the memory of setting it down is.

Every existing tool answers the wrong question. Reminder apps need you to
remember to set a reminder. Trackers only work on things you remembered to tag.
Both require the exact faculty that isn't available at the moment you need it.

So the agent has to be the one paying attention.

## What it does

**It watches.** Turn the camera on and Omnistant keeps looking — not one photo
when you ask, but a running picture of what's in front of it. It notices when
something *appears*, and it notices when something that was there is *gone*.
That second one is how it captures you putting your AirPods in your bag: not by
seeing the bag, but by seeing the AirPods stop being on the desk.

**It catches you at the door.** Say "I'm heading out" and it compares what the
camera can actually see against what you normally carry to that destination.
Anything missing comes back with where it was last seen.

**It follows you out.** Two minutes after you leave, if something is still
missing, it posts to Slack — with the location, so you can decide in one glance
whether it's worth turning back:

> **You left without something**
> You headed to work about 2 minutes ago without your AirPods.
> Last seen: home, on the kitchen counter, 8:31 AM.

That reminder cancels itself. If the agent has seen the item since the scan that
raised it, nothing is sent. Telling someone they forgot the keys they're holding
is exactly the noise that gets an assistant muted.

**It remembers.** "Where are my AirPods?" is answered from a log of real
sightings, each with a confidence that decays as it ages — a sighting from ten
minutes ago is an answer, one from yesterday is a lead, and it says which.
"What did I do today?" reconstructs the day as a glanceable bar, not a list.

**It acts with nobody there.** A pre-departure brief fires in the 25 minutes
before you leave — at the time this routine has actually been *observed* to
happen, learned from your own departures, not a fixed hour someone guessed.

## How we built it

Google ADK orchestrates the agent; Gemini 3.5 Flash does the seeing and the
wording. The critical decision is what it *doesn't* do: **Gemini never sources a
fact.** Every claim — what's missing, when something was last seen — is computed
by a deterministic workflow over an append-only PostgreSQL log. Tools are the
surface the model can reach; workflows are where decisions get made. There is no
code path where a model's guess becomes a stored fact.

Continuous watching is only affordable because most frames are never sent. Each
tick is compared locally against the last frame actually transmitted — a 32×24
greyscale fingerprint, mean luma delta. A still desk costs zero API calls; only
real movement earns one.

Sessions are Postgres-backed, so conversations survive restarts and instance
changes. Delayed reminders are rows with due times, drained by a scheduler job,
because Cloud Run scales to zero and nothing can sit in memory waiting.

Cloud Run, Cloud SQL, Cloud Scheduler, Secret Manager. Vanilla-JS mobile
frontend: a draggable memory globe, Camera API, Web Speech API, opt-in wake word.

## Challenges

Four bugs that all failed *quietly*, because the system degrades gracefully:

- **Recall matched a leave-scan's `missing` list.** Asking "where are my keys?"
  returned a sighting at the exact moment the agent had established they were
  nowhere.
- **Voice input died instantly.** Speech synthesis and recognition compete for
  the audio device, so the agent answering aloud killed the mic — precisely the
  conversational path.
- **The wake word never fired.** "Omni" is an invented word, so recognition
  never returns it twice the same way. Worse, `"Hey, Omni!"` failed even when the
  words were right: stripping the comma left a double space and the match missed.
- **Durable sessions shipped broken.** `requirements.txt` was stale, so
  production silently fell back to in-memory — the fix undone by packaging.

That last one was caught by making `/health` name the backend in use instead of
saying "up". The two backend bugs have regression tests; the two browser ones are
fixed in `src/frontend/speech.js`, where the reasoning sits in comments at the
fix site — the repo has no JS test runner.

## What we learned

**Graceful degradation hides bugs.** Every subsystem here falls back rather than
crashing, which is right — and it means a broken deploy looks healthy. Health
checks have to report *which* backend is live, not whether something answered.

**A reminder's value decays with distance.** Five minutes on foot is 400m and an
easy turn-back; five minutes driving is 3km and isn't. Arriving early costs a
glance at a phone. Arriving late costs the thing for the day.

## What's next

**A native mobile app.** This is a phone-first product built as a web app,
deliberately: a hackathon is short, and the browser let us ship the whole loop —
camera, voice, continuous watching, deployment — inside the time available. It
also cost us two things worth naming. There is no background location, so the
agent only knows you're leaving because you told it; and there is no background
execution, so the eyes close when the tab does. Going native closes both, and
turns "I'm heading out" from something you say into something it notices.

**Meta Ray-Ban smart glasses**, if this finds funding and traction. That is the
form factor the product actually wants. Everything here is built around a camera
that sees what you see, and a phone you have to hold up is a worse version of
glasses you already wear. Head-mounted capture removes the last bit of friction
we could not design away: remembering to point something at your desk. It is a
bet on distribution and hardware access rather than a next sprint — but it is
the direction every architectural decision here already points, which is why the
vision layer is one module behind an interface rather than something welded to
the browser's camera.

Then multi-device sync and routines learned per day-of-week — a Tuesday gym bag
is not a Saturday one.

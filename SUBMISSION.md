# Devpost submission — Omnistant

**Category:** Taskmaster
**Live:** https://personal-context-agent-3oe5odab6a-uc.a.run.app
**Repo:** https://github.com/TheGh0xt/Omnistant

---

## Inspiration

People with ADHD don't forget that they own things. They forget what *happened*
to them. The wallet isn't lost — the memory of setting it down is. Existing
tools answer the wrong question: reminder apps need you to remember to set a
reminder, and trackers only work on things you remembered to tag.

## What it does

Omnistant holds that thread for you. Say *"I'm going to work"* and it takes a
camera frame, identifies what's actually in it, compares that against what you
normally carry to work, and tells you what's missing — along with where it was
last seen. Ask *"where are my AirPods?"* and it answers from a log of real
sightings, with a confidence that decays as the sighting ages. Ask *"what did I
do today?"* and it reconstructs the day from what it observed.

Then it does the part that makes it an agent: at 08:00, with nobody watching, it
checks what it can't currently vouch for and posts to Slack before you leave.

## How we built it

Google ADK orchestrates the agent; Gemini 3.5 Flash does the seeing and the
wording. The critical design decision is what it *doesn't* do: Gemini never
sources a fact. Every claim — what's missing, when something was last seen — is
computed by a deterministic workflow over an append-only PostgreSQL log. Tools
are the surface the model can reach; workflows are where decisions get made.
There is no code path where a model's guess becomes a stored fact.

Sessions are Postgres-backed, so conversations survive restarts and instance
changes. Cloud Run, Cloud SQL, Cloud Scheduler. Vanilla JS frontend using the
Camera and Web Speech APIs.

## Challenges

Recall originally matched a leave-scan's *missing* list, so asking "where are my
keys?" returned a sighting at the exact moment the agent had established they
were nowhere. Voice input died instantly because speech synthesis and
recognition compete for the audio device — the agent answering aloud killed the
mic. And durable sessions shipped broken: `requirements.txt` was stale, so
production silently fell back to in-memory. Every one of those failed *quietly*,
because the system degrades gracefully. There are now regression tests for all
three.

## What we learned

Graceful degradation hides bugs. Health endpoints must name the backend in use,
not just say "up" — an in-process fallback always answers healthy.

## What's next

True geofencing, multi-device sync, and learning routines per day-of-week.

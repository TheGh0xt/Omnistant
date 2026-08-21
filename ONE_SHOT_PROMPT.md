# Personal Context Agent — Comprehensive Build Spec

## Problem Statement
People with ADHD don't forget they own things; they forget what happened to them. 
They need an autonomous agent that continuously observes their context (location, time, items) 
and acts without prompting to:
1. Flag missing items before leaving
2. Recall where things were last seen
3. Reconstruct their day from observations

## Success Criteria
- Three complete workflows working end-to-end
- Deployable to Google Cloud Run
- Real demo data from actual test runs
- Clean, documented codebase
- GitHub-ready with deployment instructions

## Technology Stack (Required)
- Gemini 3.5 via Google AI SDK (or Vertex AI)
- Google ADK for agent orchestration
- PostgreSQL for event log (or Cloud SQL if on GCP)
- Redis for state cache
- Web frontend: vanilla HTML/JS with Camera API + Web Speech API
- Google Cloud Run for deployment
- Cloud Scheduler for scheduled triggers (Cloud Pub/Sub optional)

## Three Core Workflows

### Workflow 1: Leave Detection
User says: "I'm going to work"
Agent: 
  1. Recognizes intent (location change)
  2. Retrieves learned routine for "work" (expected items)
  3. Initiates multimodal scan (camera input)
  4. Uses Gemini vision to identify items in frame
  5. Compares against routine (X missing, Y found)
  6. Acts: "You're missing your AirPods. You usually take them to work."

### Workflow 2: Item Recall
User asks: "Where are my AirPods?"
Agent:
  1. Searches observation log for "AirPods"
  2. Finds timestamped entries: [home 8:42 AM], [office 9:21 AM], [no confirmation after]
  3. Returns: "Last confirmed at home at 8:42 AM, before you left for work."
  4. Shows confidence level based on recency/verification method

### Workflow 3: Daily Timeline
User asks: "What did I do today?"
Agent:
  1. Reconstructs day from observations
  2. Extracts: times, locations, activities
  3. Returns narrative: "You left home at 8:47, arrived at office around 9:20, went to lunch at 12:30, returned at 13:45..."
  4. Can answer follow-ups: "What was I doing at 2 PM?"

## Architecture

┌─────────────────────────────────┐
│ Web Frontend │
│ (Camera + Voice + Text) │
└────────────┬────────────────────┘
│
▼
┌─────────────────────────────────┐
│ Google ADK Agent Runtime │
│ (Intent → Planning → Tools) │
└────────────┬────────────────────┘
│
┌────────┴────────┐
▼ ▼
┌──────────────┐ ┌──────────────┐
│ MCP Tools: │ │ State Mgmt: │
│ - Context │ │ - Session │
│ - Memory │ │ - Routine │
│ - Timeline │ │ - Learned │
└──────┬───────┘ └──────┬───────┘
│ │
└────────┬────────┘
▼
┌────────────────────────┐
│ Persistent State │
├────────────────────────┤
│ PostgreSQL Event Log │
│ (observations table) │
│ Redis State Cache │
└────────────────────────┘


## Data Model (PostgreSQL)

```sql
-- Observations table
CREATE TABLE observations (
  id UUID PRIMARY KEY,
  user_id UUID,
  observation_type VARCHAR (item, location, activity),
  content JSONB,
  timestamp TIMESTAMP,
  location POINT,
  confidence FLOAT,
  verification_method VARCHAR (visual, voice, manual),
  created_at TIMESTAMP
);

-- Routines table
CREATE TABLE routines (
  id UUID PRIMARY KEY,
  user_id UUID,
  routine_name VARCHAR (work, home, shopping),
  expected_items JSONB,
  location POINT,
  typical_time TIME,
  created_at TIMESTAMP
);

-- Sessions table (Redis-backed)
{
  session_id: UUID,
  user_id: UUID,
  current_location: POINT,
  current_intent: VARCHAR,
  active_observations: ARRAY,
  learned_context: JSONB
}
```

## Implementation Priorities

### Must Ship (Hackathon MVP)
1. ✅ Leave detection workflow (full)
2. ✅ Item recall workflow (full)
3. ✅ Daily timeline workflow (basic)
4. ✅ Multimodal input (camera + voice)
5. ✅ Cloud Run deployment
6. ✅ Real demo data

### Should Ship (if time)
- Confidence scoring for observations
- Location history visualization
- Learned routine refinement

### Post-Hackathon
- Native app
- True geofencing + location tracking
- Multi-device sync
- Advanced ML patterns

## Code Structure
```
src/
├── agent/
│ ├── init.py
│ ├── engine.py (ADK setup, main agent loop)
│ ├── workflows.py (three main workflows)
│ └── intents.py (intent recognition)
│
├── tools/
│ ├── init.py
│ ├── context_tools.py (observe, store, recall)
│ ├── memory_tools.py (retrieve observations)
│ └── timeline_tools.py (reconstruct day)
│
├── frontend/
│ ├── index.html
│ ├── app.js (main app logic)
│ ├── camera.js (vision input)
│ ├── speech.js (voice input)
│ └── styles.css
│
├── utils/
│ ├── init.py
│ ├── db.py (PostgreSQL connection)
│ ├── cache.py (Redis state)
│ ├── logger.py (structured logging)
│ └── config.py (env vars)
│
└── main.py (entry point, Cloud Run handler)
```

## Deployment

- Google Cloud Run (main agent service)
- Cloud SQL for PostgreSQL
- Cloud Memorystore for Redis
- Cloud Scheduler for async loops (morning/evening nudges)
- Cloud Storage for demo video

## Testing

Before submission:
1. Manually test all 3 workflows
2. Generate real demo data (actually run them 3-4 times)
3. Record unedited demo video
4. Test deployment locally with `gcloud emulator`

## Deliverables for Devpost

1. **Demo video** (3-5 min, unedited)
2. **GitHub repo** with clean code + README + DEPLOYMENT.md
3. **Architecture diagram** (include in submission)
4. **Submission text** (~300 words)

## Notes

- Don't over-engineer for first pass; iterate on feedback
- Real demo data > fabricated data
- Autonomous behavior (scheduled loops) is critical for "agent" claim
- Multimodal UX is explicitly rewarded category; show it off
- State management clarity = judges care about this

## Success Definition

Judges watch 5-min demo and think:
"Oh, that's actually useful. And it's actually autonomous—it does things without being asked."

That's the win.´

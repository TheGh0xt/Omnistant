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

### [To be filled as you build]

---

## Build Status

- [ ] Day 1-2: Agent engine + workflow setup
- [ ] Day 3: Leave detection workflow
- [ ] Day 4: Item recall workflow
- [ ] Day 5: Timeline reconstruction
- [ ] Day 6: Frontend integration
- [ ] Day 7: Cloud Run deployment
- [ ] Day 8: Demo recording
- [ ] Day 9: Devpost submission

---

## Key Insights (for next project)

### What Worked
- [To be filled]

### What Would Change
- [To be filled]

### Lessons Learned
- [To be filled]
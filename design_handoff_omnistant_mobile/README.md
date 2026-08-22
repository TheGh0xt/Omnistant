# Handoff: Omnistant — ADHD-Optimized Mobile Interface

## Overview
A calm, mobile-first redesign of Omnistant: an ambient memory agent that observes what you own, where you go, and what you do, then answers three questions — *what am I forgetting?*, *where is my stuff?*, *what did I do today?* The dense dark memory log is replaced by an interactive **memory globe**, and every workflow is reachable by **voice** or by three large quick-action buttons.

## About the Design Files
`Omnistant Mobile.dc.html` in this bundle is a **design reference created in HTML** — a working prototype that demonstrates the intended look, motion, and behavior. It is **not production code to copy**. The task is to **recreate this design in the target codebase's environment** (React Native, React, SwiftUI, etc.) using that codebase's established components, styling approach, and state patterns. If no codebase exists yet, pick the framework best suited to the product (React Native or Expo is the obvious fit for a phone-first agent app) and implement there.

Open the file in any browser to interact with it. The mocked agent behavior (timers, canned transcripts, canned answers) stands in for real speech recognition, vision, and inference — replace it with real services.

## Fidelity
**High fidelity.** Colors, typography, spacing, radii, shadows, touch-target sizes, animation durations, and easings are all final and specified below. Recreate the UI pixel-for-pixel using the codebase's libraries. Copy is also final unless product wants to revise it.

---

## Global Frame

- Design viewport: **375 × 812** (iPhone-class). Prototype renders the app inside a rounded shell (`border-radius: 40px`) purely for presentation — the real app fills the device.
- Structure, top to bottom: **header (56px, fixed)** → **scrolling main** → **voice bar footer (fixed)**.
- App background `#F8F9FB`. Cards `#FFFFFF`. Page never goes dark; there is **no dark mode** by design (high contrast is fatiguing for the target user).
- Main content column: `padding: 4px 20px 28px`, `display: flex; flex-direction: column; gap: 20px`.
- Safe areas: footer bottom padding should absorb the home indicator inset (prototype uses 22px).

---

## Screens / Views

The app is one scrolling surface whose middle section swaps by workflow. `screen` ∈ `home | leave | recall | timeline | globe`.

### Persistent: Header
- Height 56px, `padding: 0 20px`, background `#F8F9FB`, no bottom border.
- Left: 10px green dot `#34A853` + wordmark "Omnistant", 18px/600.
- Right: agent status, 12px `#7F8C8D`. Values: `Ready` (idle) · `Standing by` (wake word on, idle) · `Listening` · `Thinking` · `Speaking`.
- No hamburger, no nav — deliberately.

### Persistent: Memory Globe card (STAR FEATURE)
White card, `border-radius: 16px`, `padding: 16px`, shadow `0 2px 10px rgba(44,62,80,0.06)`, `gap: 8px`.

- **Card header row**: "What I remember" (16px/600) + right-aligned ghost button `Expand` / `Close` (min-height 36px, `padding: 0 12px`, border `1px solid #E8EAED`, radius 8px, 12px/600, text `#4A90E2`; hover `background #F2F7FD`, `border-color #4A90E2`). Toggles the `globe` screen.
- **Globe**: SVG `300 × 300`, centered, `touch-action: none`.
  - Sphere: circle at (150,150), r = 112, fill = radial gradient (`cx 35% / cy 30%`, `#FFFFFF` → `#EDF2F8`), stroke `#E8EAED` 1px. Three latitude ellipses `rx = 109.8`, `ry = 39.2 / 72.8 / 100.8`, stroke `#DDE4EC` 1px, no fill. Flat lighting — no specular, no 3D shading.
  - Observations are placed by (lat, lon) in degrees and projected:
    `x = cos(lat)·sin(lon + rot)`, `y = sin(lat)`, `z = cos(lat)·cos(lon + rot)`;
    `cx = 150 + x·112`, `cy = 150 − y·112`. Points are sorted by ascending `z` so far points render behind.
  - Point radius `5 + 2.4·z` (+2 when selected); opacity `0.4 + 0.6·(z+1)/2`. Radius transitions 300ms ease.
  - Colors by type — **Location `#4A90E2`, Item `#34A853`, Activity `#BD10E0`**.
  - Highlighted point (the answer to a query) gets a concentric ring, `r + 8`, stroke = type color, 2px, opacity 0.35, animating `softPulse 2s ease-in-out infinite`.
  - **Drag to rotate**: pointer down captures; `rot += Δx · 0.45` degrees. A drag of >3px suppresses the tap-select on release. Vertical axis only — no tumbling, no inertia, and **no idle auto-rotation** (constant motion is the thing being avoided).
  - **Tap a point** → selects it (tap again to deselect).
- **Tooltip**: absolutely positioned over the globe at the selected point, `transform: translate(-50%, -125%)`, `background #2C3E50`, white 12px/1.4 text, `padding: 8px 10px`, radius 8px, `max-width: 220px`, wraps, `pointer-events: none`, fades via `opacity` 300ms ease. Content: `Label • Place • Time` (e.g. "Keys • Home · kitchen counter • 8:42 AM").
  - **Clamping is required**: `left = clamp(cx, 112, 188)px`, `top = max(cy, 44)px`, so the tooltip never spills outside the 375px screen.
- **Legend row**: three 12px `#7F8C8D` labels, each with a 9px dot — Location / Item / Activity. Centered, `gap: 16px`. Color is never the only signal: the list view repeats the type in text.

### Screen: Home
- Workflow block shows title **"Ready to listen"** (20px/600, centered) and subtitle *"Tap one thing below. I'll handle the rest."* (16px, `#7F8C8D`, centered).
- No primary button — the quick actions and the mic are the affordances.

### Screen: Leave detection (`leave`)
- Title **"I'm heading to work"**; subtitle *"Point the camera at your desk and I'll check what's missing."*
- Primary button **Start camera** (see Buttons). Tapping it → scanning state.
- **Scanning card**: white, `1px solid #E8EAED`, radius 12px, `padding: 20px`, centered column, `gap: 12px`. Inside: a 120px-tall preview area, radius 10px, `linear-gradient(180deg,#EEF3F8,#E4EBF3)`, placeholder label "Camera preview" 12px `#7F8C8D` — **replace with the live camera feed**. Below it, "Scanning your desk…" 16px/500 `#4A90E2` animating `softPulse 2s ease-in-out infinite`. Deliberately no red REC dot.
- After **1800ms** (stand-in for inference) → agent response *"You're missing your keys from this morning."*, globe highlights the `keys` point. Details rows: Last seen / Time / Confidence.

### Screen: Item recall (`recall`)
- Title **"Where are my keys?"**; subtitle *"I'll look through what I noticed today."*
- Primary button **Speak now** → immediate response *"Your keys were last seen at home, on the kitchen counter, at 8:42 AM."*, `keys` point highlighted. Details rows: Observation / Location / Confidence 92%.

### Screen: Daily timeline (`timeline`)
- Title **"What did I do today?"**; subtitle *"Five moments. Tap one to see more."*
- **Timeline card**: white, `1px solid #E8EAED`, radius 12px, `padding: 20px 16px`, `gap: 16px`.
  - Track: 56px-tall relative box; rail is absolutely positioned `top: 14px; left/right: 6px; height: 6px; radius 999px; background #E8EAED`.
  - Events are 48×56 buttons at `left: 6% + index·22%`, `translateX(-50%)`: a 16px dot in the type color with `box-shadow: 0 0 0 4px <ring>` (selected `rgba(74,144,226,0.22)`, else `rgba(232,234,237,0.9)`, 300ms ease), and the time beneath in 12px `#7F8C8D`. Each carries `aria-label="<time> <title>"`.
  - Detail block below a `1px solid #E8EAED` divider: title 16px/500 + meta 12px `#7F8C8D`.
- A horizontal visual bar, **not** a list — that was the point of the redesign.

### Screen: Globe details (`globe`)
- Replaces the workflow block. Heading "Everything I've noticed" (18px/600).
- **Filter pills**: All / Location / Item / Activity. min-height 36px, `padding: 0 14px`, `border-radius: 999px`, 12px/600. Selected: `#4A90E2` bg, white text. Unselected: white bg, `#E8EAED` border, `#2C3E50` text. Filtering also filters the globe's points.
- **Observation rows**: full-width buttons, min-height 48px, white, `1px solid #E8EAED`, radius 12px, `padding: 12px 14px`, shadow `0 2px 8px rgba(44,62,80,0.05)`, hover `border-color #4A90E2`. Layout: 10px type dot · (label 16px/500 + meta 12px `#7F8C8D` stacked, 2px gap) · confidence `NN%` 12px `#7F8C8D`. Tapping a row selects that point on the globe.

### Persistent: Agent response (appears when there is an answer)
- White card, radius 16px, `padding: 20px`, shadow `0 2px 12px rgba(44,62,80,0.07)`, `gap: 16px`, `aria-live="polite"`, entrance `riseIn 400ms ease-out`.
- Answer text **24px / line-height 1.5 / weight 500**, `min-height: 72px` so the card doesn't jump as words appear.
- **Word-by-word reveal**: one word every **150ms** (configurable 60–400ms).
- Optional **details panel** (toggled): `#F8F9FB` block, radius 12px, `padding: 14px`, rows of `key` (12px `#7F8C8D`) / `value` (12px/500 `#2C3E50`) spaced apart.
- Buttons row: **Got it** (green `#7ED321`, text `#2C3E50`, flex 1, min-height 48px, radius 12px) → returns home and clears state; **Details / Hide details** (white, `1px solid #E8EAED`, text `#4A90E2`).

### Persistent: Quick actions
- Sits at the bottom of the scroll area (`margin-top: auto`), above the voice bar. A `1px #E8EAED` divider, a 12px `#7F8C8D` "Quick actions" label, then three stacked full-width buttons, `gap: 8px`:
  **"I'm heading out" · "Where is…?" · "What did I do today?"**
- Each: min-height 48px, radius 12px, left-aligned text 16px/600, `padding: 0 16px`, white bg with `#E8EAED` border. Active workflow gets `#F2F7FD` bg, `#4A90E2` border, `#357ABD` text.
- Stacked, not a horizontal scroller — three targets that are all visible beat a row that hides one.

### Persistent: Voice bar (footer)
White, `border-top: 1px solid #E8EAED`, `padding: 14px 20px 22px`, column `gap: 12px`.

- **Mic button**: 56×56 circle, white icon, shadow `0 4px 12px rgba(44,62,80,0.16)`, `active { transform: scale(0.98) }`, 300ms ease.
  - Fill: idle `#4A90E2`, listening `#34A853`, thinking/speaking `#7F8C8D`.
  - **Halo**: an absolutely-positioned sibling circle behind it, filled with the state color at `opacity 0.3`, animating `haloPulse 2s` while listening and `haloPulse 4s` while wake-word standby; hidden (opacity 0) otherwise.
  - `aria-label` "Talk to Omnistant" / "Stop listening", `aria-pressed` = listening.
- **Status line** 12px/600 — `Voice` (`#7F8C8D`) / `Listening — tap to stop` (`#34A853`) / `Thinking…` / `Answering out loud` (`#4A90E2`).
- **Transcript line** 16px/1.5 `#2C3E50`: live transcript while listening; `"…"` quoted back while speaking; otherwise the resting hint — *"Tap to talk. I only listen while you hold the floor."* or, with wake word on, *"Say "Hey Omni", or tap to talk."*
- **Speaking indicator**: three 4×20px `#4A90E2` bars, radius 2px, animating `speakBar 1.2s ease-in-out infinite` at 0 / 0.2s / 0.4s delays. Shown only while the agent is answering.
- **Wake-word toggle** pill: min-height 36px, radius 999px, 9px dot + label. Off (default): white, `#E8EAED` border, `#7F8C8D` text, `#C7CDD4` dot, "Wake word off — tap to talk". On: `#F2F7FD` bg, `#4A90E2` border, `#357ABD` text, `#34A853` dot, "Wake word on — always listening". `aria-pressed` reflects state.
- **Product decision to preserve**: always-listening is **opt-in and always visibly indicated** (halo + header "Standing by"). Default is push-to-talk. Do not ship an invisible hot mic.

---

## Interactions & Behavior

### Voice flow (mocked in the prototype — wire to real ASR/TTS)
1. Tap mic → `voice = listening`, prior response cleared. Prototype fakes a transcript at 220ms/word cycling through "I'm heading to work" → "Where are my keys?" → "What did I do today?". **Replace with streaming speech-to-text results.**
2. 600ms after the utterance ends → `voice = thinking` → 900ms → route to the matching workflow. **Replace with real intent classification.**
3. `leave` via voice skips the camera button and goes straight to scanning (1600ms), then answers.
4. While the answer reveals, `voice = speaking`; back to `idle` on the last word. If you add real TTS, drive the state from playback rather than the reveal timer, and keep the on-screen reveal roughly in sync with the spoken audio.
5. Tapping the mic while listening cancels. Any quick action or "Got it" cancels all timers and resets.

### Motion spec
| Element | Animation | Duration | Easing |
|---|---|---|---|
| Section / response entry | `riseIn` (translateY 28px + fade) | 400ms | ease-out |
| New globe point | `popIn` (scale 0.5→1 + fade) | 500ms | ease-out |
| Scanning label, highlight ring | `softPulse` opacity 0.55↔1 | 2s loop | ease-in-out |
| Mic halo | `haloPulse` scale 1→1.18, opacity 0.35→0.12 | 2s listening / 4s standby | ease-in-out |
| Speaking bars | `speakBar` scaleY 0.4↔1 | 1.2s loop, 0/0.2/0.4s stagger | ease-in-out |
| Button press | `transform: scale(0.98)` | 300ms | ease |
| Tooltip, colors, borders | opacity / color | 300ms | ease |
| Word reveal | one word per tick | 150ms/word | — |

Nothing exceeds 500ms except the intentional slow loops. No strobing, no simultaneous color+scale changes, no autoplay: every animation follows a user action.

### Accessibility (non-negotiable)
- `@media (prefers-reduced-motion: reduce)` disables **all** animation and transition — implement the platform equivalent (`useReducedMotion` / `UIAccessibility.isReduceMotionEnabled`).
- Every touch target ≥ 48×48; ≥ 8px between targets.
- Focus: `outline: 3px solid #4A90E2; outline-offset: 2px`.
- Semantic elements throughout (`header`, `main`, `nav`, `footer`, `button`, `h1`/`h2`/`h3` in order); `aria-label` on every icon-only control; response region is `aria-live="polite"`.
- All text ≥ 12px (meta) / 16px (body); no fixed font scaling — honor system text size.
- Contrast meets WCAG AA. Note: the green "Got it" button uses **dark text on green**, not white, for that reason.

### Responsive (stretch)
At ≥ 800px, two columns: globe (larger, ~400px) left; workflow + response right; quick actions and voice bar remain a single full-width bar. Keep the same generous spacing — do not add density.

---

## State Management

```
screen     : 'home' | 'leave' | 'recall' | 'timeline' | 'globe'
stage      : 'idle' | 'scanning' | 'done'      // workflow progress
voice      : 'idle' | 'listening' | 'thinking' | 'speaking'
wake       : boolean                            // wake word opt-in, default false
heard      : string                             // live transcript
rot        : number                             // globe rotation, degrees
sel        : observationId | null                // tooltip target
highlight  : observationId | null                // answer target (pulsing ring)
resp       : string | null                       // agent answer
reveal     : number                              // words revealed so far
detailRows : {k, v}[]
details    : boolean
filter     : 'all' | 'location' | 'item' | 'activity'
tlSel      : eventId
```

Transitions: quick action / "Got it" → full reset of `stage, resp, reveal, details, highlight, sel, voice, heard` and cancellation of every timer. Guard against leaked intervals on unmount.

**Data the real app must supply**: a stream of observations `{ id, label, place, time, type: location|item|activity, confidence, lat, lon }` — `lat`/`lon` are presentation-only sphere coordinates; derive them deterministically (e.g. hash the id, or cluster by type) so a given memory keeps its place on the globe between sessions. Plus a day timeline `{ id, time, title, meta, type }`.

## Design Tokens

**Color**
| Token | Hex |
|---|---|
| Background | `#F8F9FB` |
| Surface / card | `#FFFFFF` |
| Canvas outside device | `#EDF0F4` |
| Primary action | `#4A90E2` (hover `#3F81CE`, text-on-light `#357ABD`, tint `#F2F7FD`) |
| Secondary action | `#7ED321` (hover `#72C119`) |
| Accent / activity | `#BD10E0` |
| Success / item | `#34A853` |
| Warning | `#FBBC04` |
| Error | `#EA4335` |
| Text | `#2C3E50` |
| Secondary text | `#7F8C8D` |
| Divider / border | `#E8EAED` (globe rings `#DDE4EC`, inactive dot `#C7CDD4`) |

**Type** — Montserrat (400/500/600/700), fallback SF Pro Display, Segoe UI, system-ui. Letter-spacing `+0.3px` globally, line-height 1.5–1.6.
| Role | Size / weight |
|---|---|
| Agent response | 24 / 500 |
| Screen title | 20 / 600 |
| Section heading | 18 / 600 |
| Card heading, body, buttons | 16 / 500–600 |
| Meta, status, legend | 12 / 400–600 |

**Spacing** 4 · 8 · 12 · 14 · 16 · 20 · 28 (px). Section gap 20; card padding 16–20; min 16px between sections.

**Radius** 8 (small controls) · 10 (preview) · 12 (buttons, list rows) · 16 (cards) · 999 (pills) · 40 (device shell).

**Shadow** card `0 2px 10px rgba(44,62,80,0.06)` · response `0 2px 12px rgba(44,62,80,0.07)` · list row `0 2px 8px rgba(44,62,80,0.05)` · primary button `0 4px 12px rgba(74,144,226,0.24)` · mic `0 4px 12px rgba(44,62,80,0.16)`.

**Sizes** touch target ≥ 48 · primary button 56 · mic 56 · small control 36 · header 56 · globe 300.

## Assets
No image or icon assets. The only icon is an inline SVG microphone (24×24 viewBox, `stroke: currentColor`, width 2, round caps: rounded rect 9,2 w6 h12 r3 + arc `M5 11a7 7 0 0 0 14 0` + stem `M12 18v4`) — swap for the codebase's icon set. The globe is generated SVG. Montserrat loads from Google Fonts; bundle it locally in production.

## Screenshots
In `screenshots/`. Captured from the prototype with the phone shell expanded to full content height, so each image shows a whole screen end to end (the real device scrolls).

| File | State |
|---|---|
| `01-home.png` | Home — globe, "Ready to listen", quick actions, voice bar |
| `02-leave-idle.png` | Leave detection before the camera starts |
| `03-leave-scanning.png` | Camera preview + "Scanning your desk…" |
| `04-leave-answer.png` | Answer mid word-by-word reveal, globe ring on the keys point |
| `05-answer-details.png` | Answer with the details panel open |
| `06-recall-answer.png` | Item recall answer |
| `07-timeline.png` | Daily timeline bar |
| `08-timeline-event-selected.png` | Timeline with the 9:20 event selected |
| `09-globe-expanded.png` | Globe details screen, All filter |
| `10-globe-filtered-item.png` | Globe details filtered to Item |
| `11-voice-listening.png` | Voice bar listening, live transcript, green halo |
| `12-voice-answering.png` | Voice bar answering, speaking bars |

## Files
- `Omnistant Mobile.dc.html` — the full interactive prototype (all screens, globe, voice bar). Open directly in a browser.
- `support.js` — runtime needed to render the prototype file locally. Not part of the design; do not port it.

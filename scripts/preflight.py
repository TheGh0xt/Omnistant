"""Prove the autonomous loop works, end to end, before recording.

Every failure in this project so far has been silent: a scan with no frame, a
queue with nothing in it, a brief whose window never opened. None of them raised
an error, and all of them looked fine on screen. This exercises the real
deployed service the way the demo does and fails loudly instead.

    TASK_TOKEN=$(gcloud scheduler jobs describe omnistant-drain-nudges \
        --location=us-central1 --format='value(httpTarget.headers.X-Task-Token)') \
      python scripts/preflight.py

Sends real Slack messages: it is checking that they arrive. Delete them before
you record.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("OMNISTANT_URL", "https://omnistant-3oe5odab6a-uc.a.run.app")
TOKEN = os.getenv("TASK_TOKEN", "")

# A 1x1 JPEG. Vision finds nothing in it, which is the point: every expected
# item comes back missing, so the reminder path is exercised in full.
FRAME = "data:image/jpeg;base64," + (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iii"
    "gD//2Q=="
)

PASSED: list[str] = []
FAILED: list[str] = []


def _call(path: str, body: dict | None = None, token: bool = False) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Task-Token"] = TOKEN
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    print(f"\nOmnistant preflight against {BASE}\n")

    print("service")
    try:
        # NOT /healthz: Google's frontend intercepts that path before it
        # reaches the container and answers its own 404.
        health = _call("/health")
    except urllib.error.URLError as exc:
        check("service answers", False, str(exc))
        return 1
    # `postgres` and `redis`, not `database`/`cache`: those keys have never
    # existed in /health's response, so this line reported `db=None cache=None`
    # on a perfectly healthy service and would have said exactly the same thing
    # on a broken one.
    status = health.get("status")
    check(
        "service answers",
        status == "ok",
        f"status={status} postgres={health.get('postgres')} redis={health.get('redis')}",
    )
    # Degraded is a real answer, not a failure to answer — the whole design is
    # that it keeps serving on in-memory fallbacks. Worth stopping for anyway:
    # everything below writes through those subsystems.
    if status != "ok":
        print("    /health reports a degraded subsystem; the checks below may not mean what they say.")

    print("\nleave scan -> reminder -> Slack")
    session = str(uuid.uuid4())
    # The watch loop is what holds a frame in the real app; drive it the same way.
    tick = _call("/api/observe", {"session_id": session, "frame": FRAME})
    check("watch loop accepts a frame", bool(tick.get("available")), tick.get("note", ""))

    # Deliberately NO frame on this turn: it must come from the cache the watch
    # loop just filled. This is the exact path that silently queued nothing.
    reply = _call("/api/chat", {"text": "I'm heading to work", "session_id": session})
    scan = next(
        (w for w in reply.get("workflow_results", []) if w.get("workflow") == "leave_detection"),
        None,
    )
    if not check("the leave scan ran", scan is not None):
        return 1
    seeing = bool(scan.get("vision_available"))
    check("the scan could see", seeing, scan.get("note") or "no frame reached it")
    missing = [m["item"] for m in scan.get("missing_items", [])]
    carried = [c["item"] for c in scan.get("carried_items", [])]
    check("it found something to warn about", bool(missing), f"missing={missing} carried={carried}")

    if missing:
        print("  ...waiting for the scheduled drain to deliver it")
        delivered = False
        for _ in range(24):          # up to ~2 minutes
            time.sleep(5)
            drain = _call("/api/tasks/drain-nudges", {}, token=True) if TOKEN else {}
            if drain.get("sent"):
                delivered = True
                break
        check("Slack received the reminder", delivered,
              "" if delivered else "nothing drained — check SLACK_WEBHOOK_URL and the drain job")

    print("\nautonomous brief")
    if not TOKEN:
        print("  SKIP  no TASK_TOKEN set (see this file's docstring)")
    else:
        natural = _call("/api/tasks/morning-brief", {}, token=True)
        if natural.get("triggered"):
            check("the brief fired on its own", bool(natural.get("delivered")), str(natural))
        else:
            # Not a failure by itself: outside the departure window is correct
            # behaviour. Report it, then prove the delivery path still works.
            print(f"  INFO  not in the window right now — {natural.get('skipped')} "
                  f"{natural.get('window', '')} now={natural.get('now', '')}")
            forced = _call("/api/tasks/morning-brief?force=true", {}, token=True)
            check("the brief delivers when forced", bool(forced.get("delivered")), str(forced)[:160])

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

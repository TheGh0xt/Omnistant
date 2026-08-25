"""One place where notifications are worded.

The daily recap used to arrive as a paragraph of model prose:

    "Your observations for the day are extremely sparse, covering only a single
    minute. At 11:09 AM, you had an airpod in your ear, held an airpods case in
    your right hand, held up a phone connected to a charging cable in your left
    hand, and a laptop showing its keyboard under red light left your view."

Every fact in that is correct. It is still the wrong output: it opens by
apologising, it chains four clauses into one sentence, and the timestamps are
buried mid-clause. For the person this is built for — someone who is being
reminded of things precisely because holding them in working memory is the hard
part — a dense paragraph is the shape most likely to go unread.

So: emoji anchor, one idea per line, time first, short phrases, meta in italics
at the bottom where it does not compete. The rules are applied here rather than
in each workflow so the three notification types cannot drift apart, and so
changing the house style is one edit rather than three.

The bullets are built from the observation log directly, not narrated by a
model. That is the project's standing rule — the model does the seeing and the
wording of *speech*, never the sourcing of a fact — and it has a second benefit
here: no model call means the evening recap compiles in milliseconds, which is
what makes it fit inside a demo-length window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# Rule 4: past roughly this many words a bullet stops being scannable and
# becomes a sentence again. Lists are truncated with a count rather than wrapped.
MAX_NAMES_PER_BULLET = 3

# Below this, a day is thin enough to say so — but briefly, and without the
# apologetic register that reads like an error message.
SPARSE_BELOW = 4


@dataclass
class Line:
    """One observation, already formatted for display by the caller."""

    time_str: str
    kind: str
    subject: str
    detail: str = ""


@dataclass
class Block:
    """A channel-independent notification body in the house style."""

    emoji: str
    header: str
    subheader: str | None = None
    where: str | None = None
    section: str | None = None
    bullets: list[str] = field(default_factory=list)
    footer: str | None = None

    def to_mrkdwn(self) -> str:
        """Slack mrkdwn: single asterisks bold, underscores italic, • bullets."""
        parts: list[str] = [f"{self.emoji} *{self.header}*" + (f" — {self.subheader}" if self.subheader else "")]
        if self.where:
            parts.append(f"📍 *Where:* {self.where}")
        if self.bullets:
            body = [f"*{self.section}*"] if self.section else []
            body.extend(f"• {b}" for b in self.bullets)
            parts.append("\n".join(body))
        if self.footer:
            parts.append(f"_{self.footer}_")
        return "\n\n".join(parts)

    def to_plain(self) -> str:
        """Fallback for clients that cannot render mrkdwn, and for the log."""
        parts = [self.header + (f" — {self.subheader}" if self.subheader else "")]
        if self.where:
            parts.append(f"Where: {self.where}")
        parts.extend(f"- {b}" for b in self.bullets)
        if self.footer:
            parts.append(self.footer)
        return "\n".join(parts)


def _names(items: Sequence[str]) -> str:
    """Join a list without letting it run past the width a bullet can carry."""
    unique = list(dict.fromkeys(n for n in items if n))
    if not unique:
        return ""
    if len(unique) <= MAX_NAMES_PER_BULLET:
        return ", ".join(unique)
    shown = ", ".join(unique[:MAX_NAMES_PER_BULLET])
    return f"{shown} +{len(unique) - MAX_NAMES_PER_BULLET} more"


def bullets_from_lines(lines: Iterable[Line]) -> list[str]:
    """Collapse a day's observations into time-first, one-idea-per-line bullets.

    Grouped by timestamp and by what happened, because five items seen in the
    same second is one moment, not five. Rule 5 — no compound sentences — is why
    "saw" and "out of view" become separate bullets at the same time rather than
    one clause-chained line.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for line in lines:
        subject = (line.subject or "").strip()
        if not subject:
            continue
        # "phone left view" is an activity describing an item; strip the suffix
        # so the bullet can group them and say it once.
        if subject.endswith(" left view"):
            bucket, name = "gone", subject[: -len(" left view")]
        elif line.kind == "item":
            bucket, name = "seen", subject
        else:
            bucket, name = "did", subject
        key = (line.time_str, bucket)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(name)

    out: list[str] = []
    for time_str, bucket in order:
        names = _names(grouped[(time_str, bucket)])
        if not names:
            continue
        if bucket == "seen":
            phrase = f"Saw {names}"
        elif bucket == "gone":
            phrase = f"{names} out of view"
        else:
            phrase = names
        out.append(f"{time_str} — {_sentence(phrase)}")
    return out


def _sentence(text: str) -> str:
    """Capitalise a bullet without touching a name the user cased themselves."""
    return text[0].upper() + text[1:] if text and text[0].islower() else text


def _count_footer(count: int) -> str:
    if count <= 0:
        return "Nothing noticed today"
    if count < SPARSE_BELOW:
        return "Only a few things noticed today"
    return f"{count} things noticed today"


def daily_recap(*, day_label: str, where: str | None, lines: Sequence[Line], count: int) -> Block:
    """The evening summary. Issue 3's worked example."""
    return Block(
        emoji="🧠",
        header="Your Day",
        subheader=day_label,
        where=where,
        section="Timeline",
        bullets=bullets_from_lines(lines),
        footer=_count_footer(count),
    )


def leaving_without(*, destination: str, missing: Sequence[str], last_seen: Sequence[str], when: str) -> Block:
    """The reminder that fires after you have actually walked out."""
    bullets = [f"Missing: {_names(list(missing))}"]
    bullets.extend(last_seen)
    return Block(
        emoji="🚪",
        header="You left without something",
        where=_sentence(destination),
        bullets=bullets,
        footer=f"Left {when} — still close enough to turn back?",
    )


def item_status(*, destination: str, unverified: Sequence[str], expected: Sequence[str]) -> Block:
    """The pre-departure brief: what the agent can and cannot vouch for."""
    if unverified:
        bullets = [f"Can't vouch for: {_names(list(unverified))}"]
        emoji, header = "🔍", "Before you leave"
    else:
        bullets = ["Everything you normally take was seen recently"]
        emoji, header = "✅", "You're good to go"
    return Block(
        emoji=emoji,
        header=header,
        where=_sentence(destination),
        bullets=bullets,
        footer=f"Usually taken: {', '.join(expected)}" if expected else None,
    )

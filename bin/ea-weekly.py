#!/usr/bin/env python3
"""Sunday review, pre-drafted.

The point is to turn the weekly review from something you produce into something
you edit -- producing from a blank page is the step that does not happen. All the
facts come from the state store and the week's daily notes; the LLM only writes
prose around numbers it is given, and is told not to invent encouragement.

Writes '00 📅 Daily/Reviews/YYYY-Www.md' and sends a short summary.
"""
import re
import sys
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import brief, config, llm, telegram, vault
from ea.store import TaskStore, tracking_since

SYSTEM = """You draft a weekly review of someone's to-do list, \
from data they give you. Rules:

- Use only the facts supplied. Never invent a task, a number or an event.
- No praise, no encouragement, no "great job". Flat and factual.
- Name one pattern you can actually see in the data, and say what it suggests. \
If the data is too thin to support a pattern, say that instead of guessing.
- If the tracking window is shorter than the review period, the low counts are an \
artefact of when tracking started, NOT evidence about the reader's week. Say so in \
one line and do not characterise how their week went. Never write anything that \
reads as "you did nothing".
- Propose exactly one change for next week, concrete and small.
- Four short sections: What closed. What didn't. The pattern. One change.
- Under 320 words. Markdown, no top-level heading."""

CAPTURE_LINE = re.compile(r"^\s*-\s+(?:🎙 |⏱ )?(.+?)(?:\s+_\(\d{2}:\d{2}\)_)?\s*$")


def section_lines(text, header):
    """Pull the bullet lines out of one '## Header' section."""
    lines, out, inside = text.splitlines(), [], False
    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            inside = s == header
            continue
        if inside and s.startswith("-"):
            m = CAPTURE_LINE.match(ln)
            if m:
                out.append(m.group(1).strip())
    return out


def main():
    store = TaskStore()
    snap = brief.snapshot(store=store)
    now = snap["when"]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=6)

    closed = store.completed_between(start, now)
    captures, logs, shutdowns = [], [], []
    for day, path in vault.recent_dailies(7):
        text = vault.read(path)
        captures += [(day, c) for c in section_lines(text, "## Captured")]
        logs += [(day, c) for c in section_lines(text, "## Log")]
        if "### Shutdown" in text:
            shutdowns.append(day)

    focus_runs = [c for _d, c in logs if "focus" in c.lower()]
    iso = now.isocalendar()
    slug = f"{iso.year}-W{iso.week:02d}"

    since = tracking_since()
    days_tracked = max(0, (now - since).days)
    partial = days_tracked < 7

    facts = [
        f"Week: {start.date()} to {now.date()} ({slug})",
        f"Completion tracking began {since.date()}, {days_tracked} full day(s) ago."
        + (" THIS IS A PARTIAL WINDOW: it is shorter than the review period, so the"
           " counts below undercount real activity. Tasks finished before tracking"
           " began carry no timestamp and are not counted." if partial else ""),
        f"Tasks closed this week: {len(closed)}",
        *[f"  - {rec.get('text','?')}" for _k, rec in closed[:25]],
        f"Still open on to-do lists: {snap['open_count']} (plus {snap['plan_count']}"
        f" unticked checklist items in project plans, which are not nagged about)",
        f"Items now stale (7+ days untouched): {len(snap['stale'])}",
        f"Items waiting on someone: {len(snap['waiting'])}",
        f"Overdue: {len(snap['overdue'])}",
        f"Things captured to the bot this week: {len(captures)}",
        *[f"  - {c}" for _d, c in captures[:20]],
        f"Focus sessions logged: {len(focus_runs)}",
        f"Evenings closed out with a shutdown note: {len(shutdowns)} of 7",
        "Oldest open items:",
        *[f"  - {t.text} ({brief.age_str(store, r)})" for t, r in snap["stale"][:8]],
    ]
    if snap["frogs"]:
        facts.append(f"Current frog: {snap['frogs'][0][0].text}")

    body, err = (None, "no LLM key")
    if llm.available():
        body, err = llm.ask("\n".join(facts), system=SYSTEM, max_tokens=900,
                            temperature=0.35)
    if err:
        # A failed model call must not cost the week's data: ship the facts.
        body = ("_Draft unavailable (" + str(err) + "). Raw figures:_\n\n"
                + "\n".join(f"- {f.strip()}" for f in facts))

    config.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    note = config.REVIEW_DIR / f"{slug}.md"
    vault.write(note, f"# Week {slug}\n\n"
                      f"_{start.date()} to {now.date()}. Drafted by exec-assist — "
                      f"edit freely, this is a starting point, not a record._\n\n"
                      f"{body}\n\n---\n\n## Figures\n\n"
                      + "\n".join(f"- {f.strip()}" for f in facts) + "\n")

    note_partial = (f"\n\n_First {days_tracked} day(s) of tracking — these counts "
                    f"undercount the week._" if partial else "")
    telegram.send(
        f"📋 **Week {slug} review drafted**\n\n"
        f"- closed: **{len(closed)}**\n"
        f"- still open: {snap['open_count']} ({len(snap['stale'])} stale)\n"
        f"- captured: {len(captures)}\n"
        f"- focus sessions: {len(focus_runs)}\n"
        f"- evenings logged: {len(shutdowns)}/7\n\n"
        f"Draft is in `{vault.rel(note)}` — it's a draft, edit it."
        + note_partial)
    print(f"wrote {vault.rel(note)} ({len(closed)} closed, {len(captures)} captures)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

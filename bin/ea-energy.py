#!/usr/bin/env python3
"""Passive energy and rhythm log.

Replaces the manual attempt in the vault's 'Energy/' folder, which holds three
notes called Untitled -- manual state tracking is the first thing to go. Every
number here is derived from activity that already happens: when tasks get ticked,
when things get captured, when focus sessions run.

Reports sample size honestly. Completion timestamps only start accumulating from
the day this tooling was installed, so early runs will say so rather than
claiming a pattern from four data points.
"""
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea import config, vault
from ea.store import TaskStore

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_IN_LINE = re.compile(r"_\((\d{2}):(\d{2})\)_")
MIN_SAMPLE = 12          # below this, report counts but claim no pattern


def band(hour):
    if hour < 6:
        return "night (00-06)"
    if hour < 12:
        return "morning (06-12)"
    if hour < 17:
        return "afternoon (12-17)"
    if hour < 21:
        return "evening (17-21)"
    return "late (21-24)"


def main():
    store = TaskStore()
    store.sync()
    store.commit()

    by_day, by_band, by_hour = Counter(), Counter(), Counter()
    completions = 0
    for rec in store.tasks.values():
        at = rec.get("done_at")
        if not at:
            continue
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            continue
        completions += 1
        by_day[DAYS[when.weekday()]] += 1
        by_band[band(when.hour)] += 1
        by_hour[when.hour] += 1

    cap_day, cap_band = Counter(), Counter()
    captures = focus = 0
    for day, path in vault.recent_dailies(60):
        text = vault.read(path)
        for ln in text.splitlines():
            m = TIME_IN_LINE.search(ln)
            if not m:
                continue
            hour = int(m.group(1))
            if "focus" in ln.lower():
                focus += 1
                continue
            captures += 1
            cap_day[DAYS[day.weekday()]] += 1
            cap_band[band(hour)] += 1

    L = [
        "# Energy & rhythm",
        "",
        "> [!info] Derived automatically by exec-assist from when things actually "
        "get ticked, captured and focused on. Overwritten on each run — nothing "
        "here is hand-entered, so it cannot fall behind.",
        "",
        f"_Updated {vault.now().strftime('%Y-%m-%d %H:%M %Z')}._",
        "",
        "## Sample",
        "",
        f"- Task completions with a timestamp: **{completions}**",
        f"- Captures with a timestamp: **{captures}**",
        f"- Focus sessions logged: **{focus}**",
        "",
    ]

    total = completions + captures
    if total < MIN_SAMPLE:
        L += [
            "## Pattern",
            "",
            f"Not enough data yet — {total} timestamped events, and a claim about "
            f"when you work best needs at least {MIN_SAMPLE}. Completion times only "
            "start accruing from when this tooling was installed, so this fills in "
            "over the next couple of weeks on its own.",
            "",
        ]
    else:
        L += ["## When you close things", ""]
        for dayname, n in by_day.most_common():
            L.append(f"- {dayname}: {n}")
        L += ["", "## Time of day", ""]
        for b, n in by_band.most_common():
            L.append(f"- {b}: {n}")
        best_day = by_day.most_common(1)[0][0] if by_day else None
        best_band = by_band.most_common(1)[0][0] if by_band else None
        L += ["", "## Read", ""]
        if best_day and best_band:
            L.append(f"Most things close on **{best_day}**, in the **{best_band}** "
                     f"band. If a task needs real focus, that is the slot to defend.")
        if cap_band:
            noisy = cap_band.most_common(1)[0]
            L.append(f"Most capture happens in the **{noisy[0]}** band "
                     f"({noisy[1]} items) — that is when things are landing in your "
                     f"head, not when you are acting on them.")
        L.append("")

    config.VAULT.joinpath("Energy").mkdir(parents=True, exist_ok=True)
    path = vault.write(config.VAULT / "Energy" / "Patterns.md", "\n".join(L) + "\n")
    print(f"wrote {vault.rel(path)}: {completions} completions, {captures} captures, "
          f"{focus} focus sessions")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

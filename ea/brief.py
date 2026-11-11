"""Build the deterministic daily snapshot.

Two consumers, one computation:

  * bin/ea-snapshot.py writes this into the vault as '00 📅 Daily/_Today.md', so
    the LibreChat Morning Brief agent can read it over the obsidian MCP. The
    agent cannot compute task ages itself -- those live in the state store -- so
    handing it a rendered note is what lets the 07:00 brief talk about staleness.
  * ea-bot.py serves it instantly for /today and /brief, with no LLM call.

Nothing here calls an LLM: the numbers must be the same every time they are
asked for.
"""
import re
from datetime import timedelta

from . import config, vault
from .store import TaskStore

# Buckets that should not be offered as "the work for today" -- they are real
# tasks, but surfacing "peanut butter" next to a deep-work item trains you to
# ignore the list.
ERRAND_HINTS = ("shopping", "errand", "procurement", "groceries")
STALE_AFTER_DAYS = 7
WAIT_CHASE_DAYS = 5

# Backfilled ages bottom out at the oldest vault archive, so a large share of the
# pile shares one age and sorting by age alone makes the shortlist an arbitrary
# tiebreak. These weights make the choice defensible instead: things with a
# deadline or a cost of inaction outrank things that are merely old, and vague
# "look into X" items -- which cannot be started in one sitting anyway -- sink.
# Matched as word-prefixes, not substrings: plain `in` made "Facebook" match
# "book" and "generic scripts" match a prescription "script".
CONSEQUENCE = (
    "pay", "bill", "levy", "tax", "invoice", "refund", "return", "claim",
    "renew", "licen", "expir", "deadline", "submit", "apply", "form",
    "appointment", "book", "refill", "prescription", "doctor", "dentist",
    "insurance", "customs", "fine", "registration", "membership",
    "subscription", "overdue", "cancel",
)
VAGUE = (
    "look into", "maybe", "someday", "research", "explore", "consider",
    "think about", "optimi", "refactor", "tinker", "read up", "investigate",
)

_CONSEQUENCE_RE = re.compile(r"\b(?:" + "|".join(CONSEQUENCE) + r")", re.I)
_VAGUE_RE = re.compile(r"\b(?:" + "|".join(VAGUE) + r")", re.I)


def score(store, task, rec):
    """Rank an open item by how much it costs to keep not doing it."""
    text = task.text or ""
    val = float(store.age_days(rec))
    if _CONSEQUENCE_RE.search(text):
        val += 12
    if _VAGUE_RE.search(text):
        val -= 10
    if task.frog:
        val += 20
    if task.due:
        val += 25
    # A parent item with sub-items is a project, not a next action.
    if task.indent == 0 and len(text.split()) <= 2:
        val -= 3
    return val


def age_str(store, rec):
    """Human age that never overstates precision.

    Items dated from the oldest available vault archive carry floor=True: we
    know they are at least that old, not exactly that old.
    """
    days = store.age_days(rec)
    if days <= 0:
        return "today"
    suffix = "+" if rec.get("floor") else ""
    return f"{days}{suffix}d"


def _is_errand(rec):
    hay = " ".join(str(rec.get(k) or "").lower() for k in ("domain", "section", "where"))
    return any(h in hay for h in ERRAND_HINTS)


def snapshot(store=None, when=None):
    """Compute today's state. Returns a dict of plain data."""
    store = store or TaskStore()
    when = when or vault.now()
    today = when.date()
    live = store.sync(when=when)
    store.commit()

    # Due dates and [WAIT]/[FROG]/[LOW] tags are set on purpose, so they count
    # wherever they are written. Age alone only counts on a to-do list: project
    # plans are mostly unticked by design and would otherwise fill the stale pile.
    open_items = [(t, r) for t, r in live if t.open]
    todo_items = [(t, r) for t, r in open_items if vault.is_todo_list(t.file)]

    overdue, due_today, due_soon = [], [], []
    for t, r in open_items:
        if not t.due:
            continue
        if t.due < today:
            overdue.append((t, r))
        elif t.due == today:
            due_today.append((t, r))
        elif t.due <= today + timedelta(days=3):
            due_soon.append((t, r))

    frogs = [(t, r) for t, r in open_items if t.frog]
    waiting = [
        (t, r) for t, r in open_items
        if t.waiting and store.age_days(r) >= WAIT_CHASE_DAYS
    ]
    stale = sorted(
        (
            (t, r) for t, r in todo_items
            if store.age_days(r) >= STALE_AFTER_DAYS
            and not t.waiting
            and not _is_errand(r)
        ),
        key=lambda tr: score(store, *tr),
        reverse=True,
    )
    low_energy = [(t, r) for t, r in open_items if any(tag in ("LOW", "LOW ENERGY") for tag in t.tags)]

    yesterday = today - timedelta(days=1)
    closed_yesterday = store.completed_between(
        when.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1),
        when.replace(hour=0, minute=0, second=0, microsecond=0),
    )

    return {
        "when": when,
        "today": today,
        "open_count": len(todo_items),
        "plan_count": len(open_items) - len(todo_items),
        "overdue": overdue,
        "due_today": due_today,
        "due_soon": due_soon,
        "frogs": frogs,
        "waiting": waiting,
        "stale": stale,
        "low_energy": low_energy,
        "closed_yesterday": closed_yesterday,
        "three": pick_three(store, todo_items, overdue, due_today, frogs, stale),
        "store": store,
    }


def pick_three(store, todo_items, overdue, due_today, frogs, stale):
    """Choose at most three items, with the reason each was chosen.

    Deliberately opinionated: the point of a shortlist is to remove the choice,
    so this returns a decision rather than a ranked menu.
    """
    chosen, seen = [], set()

    def take(items, why, limit=3):
        for t, r in items:
            if len(chosen) >= 3:
                return
            if t.key in seen:
                continue
            seen.add(t.key)
            chosen.append({"task": t, "rec": r, "why": why})
            limit -= 1
            if limit <= 0:
                return

    take(overdue, "overdue", 2)
    take(due_today, "due today", 2)
    take(frogs, "the frog", 1)
    take(stale, "stale, has a cost", 3)
    if len(chosen) < 3:
        rest = sorted(
            ((t, r) for t, r in todo_items if not _is_errand(r)),
            key=lambda tr: score(store, *tr), reverse=True,
        )
        take(rest, "next up", 3)
    return chosen


def render_markdown(snap):
    """The '_Today.md' note the Morning Brief agent reads."""
    store = snap["store"]
    L = [
        f"# Today — {snap['today'].strftime('%A %d %B %Y')}",
        "",
        "> [!info] Generated by exec-assist. Overwritten every morning; edit "
        "TODO.md or the daily note instead, not this file.",
        "",
        f"**Open to-dos:** {snap['open_count']} "
        f"_(plus {snap['plan_count']} unticked in project plans, not counted as stale)_",
        "",
        "## Suggested three",
        "",
    ]
    if snap["three"]:
        for i, pick in enumerate(snap["three"], 1):
            t, r = pick["task"], pick["rec"]
            L.append(f"{i}. **{t.text}** — _{pick['why']}_, open {age_str(store, r)}"
                     f"{f', {t.domain}' if t.domain else ''}")
    else:
        L.append("_Nothing open. Genuinely nothing._")
    L.append("")

    def section(title, items, fmt):
        if not items:
            return
        L.append(f"## {title}")
        L.append("")
        for t, r in items:
            L.append(fmt(t, r))
        L.append("")

    section("Overdue", snap["overdue"],
            lambda t, r: f"- **{t.text}** — due {t.due.isoformat()}")
    section("Due today", snap["due_today"],
            lambda t, r: f"- **{t.text}**")
    section("Due within 3 days", snap["due_soon"],
            lambda t, r: f"- {t.text} — due {t.due.isoformat()}")
    section("Waiting on someone", snap["waiting"],
            lambda t, r: f"- {t.text} — waiting {age_str(store, r)}")
    section("Going stale", snap["stale"][:8],
            lambda t, r: f"- {t.text} — open {age_str(store, r)}"
                         f"{f' ({t.domain})' if t.domain else ''}")
    section("Low-energy options", snap["low_energy"][:5],
            lambda t, r: f"- {t.text}")

    if snap["closed_yesterday"]:
        L.append("## Closed yesterday")
        L.append("")
        for _key, rec in snap["closed_yesterday"]:
            L.append(f"- {rec.get('text', '?')}")
        L.append("")

    L.append(f"_Snapshot taken {snap['when'].strftime('%Y-%m-%d %H:%M %Z')}._")
    return "\n".join(L) + "\n"


def render_telegram(snap, greeting=None, commands=True):
    """Compact form for a chat reply.

    `commands=False` drops the slash-command hints, which only exist on the
    dedicated Telegram bot -- the MCP server is driven by Hermes, where those
    commands do not exist and advertising them just misleads the model.
    """
    store = snap["store"]
    L = []
    if greeting:
        L.append(f"**{greeting}**")
        L.append("")
    if snap["overdue"]:
        L.append(f"⚠️ **{len(snap['overdue'])} overdue**")
        for t, r in snap["overdue"][:4]:
            L.append(f"- {t.text} (due {t.due.isoformat()})")
        L.append("")
    L.append("**Your three:**")
    if snap["three"]:
        for i, pick in enumerate(snap["three"], 1):
            t, r = pick["task"], pick["rec"]
            L.append(f"{i}. {t.text} — _{pick['why']}, {age_str(store, r)}_")
    else:
        L.append("_nothing open_")
    if snap["waiting"]:
        L.append("")
        L.append("**Waiting on:**")
        for t, r in snap["waiting"][:4]:
            L.append(f"- {t.text} — {age_str(store, r)}")
    if snap["low_energy"]:
        L.append("")
        L.append(f"_Low-energy option: {snap['low_energy'][0][0].text}_")
    L.append("")
    if commands:
        L.append(f"_{snap['open_count']} open to-dos. /stale for the pile, "
                 f"/waiting to chase._")
    else:
        L.append(f"_{snap['open_count']} open to-dos (plus {snap['plan_count']} "
                 f"in project plans)._")
    return "\n".join(L)

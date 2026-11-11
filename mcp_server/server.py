"""exec-assist as an MCP server, for the Hermes agent.

Hermes drives the conversation; this exposes the parts an LLM cannot do itself.
Task ages, the shortlist scoring and the nag cooldowns are computed from a state
store on disk -- asking a model "what's gone stale?" cannot work.

Everything here is deterministic. No LLM calls, no network.
"""
import os
import sys

sys.path.insert(0, os.environ.get("EA_ENGINE_PATH", "/app/exec-assist"))

from fastmcp import FastMCP

from ea import brief, config, evening, tasks as tasklib, vault
from ea.match import best_match, retag_frog, tick_all
from ea.store import TaskStore

mcp = FastMCP("Exec_Assist")
MAX_LIST = 40


def _live():
    store = TaskStore()
    live = store.sync()
    store.commit()
    return store, live


def _age(store, rec):
    return brief.age_str(store, rec)


@mcp.tool()
def today() -> str:
    """The user's shortlist for today: up to three suggested items with the
    reason each was chosen, plus anything overdue or being waited on.

    Ages like "9+d" are a floor -- the item is AT LEAST that old, because the
    age was recovered from the oldest available vault backup. Never round "9+d"
    down to "9 days"; say "at least 9 days" or keep the "+".

    Read-only. Call this when asked what to do, what's on, or how things stand.
    """
    snap = brief.snapshot()
    return brief.render_telegram(snap, commands=False)


@mcp.tool()
def capture(text: str) -> str:
    """Write a thought into today's daily note, VERBATIM.

    Pass the user's own words exactly as they said them. Do NOT summarise,
    tidy, correct, rephrase, expand or translate. Capture exists so a thought
    leaves their head intact and can be sorted later; a paraphrase loses the
    detail that made it worth capturing. If they said "chase the ER claim thing
    before fri", write that, not "Follow up on emergency room claim by Friday".

    Use this for thoughts, notes and observations. For something that needs
    doing, use add_task instead.
    """
    text = (text or "").strip()
    if not text:
        return "Nothing to capture: empty text."
    path = vault.ensure_daily()
    stamp = vault.now().strftime("%H:%M")
    vault.append(path, f"- {text}  _({stamp})_", header="## Captured")
    return f"Captured verbatim in {vault.rel(path)}."


@mcp.tool()
def add_task(text: str, due: str = "") -> str:
    """Add an open to-do item to today's daily note.

    Keep the user's wording. `due` is optional and must be ISO format
    (YYYY-MM-DD); it is written as a "📅 YYYY-MM-DD" tag, which the overdue
    detection reads. Set a due date whenever the user names or implies one --
    almost nothing in this vault has one yet, and deadlines are the single most
    useful thing to record here.
    """
    text = (text or "").strip()
    if not text:
        return "Nothing to add: empty text."
    if due:
        try:
            from datetime import date
            date.fromisoformat(due)
            text = f"{text} 📅 {due}"
        except ValueError:
            return f"Refusing to add: '{due}' is not a valid YYYY-MM-DD date."
    path = vault.ensure_daily()
    vault.append(path, f"- [ ] {text}", header="## TODO")
    return f"Added to {vault.rel(path)}: {text}"


@mcp.tool()
def complete(query: str) -> str:
    """Tick a task off, matching on a few words of its text.

    Ticks every copy of the task across the vault, because the same item is
    often written in more than one note and leaving one copy open makes it come
    straight back.

    If the query is ambiguous this returns the candidates instead of guessing --
    show them to the user and ask which one, then call again with more of the
    wording. Do not pick for them.
    """
    store, live = _live()
    task, _rec, confident, matches = best_match(live, query)
    if not matches:
        return (f"No open task matches '{query}'. If they finished something not "
                f"on the list, use capture() to record it instead.")
    if not confident:
        lines = [f"'{query}' is ambiguous -- {len(matches)} possible matches. "
                 f"Ask which one, then call complete() again with more wording:"]
        for _s, t, r in matches[:6]:
            lines.append(f"  - {t.text}  ({t.where}, open {_age(store, r)})")
        return "\n".join(lines)
    where = tick_all(task)
    if not where:
        return f"Found '{task.text}' but could not rewrite the line. Check {task.where}."
    store, _ = _live()
    extra = f" and {len(where) - 1} duplicate copy/copies" if len(where) > 1 else ""
    return f"Ticked '{task.text}' in {where[0]}{extra}."


@mcp.tool()
def close_task(task_id: str, how: str = "done") -> str:
    """Close a task by its id. This backs the Done / Drop buttons on the
    Telegram nags, which carry an id rather than the wording.

    Not for conversation: when the user says in words that something is done,
    use complete() with a few words of it instead.

    `how` is "done" (ticks [x]) or "drop" (marks [-], cancelled: it no longer
    matters, and it is not counted as an accomplishment).
    """
    if how not in ("done", "drop"):
        return f"Unknown how='{how}': use 'done' or 'drop'."
    store, live = _live()
    task = next((t for t, _r in live if t.key == task_id), None)
    if task is None:
        rec = store.tasks.get(task_id)
        if rec:
            return f"No longer open in the vault: {rec.get('text', task_id)}"
        return f"No task with id {task_id}."
    if not task.open:
        return f"Already closed: {task.text}"
    where = tick_all(task, "x" if how == "done" else "-")
    if not where:
        return f"Found '{task.text}' but could not rewrite the line. Check {task.where}."
    _live()
    verb = "Ticked" if how == "done" else "Dropped"
    extra = f" (+{len(where) - 1} copies)" if len(where) > 1 else ""
    return f"{verb}: {task.text}{extra}"


@mcp.tool()
def stale(limit: int = 10) -> str:
    """Open items untouched for 7+ days, ordered by what it costs to keep
    ignoring them (deadlines and money rank above merely-old; vague
    "look into X" items rank last).

    There are usually a lot. Show a handful, not the whole list -- a list this
    long is wallpaper and gets ignored. Ages ending in "+" are floors.
    """
    snap = brief.snapshot()
    store = snap["store"]
    items = snap["stale"][:max(1, min(MAX_LIST, limit))]
    if not items:
        return "Nothing has gone stale."
    head = f"{len(snap['stale'])} items are stale. The {len(items)} costliest:"
    body = [f"  - {t.text}  (open {_age(store, r)}"
            f"{', ' + t.domain if t.domain else ''})" for t, r in items]
    return "\n".join([head] + body)


@mcp.tool()
def waiting() -> str:
    """Items blocked on someone else -- tagged [WAIT] in the vault -- with how
    long they have been sitting. Anything past a week is usually worth a nudge
    rather than more waiting.
    """
    store, live = _live()
    items = [(t, r) for t, r in live if t.open and t.waiting]
    if not items:
        return ("Nothing is tagged [WAIT]. Tagging an item [WAIT] in TODO.md "
                "makes it tracked here.")
    items.sort(key=lambda tr: store.age_days(tr[1]), reverse=True)
    return "\n".join(f"  - {t.text}  (waiting {_age(store, r)}"
                     f"{', ' + t.domain if t.domain else ''})" for t, r in items)


@mcp.tool()
def frog(text: str = "") -> str:
    """Get or set "the frog" -- the single most aversive task, the one being
    avoided. Called with no argument it reports the current frog. Called with
    text it moves the tag to that task, untagging whatever held it.

    By the user's own convention this holds exactly ONE item at a time.
    """
    store, live = _live()
    current = [(t, r) for t, r in live if t.open and t.frog]
    text = (text or "").strip()
    if not text:
        if not current:
            return "No frog is set."
        t, r = current[0]
        return f"The frog: {t.text}  (open {_age(store, r)}, in {t.where})"
    task, _rec, confident, matches = best_match(live, text)
    if confident and retag_frog(task, live):
        return f"The frog is now: {task.text}  (tagged in {task.where})"
    vault.append(config.TODO_NOTE, f"- [ ] [FROG] {text}",
                 header="### 🐸 DOMAIN: THE FROG")
    near = f" Nearest existing item was '{matches[0][1].text}'." if matches else ""
    return f"No confident match, so added a new frog to TODO.md: {text}.{near}"


@mcp.tool()
def task_context(query: str) -> str:
    """Everything known about one task, for working out how to start it: its
    exact wording, which note and section it lives in, how long it has been
    open, its due date, and any sub-items already written under it.

    Use this before helping the user break a task down, so the suggestion fits
    what is actually written rather than what you assume. The useful output is
    ONE concrete physical first action that takes under two minutes and needs no
    decisions -- name the exact app, page, drawer or person. Task initiation is
    the difficulty here, not knowledge, so skip encouragement entirely.
    """
    store, live = _live()
    task, rec, _confident, matches = best_match(live, query)
    if not matches:
        return f"No open task matches '{query}'."
    lines = [
        f"Task:     {task.text}",
        f"Note:     {task.where}",
        f"Section:  {' / '.join(b for b in (task.domain, task.section) if b) or '(none)'}",
        f"Open for: {_age(store, rec)}",
        f"Due:      {task.due.isoformat() if task.due else '(none set)'}",
        f"Tags:     {', '.join(task.tags) or '(none)'}",
    ]
    kids = [k.text for k in tasklib.parse_note(task.file)
            if k.indent > task.indent and k.lineno > task.lineno][:6]
    lines.append(f"Sub-items already written: {'; '.join(kids) if kids else '(none)'}")
    if len(matches) > 1:
        lines.append(f"({len(matches) - 1} other item(s) also matched.)")
    return "\n".join(lines)


@mcp.tool()
def log_focus(minutes: int, label: str, outcome: str = "") -> str:
    """Record a focus session in today's note.

    This only writes the record. To actually nudge the user partway through or
    at the end, schedule it yourself with your cronjob toolset -- this server
    has no timers.
    """
    path = vault.ensure_daily()
    stamp = vault.now().strftime("%H:%M")
    tail = f" -- {outcome}" if outcome else ""
    vault.append(path, f"- ⏱ {minutes}min focus: {label}{tail}  _({stamp})_",
                 header="## Log")
    return f"Logged a {minutes}min focus session on '{label}'."


@mcp.tool()
def log_shutdown(text: str) -> str:
    """File the user's end-of-day reply into today's note and report what is
    still unfinished.

    Pass their words roughly as given; light tidying of a rambling voice note is
    fine here, unlike capture(). Call this when they are recapping their day.
    """
    text = (text or "").strip()
    if not text:
        return "Nothing to log: empty text."
    path = vault.ensure_daily()
    stamp = vault.now().strftime("%H:%M")
    vault.append(path, f"### Shutdown — {stamp}\n\n{text}", header="## Log")
    evening.disarm()
    pending = evening.unfinished()
    if not pending:
        return f"Filed in {vault.rel(path)}. Nothing left open from today."
    listed = "\n".join(f"  - {t.text}" for t in pending[:8])
    return (f"Filed in {vault.rel(path)}. {len(pending)} unfinished from today, "
            f"which tomorrow's note will inherit:\n{listed}")


@mcp.tool()
def snapshot_note() -> str:
    """The full rendered '_Today.md' snapshot: counts, the suggested three,
    overdue, due soon, waiting, stale and low-energy options. More detail than
    today(); use it when a complete picture is wanted.
    """
    return brief.render_markdown(brief.snapshot())


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

"""Fuzzy matching and mutation of vault tasks.

Extracted from the Telegram bot so the MCP server and the bot cannot drift
apart: the confidence rules here are subtle and were wrong twice during
development (a lone hit on "put" confidently retagged the wrong task, and a
three-letter query auto-ticked the wrong bill).
"""
from . import tasks as tasklib, vault

# A match must clear this share of the query's words before it counts at all.
MIN_MATCH_RATIO = 0.6
CONFIDENT = 500
# A substring hit on a tiny query is not confidence.
MIN_CONFIDENT_WORDS = 2
MIN_CONFIDENT_CHARS = 8


def find_open(live, query, min_ratio=MIN_MATCH_RATIO):
    """Open tasks matching a loose query, best first, as (score, task, rec)."""
    q = tasklib.normalise(query)
    if not q:
        return []
    words = [w for w in q.split() if len(w) > 2] or q.split()
    scored = []
    for t, r in live:
        if not t.open:
            continue
        hay = tasklib.normalise(t.text)
        if not hay:
            continue
        if hay == q:
            score = 1000.0
        elif q in hay:
            score = CONFIDENT + max(0.0, 100.0 - (len(hay) - len(q)))
        else:
            hits = sum(1 for w in words if w in hay)
            ratio = hits / len(words)
            if ratio < min_ratio:
                continue
            score = 100.0 * ratio - 0.1 * len(hay)
            if ratio >= 0.999:
                score += 200.0
        scored.append((score, t, r))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored


def best_match(live, query):
    """Return (task, rec, confident, all_matches).

    `confident` means safe to act on without asking: an exact hit, or a
    substring/all-words hit on a query substantial enough to mean something.
    """
    scored = find_open(live, query)
    if not scored:
        return None, None, False, []
    score, task, rec = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else -1e9
    q = tasklib.normalise(query)
    substantial = len(q.split()) >= MIN_CONFIDENT_WORDS or len(q) >= MIN_CONFIDENT_CHARS
    exact = score >= 1000
    confident = exact or (
        substantial and (score >= CONFIDENT or (score >= 290 and score - runner_up > 50))
    )
    return task, rec, confident, scored


def tick_all(task, mark="x"):
    """Tick every open copy of a task across the vault.

    The same item is often written in more than one note; ticking one copy
    leaves the other open, so it returns on the next scan.
    """
    hit = []
    for t in tasklib.index():
        if t.key != task.key or not t.open:
            continue
        if tasklib.toggle(t.file, t, mark):
            hit.append(t.where)
    return hit


def add_tag(task, tag):
    lines = vault.read(task.file).splitlines()
    want = tasklib.normalise(task.text)
    for i, raw in enumerate(lines):
        cm = tasklib.CHECKBOX.match(raw)
        if not cm or tasklib.normalise(tasklib.clean(cm.group("text"))) != want:
            continue
        if f"[{tag}]" in raw:
            return True
        lines[i] = (f"{cm.group('ws')}{cm.group('bullet')} [{cm.group('mark')}] "
                    f"[{tag}] {cm.group('text')}")
        vault.write(task.file, "\n".join(lines) + "\n")
        return True
    return False


def strip_tag(task, tag):
    lines = vault.read(task.file).splitlines()
    want = tasklib.normalise(task.text)
    for i, raw in enumerate(lines):
        cm = tasklib.CHECKBOX.match(raw)
        if not cm or tasklib.normalise(tasklib.clean(cm.group("text"))) != want:
            continue
        lines[i] = raw.replace(f"[{tag}] ", "").replace(f"[{tag}]", "")
        vault.write(task.file, "\n".join(lines) + "\n")
        return True
    return False


def retag_frog(target, live):
    """Move the [FROG] tag onto `target`, removing it from whatever had it."""
    for t, _r in live:
        if t.frog and t.key != target.key:
            strip_tag(t, "FROG")
    return add_tag(target, "FROG")

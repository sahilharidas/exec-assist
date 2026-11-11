"""Deterministic engine: parsing, identity, de-duplication and the state store."""
import unittest
from datetime import date, timedelta

from tests import support
from ea import tasks as tasklib, vault
from ea.match import best_match
from ea.store import TaskStore


def by_text(items, text):
    return [t for t in items if t.text == text]


class Parsing(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.todo = tasklib.parse_note(support.note("TODO.md"))

    def test_marks(self):
        state = {t.text: (t.open, t.done, t.cancelled) for t in self.todo}
        self.assertEqual(state["Book the meeting room"], (False, True, False))
        self.assertEqual(state["Learn to juggle"], (False, False, True))
        self.assertEqual(state["Renew the passport"], (True, False, False))

    def test_tags_are_read_and_stripped_from_display(self):
        wait = by_text(self.todo, "Reply from the landlord about the lease")[0]
        frog = by_text(self.todo, "Phone the insurer about the claim")[0]
        self.assertTrue(wait.waiting)
        self.assertTrue(frog.frog)

    def test_due_date(self):
        t = by_text(self.todo, "Send the quarterly report to finance")[0]
        self.assertEqual(t.due, date(2020, 1, 15))

    def test_heading_context(self):
        t = by_text(self.todo, "Send the quarterly report to finance")[0]
        self.assertEqual((t.domain, t.section), ("WORK", "Reporting"))
        self.assertEqual(by_text(self.todo, "Renew the passport")[0].domain, "HOME")

    def test_identity_survives_tags_dates_and_emphasis(self):
        # The parser cleans the display text first, then keys on its normal form.
        a = tasklib.normalise(tasklib.clean("[WAIT] **Reply** from the landlord 📅 2026-01-01"))
        b = tasklib.normalise("reply from the landlord")
        self.assertEqual(a, b)


class Dedupe(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.all = tasklib.index()

    def test_same_item_in_two_notes_is_one_task(self):
        copies = by_text(self.all, "Renew the car licence")
        self.assertEqual(len(copies), 2)
        self.assertEqual(len(by_text(tasklib.dedupe(self.all), "Renew the car licence")), 1)

    def test_migrated_copy_is_not_live(self):
        live = by_text(tasklib.dedupe(self.all), "Buy printer paper")
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0].open)

    def test_project_checklists_are_not_todo_lists(self):
        self.assertTrue(vault.is_todo_list(support.note("TODO.md")))
        self.assertFalse(vault.is_todo_list(support.note("Projects", "Shed.md")))


class Matching(unittest.TestCase):
    def setUp(self):
        support.reset()
        store = TaskStore()
        self.live = store.sync()

    def test_tiny_query_is_never_confident(self):
        _t, _r, confident, matches = best_match(self.live, "the")
        self.assertFalse(confident)

    def test_ambiguous_query_is_not_confident(self):
        _t, _r, confident, matches = best_match(self.live, "renew")
        self.assertFalse(confident)
        self.assertGreaterEqual(len(matches), 2)

    def test_specific_query_is_confident(self):
        task, _r, confident, _m = best_match(self.live, "renew the passport")
        self.assertTrue(confident)
        self.assertEqual(task.text, "Renew the passport")

    def test_done_items_do_not_match(self):
        _t, _r, _c, matches = best_match(self.live, "book the meeting room")
        self.assertEqual(matches, [])


class Store(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_already_done_items_get_no_completion_time(self):
        store = TaskStore()
        store.sync()
        rec = next(r for r in store.tasks.values() if r["text"] == "Book the meeting room")
        self.assertTrue(rec.get("pre_existing_done"))
        self.assertNotIn("done_at", rec)

    def test_observed_tick_is_stamped(self):
        store = TaskStore()
        t0 = vault.now()
        store.sync(when=t0)
        store.commit()
        p = support.note("TODO.md")
        p.write_text(p.read_text().replace("- [ ] Renew the passport", "- [x] Renew the passport"))
        store = TaskStore()
        t1 = t0 + timedelta(hours=3)
        store.sync(when=t1)
        rec = next(r for r in store.tasks.values() if r["text"] == "Renew the passport")
        self.assertEqual(rec["done_at"], t1.isoformat())
        self.assertEqual(rec["first_seen"], t0.isoformat())

    def test_first_scan_changes_nothing_in_the_vault(self):
        before = {p: p.read_text() for p in vault.all_notes()}
        TaskStore().sync()
        self.assertEqual(before, {p: p.read_text() for p in vault.all_notes()})

    def test_tick_in_one_note_closes_the_other_copy(self):
        store = TaskStore()
        store.sync()
        store.commit()
        daily = support.note("00 📅 Daily", "2020-01-10.md")
        daily.write_text(daily.read_text().replace("- [ ] Renew the car licence",
                                                   "- [x] Renew the car licence"))
        store = TaskStore()
        store.sync()
        self.assertIn("- [x] Renew the car licence", support.note("TODO.md").read_text())

    def test_age_counts_from_first_seen(self):
        store = TaskStore()
        t0 = vault.now() - timedelta(days=9)
        store.sync(when=t0)
        rec = next(r for r in store.tasks.values() if r["text"] == "Renew the passport")
        self.assertEqual(store.age_days(rec), 9)


if __name__ == "__main__":
    unittest.main()

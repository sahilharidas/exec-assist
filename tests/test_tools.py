"""The MCP tool boundary: what the model is allowed to cause.

These are the two rules the tool descriptions insist on, checked as behaviour:
capture() stores the text it is given without touching it, and complete()
returns candidates instead of guessing when the wording is ambiguous.
"""
import unittest

from tests import support
from ea import vault

server = support.load_server()

RAW = "chase the ER claim thing before fri -- ask abt ref #A-113??"


class Capture(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_text_is_stored_verbatim(self):
        support.call(server.capture, RAW)
        self.assertIn(RAW, vault.daily_path().read_text())

    def test_empty_capture_writes_nothing(self):
        out = support.call(server.capture, "   ")
        self.assertIn("Nothing to capture", out)
        self.assertFalse(vault.daily_path().exists())


class AddTask(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_bad_due_date_is_refused(self):
        out = support.call(server.add_task, "Pay the deposit", due="next friday")
        self.assertIn("Refusing", out)
        self.assertFalse(vault.daily_path().exists())

    def test_due_date_is_written_in_the_form_the_parser_reads(self):
        support.call(server.add_task, "Pay the deposit", due="2030-05-01")
        self.assertIn("- [ ] Pay the deposit 📅 2030-05-01", vault.daily_path().read_text())


class Complete(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.todo = support.note("TODO.md")

    def test_ambiguous_query_ticks_nothing_and_lists_candidates(self):
        before = self.todo.read_text()
        out = support.call(server.complete, "renew")
        self.assertIn("ambiguous", out)
        self.assertIn("Renew the car licence", out)
        self.assertIn("Renew the passport", out)
        self.assertEqual(before, self.todo.read_text())

    def test_no_match_ticks_nothing(self):
        before = self.todo.read_text()
        out = support.call(server.complete, "defrost the freezer")
        self.assertIn("No open task matches", out)
        self.assertEqual(before, self.todo.read_text())

    def test_clear_query_ticks_every_copy(self):
        out = support.call(server.complete, "renew the car licence")
        self.assertIn("Ticked", out)
        self.assertIn("- [x] Renew the car licence", self.todo.read_text())
        daily = support.note("00 📅 Daily", "2020-01-10.md").read_text()
        self.assertIn("- [x] Renew the car licence", daily)
        self.assertIn("- [ ] Renew the passport", self.todo.read_text())


class Reads(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_today_and_waiting_render_without_changing_notes(self):
        before = {p: p.read_text() for p in vault.all_notes()}
        self.assertIn("landlord", support.call(server.waiting))
        self.assertTrue(support.call(server.today))
        after = {p: p.read_text() for p in vault.all_notes() if p in before}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

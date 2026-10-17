from django.test import TestCase
from purchases.states import PURCHASE_TRANSITIONS, SHARE_TRANSITIONS, P, S


class StateTableTest(TestCase):
    def test_terminal_states(self):
        for t in (P.COMPLETED, P.RELEASED, P.REFUNDED):
            self.assertEqual(PURCHASE_TRANSITIONS[t], set())

    def test_no_capture_before_hold(self):
        self.assertNotIn(S.CAPTURED, SHARE_TRANSITIONS[S.VIEWING])
        self.assertIn(S.CAPTURED, SHARE_TRANSITIONS[S.HELD])

    def test_every_state_has_entry(self):
        self.assertEqual(set(PURCHASE_TRANSITIONS), set(P.values))
        self.assertEqual(set(SHARE_TRANSITIONS), set(S.values))

from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from core.events import log_event, verify_chain
from core.models import EventLog


@skipUnless(connection.vendor == "postgresql", "Cross-connection advisory locking requires PostgreSQL")
class ConcurrentEventLogTests(TransactionTestCase):
    def test_parallel_writers_preserve_one_chain(self):
        # Cover both empty-chain initialization and contention on an existing tail.
        def append(writer):
            close_old_connections()
            try:
                for index in range(12):
                    log_event("test.concurrent", actor="test", payload={"writer": writer, "index": index})
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(4)))
        sequences = list(EventLog.objects.order_by("seq").values_list("seq", flat=True))
        self.assertEqual(sequences, list(range(1, 49)))
        self.assertTrue(verify_chain())

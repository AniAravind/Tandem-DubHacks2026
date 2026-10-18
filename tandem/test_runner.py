"""Dispose completed thread connection wrappers before dropping the test DB."""
import gc
from django.db import connections
from django.test.runner import DiscoverRunner


class CleanConnectionRunner(DiscoverRunner):
    def teardown_databases(self,old_config,**kwargs):
        # Joined test threads may leave cyclic, unreachable DB wrappers. Their
        # driver connections must be finalized before PostgreSQL can DROP DB.
        # This does not terminate sessions or change test failure reporting.
        connections.close_all()
        gc.collect()
        return super().teardown_databases(old_config,**kwargs)

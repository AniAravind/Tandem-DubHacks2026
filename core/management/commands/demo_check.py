from django.core.management.base import BaseCommand, CommandError

from core.management.commands.seed_demo import LISTINGS, MEMBERS, uid
from core.models import Group, Member
from merchant.models import Listing


class Command(BaseCommand):
    help = "Check the database still matches the seed exactly (ids, members, caps, listings, prices). Exits with an error if not."

    def handle(self, *args, **opts):
        problems = []
        if not Group.objects.filter(id=uid("group:tahoe")).exists():
            problems.append("the Tahoe group is missing")
        for name, role, cap, musts, last4 in MEMBERS:
            m = Member.objects.filter(id=uid(f"member:{name}")).first()
            if m is None:
                problems.append(f"member {name} is missing")
            elif (m.role, m.status, m.budget_cap_cents) != (role, "active", cap):
                problems.append(f"member {name} changed (role/status/cap)")
        for slug, ref, title, total, unit, capacity, feats, meta in LISTINGS:
            row = Listing.objects.filter(merchant__slug=slug, ref=ref).first()
            if row is None:
                problems.append(f"listing {ref} is missing")
            elif (row.total_cents, row.capacity, row.title) != (total, capacity, title) or not row.available:
                problems.append(f"listing {ref} changed (price/capacity/title/availability)")
        if problems:
            raise CommandError("Demo data does not match the seed:\n  - " + "\n  - ".join(problems))
        self.stdout.write(self.style.SUCCESS(f"Demo data OK: {len(MEMBERS)} members, {len(LISTINGS)} listings"))

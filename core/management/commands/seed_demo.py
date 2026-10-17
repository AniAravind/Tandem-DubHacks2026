import hashlib, uuid
from django.core.management.base import BaseCommand
from django.db import transaction
from core.models import Group, Member
from merchant.models import Merchant, Listing

NS = uuid.UUID("7a3f1c2e-0000-4000-8000-000000000000")
uid = lambda name: uuid.uuid5(NS, name)            # stable demo ids
phone_hash = lambda p: hashlib.sha256(p.encode()).hexdigest()

MEMBERS = [  # name, role, cap_cents, must_haves, demo_card_last4
    ("Maya", "organizer", 25000, ["hot_tub"], "1111"),
    ("Priya", "member", 22000, ["kitchen", "hot_tub"], "1111"),
    ("Sam", "member", 19000, ["separate_beds"], "1111"),
    ("Leo", "member", 23000, ["hot_tub"], "1111"),
    ("Jordan", "member", 20000, [], "0002"),
    ("Alex", "member", 21000, ["hot_tub", "parking"], "1111"),
]
MERCHANTS = [("pinestay", "PineStay Rentals", "rental", "7011"), ("summitpass", "SummitPass", "passes", "7999"),
             ("couchco", "CouchCo", "furniture", "5712"), ("gamehub", "GameHub", "electronics", "5732")]
LISTINGS = [  # merchant, ref, title, total_cents, unit_cents, capacity, features, meta
    ("pinestay", "pinecrest-a-frame", "Pinecrest A-Frame", 105000, None, 6, ["hot_tub", "kitchen", "separate_beds", "parking"],
     {"drive_min": 12, "to": "Heavenly", "cancellation": "Free until Nov 7", "beds": "3 bd, 1 bunk room"}),
    ("pinestay", "lakeview-bungalow", "Lakeview Bungalow", 106800, None, 7, ["kitchen", "separate_beds", "parking"],
     {"drive_min": 18, "to": "Heavenly", "cancellation": "Free until Nov 10"}),
    ("pinestay", "cedar-hollow", "Cedar Hollow", 97200, None, 6, ["kitchen", "separate_beds"],
     {"drive_min": 25, "to": "Heavenly", "cancellation": "Non-refundable"}),
    ("pinestay", "juniper-loft", "Juniper Loft", 92500, None, 5, ["hot_tub", "kitchen", "separate_beds", "parking"],
     {"drive_min": 8, "to": "Heavenly", "cancellation": "Free until Nov 10"}),
    ("summitpass", "lift-2day", "2-day lift pass", 9800, 9800, 50, ["lift_pass"], {"offer": "5% back (demo)"}),
    ("couchco", "sofa-3seat", "3-seat sofa", 119900, None, 4, ["delivery"], {}),
    ("gamehub", "console-bundle", "Console bundle (gift)", 54900, None, 8, ["gift_wrap"], {}),
]


class Command(BaseCommand):
    help = "Load the Tahoe demo (DEMO DATA). Safe to re-run."

    @transaction.atomic
    def handle(self, *args, **opts):
        group, _ = Group.objects.update_or_create(id=uid("group:tahoe"), defaults={"name": "Tahoe weekend", "channel": "web"})
        for i, (name, role, cap, musts, last4) in enumerate(MEMBERS):
            m, _ = Member.objects.update_or_create(id=uid(f"member:{name}"), defaults={
                "group": group, "display_name": name, "role": role, "status": "active",
                "phone_e164_hash": phone_hash(f"+1206555010{i}"), "phone_last4": f"010{i}",
                "prefs": {"must_haves": musts, "dealbreakers": ["shared_beds"], "flexibility": "firm",
                          "demo_card_last4": last4}})
            m.budget_cap_cents = cap
            m.save()
            if role == "organizer":
                group.organizer = m
                group.save(update_fields=["organizer"])
        for slug, name, cat, mcc in MERCHANTS:
            Merchant.objects.update_or_create(slug=slug, defaults={"name": name, "category": cat, "mcc": mcc})
        for slug, ref, title, total, unit, cap, feats, meta in LISTINGS:
            Listing.objects.update_or_create(merchant=Merchant.objects.get(slug=slug), ref=ref, defaults={
                "title": title, "total_cents": total, "unit_price_cents": unit, "capacity": cap,
                "features": feats, "meta": {**meta, "demo_data": True}, "available": True})
        self.stdout.write(self.style.SUCCESS(f"Seeded DEMO group {group.id} with {len(MEMBERS)} members"))

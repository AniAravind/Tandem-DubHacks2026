"""Canonical consent copy. The sha256 of render() is what the member signs."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = "v1"


def format_usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def format_deadline(dt: datetime, timezone_name: str = "America/Los_Angeles") -> str:
    local = dt.astimezone(ZoneInfo(timezone_name))
    hour = local.hour % 12 or 12
    return f"{local.strftime('%a')} {hour}:{local.strftime('%M %p')}"


def can_lines(*, share_cents: int, split_count: int, title: str, merchant_name: str) -> list[str]:
    return [
        f"Hold {format_usd(share_cents)} now (not a charge)",
        f"Charge it only if all {split_count} are in",
        f"Book only {title} via {merchant_name}",
    ]


def can_never_lines(*, cap_cents: int, deadline_label: str) -> list[str]:
    return [
        f"Charge more than {format_usd(cap_cents)}",
        "Pay any other merchant",
        f"Charge you if the group is not fully in by {deadline_label}",
        "See or store your card number",
    ]


def render(
    *,
    member_name: str,
    share_cents: int,
    cap_cents: int,
    split_count: int,
    title: str,
    merchant_name: str,
    deadline_label: str,
) -> str:
    can = "\n".join(f"- {line}" for line in can_lines(
        share_cents=share_cents, split_count=split_count, title=title, merchant_name=merchant_name))
    never = "\n".join(f"- {line}" for line in can_never_lines(
        cap_cents=cap_cents, deadline_label=deadline_label))
    return (
        f"Tandem consent {VERSION}\n"
        f"Member: {member_name}\n"
        f"Share: {format_usd(share_cents)} of a {split_count}-way split\n"
        f"Cap: {format_usd(cap_cents)}\n"
        f"Listing: {title} via {merchant_name}\n"
        f"Deadline: {deadline_label}\n"
        f"Tandem CAN\n{can}\n"
        f"Tandem can NEVER\n{never}\n"
    )

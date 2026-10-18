"""Live web discovery through Tavily. Results are public listings to compare, never Tandem quotes.

Honesty rules enforced here, not in the prompt:
- Titles, URLs and snippets come from the search provider verbatim (redacted, truncated).
- A price is kept only when a "$" amount appears in that result's own text; the exact matched text is stored too.
- Live offers live in their own table, so no vote, hold or checkout path can ever reach one.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import date
from urllib.parse import urlparse

import httpx
from django.core.cache import cache
from django.utils import timezone

from chat.services import redact
from purchases.models import LiveOffer

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
LABELS = "ABCDE"
CACHE_S = 6 * 3600                       # identical searches reuse results (and quota) for six hours
DAILY_MAX = int(os.getenv("LIVE_SEARCH_DAILY_MAX", "60"))
PER_GROUP_10_MIN = 4
DISCLAIMER = "Live web results, not Tandem quotes. Prices are as listed on the source and can change. Checkout isn’t available for these."

_PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{1,6})(?:\.(\d{2}))?(\s*(?:/|per|a)\s*(night|nt|person|guest|pp|each|ticket|day))?", re.I)
_UNITS = {"night": "night", "nt": "night", "day": "night", "person": "person", "guest": "person", "pp": "person",
          "each": "each", "ticket": "each"}


class LiveSearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def configured() -> bool:
    return bool(os.getenv("TAVILY_API_KEY"))


def _query(args: dict) -> str:
    parts = [args["query"].strip()]
    route = args.get('route') or {}
    if args.get('kind') == 'flight' and route:
        parts.insert(0, f"flights from {route['origin']} to {route['destination']}")
        for key in ('origin_airport','destination_airport'):
            if route.get(key):
                parts.append(route[key].upper())
        if route.get('trip_type') == 'round_trip':
            parts.append('round trip')
        elif route.get('trip_type') == 'one_way':
            parts.append('one way')
    for key in ('start','end'):
        value = (args.get('dates') or {}).get(key)
        if value and not any(value in part for part in parts):
            parts.append(value)
    if args.get("location") and args["location"].lower() not in parts[0].lower():
        parts.append(args["location"])
    if args.get("kind") == "stay" and args.get("group_size") and "sleep" not in parts[0].lower():
        parts.append(f"sleeps {args['group_size']}")
    free_query = args['query'].strip()
    parts.remove(free_query)
    constraints = ' '.join(parts)
    if len(constraints) > 195:
        raise LiveSearchError('query_too_long', 'Use concise route/place names so all confirmed dates and constraints fit the search query.')
    remaining = 200 - len(constraints) - (1 if constraints else 0)
    return (constraints + (' ' if constraints else '') + free_query[:remaining]).strip()


def route_direction(text, route):
    """Check explicit ordering of user/model-supplied cities/codes, without an airport lookup table."""
    positions = []
    for side in ('origin','destination'):
        values = [route.get(side,''), route.get(side+'_airport','')]
        matches = []
        for value in values:
            if not value:
                continue
            pattern = r'(?<![a-z])' + re.escape(value).replace(r'\ ', r'[\s_-]+') + r'(?![a-z])'
            flags = re.I if value != route.get(side+'_airport') or text.startswith('https://') else 0
            match = re.search(pattern, text, flags)
            if match:
                matches.append(match.start())
        positions.append(min(matches) if matches else None)
    if None in positions or positions[0] == positions[1]:
        return None
    return 'forward' if positions[0] < positions[1] else 'reverse'


def find_price(text: str) -> tuple[int, str, str] | None:
    """(cents, unit, exact matched text) for the first plausible "$" amount in the text."""
    for m in _PRICE.finditer(text or ""):
        cents = int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)
        if cents < 100:                                         # "$0" / "$1" are almost always UI noise
            continue
        unit = _UNITS.get((m.group(4) or "").lower(), "listed")
        return cents, unit, m.group(0).strip()[:60]
    return None


def nights(dates: dict | None) -> int | None:
    try:
        n = (date.fromisoformat(dates["end"]) - date.fromisoformat(dates["start"])).days
    except (TypeError, KeyError, ValueError):
        return None
    return n if 0 < n <= 30 else None


def per_person_estimate(price_cents: int | None, unit: str, group_size: int, n_nights: int | None, kind: str) -> int | None:
    """A labeled estimate only where the unit is unambiguous. Stay prices without a unit are usually nightly: no guess."""
    if price_cents is None:
        return None
    if unit in {"person", "each"}:
        return price_cents
    if kind == 'flight':
        return None  # An advertised fare is not a group total. Never divide it by travelers.
    if unit == "night":
        return -(-price_cents * n_nights // group_size) if n_nights else None
    if unit == "listed" and kind != "stay":
        return -(-price_cents // group_size)
    return None


def _guard(group_id) -> None:
    today = timezone.now().date().isoformat()
    try:
        used = cache.get_or_set(f"live:day:{today}", 0, timeout=26 * 3600)
        recent = cache.get_or_set(f"live:group:{group_id}", 0, timeout=600)
    except Exception:                                           # noqa: BLE001  fail closed: no quota bookkeeping, no calls
        raise LiveSearchError("live_search_unavailable", "Live search is temporarily unavailable.")
    if used >= DAILY_MAX:
        raise LiveSearchError("live_search_quota", "Today’s live-search allowance is used up. Use catalog options or qualitative advice.")
    if recent >= PER_GROUP_10_MIN:
        raise LiveSearchError("rate_limited", "Several live searches just ran. Compare the current results first.")


def _count(group_id) -> None:
    today = timezone.now().date().isoformat()
    for key, ttl in ((f"live:day:{today}", 26 * 3600), (f"live:group:{group_id}", 600)):
        try:
            cache.add(key, 0, timeout=ttl)
            cache.incr(key)
        except Exception:                                       # noqa: BLE001
            pass


def fetch(query: str, client=None) -> list[dict]:
    """Raw Tavily results [{title, url, content}], cached by query."""
    key = "live:q:" + hashlib.sha256(query.lower().encode()).hexdigest()
    try:
        hit = cache.get(key)
    except Exception:                                           # noqa: BLE001
        hit = None
    if hit is not None:
        return hit
    http = client or httpx
    try:
        response = http.post(TAVILY_URL, timeout=float(os.getenv("LIVE_SEARCH_TIMEOUT_S", "12")),
                             headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
                             json={"query": query, "max_results": 8, "search_depth": "basic", "topic": "general",
                                   "include_answer": False, "include_raw_content": False})
    except httpx.HTTPError:
        log.warning("Live search request failed", exc_info=True)
        raise LiveSearchError("live_search_unavailable", "Live search didn’t respond. Try again shortly or use catalog options.")
    if response.status_code == 429 or response.status_code == 432:
        raise LiveSearchError("live_search_quota", "The live-search provider is out of quota right now.")
    if response.status_code >= 400:
        log.warning("Live search HTTP %s", response.status_code)
        raise LiveSearchError("live_search_unavailable", "Live search returned an error. Try again shortly or use catalog options.")
    try:
        rows = response.json().get("results") or []
    except ValueError:
        rows = []
    results = [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""), "content": str(r.get("content") or "")}
               for r in rows if isinstance(r, dict)]
    try:
        cache.set(key, results, timeout=CACHE_S)
    except Exception:                                           # noqa: BLE001
        pass
    return results


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s*\|\s*", " · ", redact(text))
    text = re.sub(r"[#*_`>\[\]]+", "", text)                    # markdown fragments from scraped pages
    text = re.sub(r"\s+", " ", text).strip(" ·")
    return text[: limit - 1] + "…" if len(text) > limit else text


def search(member, args: dict, brief: dict | None = None, client=None) -> dict:
    if not configured():
        raise LiveSearchError("live_search_unavailable",
                              "Live web search isn’t set up. Use search_options for demo offers or give qualitative advice.")
    route = args.get('route') or {}
    if args.get('kind') == 'flight' and not (route.get('origin') and route.get('destination')):
        raise LiveSearchError('flight_route_required', 'Supply the confirmed origin and destination in route before searching flights. Ask only for missing route details.')
    _guard(member.group_id)
    query = _query(args)
    rows = fetch(query, client)
    _count(member.group_id)
    group_size = args.get("group_size") or (brief or {}).get("group_size") or 1
    n_nights = nights(args.get("dates"))
    budget = round(args["budget_per_person_max"] * 100) if args.get("budget_per_person_max") is not None else None
    search_id = uuid.uuid4().hex
    seen, offers = set(), []
    for row in rows:
        url = row["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or url in seen or len(url) > 500:
            continue
        seen.add(url)
        title = _clean(row["title"], 160)
        if not title:
            continue
        if args.get('kind') == 'flight':
            directions = [route_direction(text, route) for text in (title, url)]
            if 'reverse' in directions or 'forward' not in directions:
                continue
        price = find_price(row["title"] + " " + row["content"])
        cents, unit, matched = price if price else (None, "", "")
        estimate = per_person_estimate(cents, unit, group_size, n_nights, args.get("kind", "other"))
        offers.append(LiveOffer(
            group_id=member.group_id, search_id=search_id, brief_revision=(brief or {}).get("revision") or "",
            label=LABELS[len(offers)], kind=args.get("kind", "other"), query=query, title=title, url=url,
            source=parsed.netloc.removeprefix("www.")[:120], snippet=_clean(row["content"], 320),
            price_cents=cents, price_unit=unit, price_text=matched, est_per_person_cents=estimate))
        if len(offers) == len(LABELS):
            break
    if budget is not None:
        # Keep provider order, but list results known to fit the budget first.
        offers.sort(key=lambda o: 0 if o.est_per_person_cents is not None and o.est_per_person_cents <= budget else 1)
        for i, offer in enumerate(offers):
            offer.label = LABELS[i]
    LiveOffer.objects.bulk_create(offers)
    return {"search_id": search_id, "query": query, "offers": [offer_json(o, budget) for o in offers],
            "disclaimer": DISCLAIMER}


def sanitize_offer(data):
    """Apply current fare rules to old stored offers/plan snapshots too."""
    if data.get('kind') == 'flight':
        data = {**data, 'est_per_person_cents':data.get('price_cents') if data.get('price_unit') in {'person','each'} else None,
                'fits_budget':None, 'price_note':'Advertised fare only. Dates, passenger coverage, baggage and round-trip pricing are not verified.'}
    return data


def offer_json(o: LiveOffer, budget_cents: int | None = None) -> dict:
    return sanitize_offer({"offer_id": str(o.id), "label": o.label, "kind": o.kind, "title": o.title, "url": o.url, "source": o.source,
            "snippet": o.snippet, "price_cents": o.price_cents, "price_unit": o.price_unit or None,
            "price_text": o.price_text or None, "est_per_person_cents": o.est_per_person_cents,
            "fits_budget": (o.est_per_person_cents <= budget_cents) if o.kind != 'flight' and budget_cents is not None and o.est_per_person_cents is not None else None,
            "price_note": "Advertised fare only. Dates, passenger coverage, baggage and round-trip pricing are not verified." if o.kind == 'flight' else None,
            "retrieved_at": o.created_at.isoformat().replace("+00:00", "Z") if o.created_at else None,
            "live": True, "checkout_ready": False})


def latest(group, revision: str | None = None) -> list[LiveOffer]:
    """The group's most recent live search, optionally only if it belongs to the current buying brief."""
    newest = LiveOffer.objects.filter(group=group).order_by("-created_at").first()
    if newest is None or (revision is not None and newest.brief_revision != revision):
        return []
    return list(LiveOffer.objects.filter(group=group, search_id=newest.search_id).order_by("label"))


def for_plan(group, revision):
    """Keep the latest search for EACH requested component in the current brief."""
    rows = LiveOffer.objects.filter(group=group, brief_revision=revision or '').order_by('-created_at')
    batches, result = {}, []
    for offer in rows[:150]:
        batches.setdefault(offer.kind, offer.search_id)
        if batches[offer.kind] == offer.search_id:
            result.append(offer)
    return sorted(result, key=lambda o:(o.kind,o.label))

# Live offers and comparisons (Track 2 → Track 1 contract)

Live offers are **public web search results** (Tavily). They are for discovery and planning only.
They are never quotes: no vote, hold, consent link, or checkout can reference one. The backend enforces
this by keeping them in their own table (`purchases.LiveOffer`), separate from catalog `Option`s.

## Chat cards

Both cards arrive on an agent message whose `text` already contains a complete plain-text version
(numbered/lettered list with links). iMessage shows only the text. The web chat may render the card and
keep the text as the accessible fallback. Unknown card types render `null` today, so nothing breaks
before the card components land.

```ts
export type LiveOffer = {
  offer_id: string;            // uuid
  label: "A" | "B" | "C" | "D" | "E";   // what the user replies with; catalog options use numbers
  kind: "stay" | "product" | "gift" | "flight" | "activity" | "other";
  title: string;               // from the source page, cleaned
  url: string;                 // https only; open with target="_blank" rel="noopener noreferrer nofollow"
  source: string;              // domain, e.g. "vrbo.com"
  snippet: string;             // up to 320 chars, phone/email/card patterns removed
  price_cents: number | null;  // only when a "$" amount appears in that result's own text
  price_unit: "night" | "person" | "each" | "listed" | null;
  price_text: string | null;   // the exact matched text, e.g. "$295 per night"; show as "listed", never as a quote
  est_per_person_cents: number | null;  // labeled estimate; null when the unit is ambiguous
  fits_budget: boolean | null; // private planning only; always null in group chats (budgets are private)
  retrieved_at: string;        // ISO UTC
  live: true;
  checkout_ready: false;
};

// card.type === "LiveOffersCard"
export type LiveOffersCardData = {
  search_id: string;
  query: string;
  offers: LiveOffer[];         // 0–5, best first (budget fits first, then provider order)
  disclaimer: string;          // show verbatim near the list
  live: true;
  checkout_ready: false;
};

// card.type === "CompareCard"
export type CompareRow = {
  kind: "catalog" | "live";
  ref: string;                 // "1".."3" for catalog options, "A".."E" for live offers
  id: string;                  // Option id or LiveOffer id
  title: string;
  source: string;              // merchant slug (catalog) or domain (live)
  per_person_cents: number | null;   // catalog: exact split; live: est_per_person_cents
  total_cents: number | null;        // catalog only
  price_cents?: number | null;       // live only
  price_text: string | null;         // live only
  features: string[];                // catalog only
  cancellation: string;
  url: string | null;                // live only
  checkout_ready: boolean;           // true only for catalog rows
  demo_data: boolean;                // true for catalog rows (simulated)
  fits_budget: boolean | null;
};
export type CompareCardData = {
  rows: CompareRow[];
  cheapest_ref: string | null;
  budget_per_person_cents: number | null;   // the organizer's own budget, private chat only
  note: string;
};
```

UI rules:

- Badge every live row "Live web result" and every catalog row "Demo offer · simulated payment".
- Never show a pay, hold, or "book" button on a live offer. Linking out to the source is fine.
- Show `price_text` with "listed" (for example "$295 per night listed"), plus `retrieved_at` as relative time.
- `est_per_person_cents` is an estimate: label it "about $X each".

## Plan page

`GET /api/v1/plans/<id>` already returns `details`. A planning-only plan (no `selected_option`) now also has:

```ts
details.live_offers: LiveOffer[]        // results from the current buying brief, may be []
details.featured_offer: "A" | ... | null  // the one the organizer picked by replying with its letter
details.live_disclaimer: string | null
```

`checkout_ready` stays `false` for these plans, and `POST .../fund` keeps returning `quote_required`.
Republishing the same brief keeps the same URL and refreshes `live_offers`.

## Chat shortcuts that never call the AI

| User sends (private chat) | Result |
|---|---|
| `1`, `option 2`, `the first one`, an exact title | Publishes that current catalog option (existing) |
| `A` … `E`, `pick B` | Planning page featuring that live offer |
| `compare 1 and B`, `compare A B C` | CompareCard from current options/results |
| `find options` | Re-runs the saved catalog search (existing) |

## Configuration (Track 3)

| Var | Default | Purpose |
|---|---|---|
| `TAVILY_API_KEY` | unset | Enables `live_search`. Unset means the tool reports "not set up" and the agent says so. |
| `LIVE_SEARCH_DAILY_MAX` | 60 | Daily cap across all users (Tavily free tier is 1,000 credits per month). Identical queries are cached for 6 hours. |
| `LIVE_SEARCH_TIMEOUT_S` | 12 | Per search. |
| `XAI_FALLBACK_MODELS` | `openai/gpt-oss-20b` on Groq | Models tried when the primary is rate limited (Groq limits are per model). |
| `XAI_MAX_OUTPUT_TOKENS` | 1500 | Cap on reasoning + reply for gpt-oss models (also sends `reasoning_effort=low`). |

Migration `purchases/0004_liveoffer` runs in the Heroku release phase.

## Flow testing

- Offline (no AI quota): `python manage.py test agent.tests.test_journeys purchases.tests.test_live` covers topic changes,
  stale choices, provider outages, repeated tool calls, live search, letter picks, comparisons, and preferences.
- Real model: `python manage.py journey_check --journey topic` (or `gift`, `vague`; `--list` shows them). Each run makes
  an isolated "Journey check …" private group and fails if a reply misses its expectation or says the AI is stuck.
  Each turn costs about 3–6k AI tokens, so run one journey at a time on the free Groq tier.

"""Two deterministic layers around Grok.

guard(): obvious injection requests (change caps, reveal budgets, skip the vote, "admin mode") get a fixed, polite
         refusal without calling the LLM at all.
filter_text(): before anything Grok wrote is sent, block amounts that no tool returned (and nobody typed), anyone's
         budget, card-number-like digits, and claims that money moved or limits changed.
"""
import re

SAFE_TEXT = "Here's what I found. The cards have the details."
SAFE_TEXT_NO_TOOLS = "I can help find options, run a vote, or split an expense. What would you like to do?"

CAP_REFUSAL = ("I can't change anyone's spending limit from chat. Only each person can do that on their own consent page. "
               "Want me to show {thing} as an option for a vote instead?")
BUDGET_REFUSAL = "I can't share anyone's budget. Budgets stay private. I only check whether an option fits everyone."
VOTE_REFUSAL = "I can't book without a group vote. Want me to start one with the options so far?"
RULES_REFUSAL = "I can't change how I work from chat. I can find options, run a vote, or split an expense."
PRIVACY_REFUSAL = "I keep who's in, who's out, and why private. I can share counts, like how many have held their share."
MONEY_REFUSAL = ("I can't move money from chat. Nothing is charged until everyone has held their own share "
                 "on their private consent page.")

_CAP = re.compile(r"\b(raise|increase|bump|change|set|lift|double|up|max)\b[^.?!]{0,40}\b(caps?|limits?|budgets?)\b", re.I)
_REVEAL = re.compile(r"\b(what'?s|what is|what are|tell me|reveal|show me|list|leak)\b[^.?!]{0,40}\b(budgets?|caps?|limits?|card)\b", re.I)
_PRIVACY = re.compile(r"\bwho (declined|dropped|bailed|said no|backed out|hasn'?t (paid|held)|didn'?t (pay|hold))\b|"
                      r"\bwhy did \w+ (decline|drop|leave|back out)\b", re.I)
_MOVE_MONEY = re.compile(r"\b(charge|capture|refund|transfer|withdraw)\b", re.I)
_NO_VOTE = re.compile(r"\bwithout (a |the |any )?vot(e|ing)\b|\bskip (the )?vot(e|ing)\b|\bno vote\b|\bjust book\b", re.I)
_RULES = re.compile(r"\b(admin|developer|god|debug|sudo) mode\b|\byou are (now )?(an? )?(admin|developer|root)\b|"
                    r"\bignore (all |your |previous |prior |the )*(rules|instructions)\b|\bsystem prompt\b|\bnew instructions\b|"
                    r"</?\s*chat_transcript|\bsystem\s*:", re.I)
_THING = re.compile(r"\bthe (lodge|cabin|house|loft|villa|condo|place)\b", re.I)


def guard(text: str) -> str | None:
    if _CAP.search(text):
        thing = _THING.search(text)
        return CAP_REFUSAL.format(thing=f"the {thing.group(1).lower()}" if thing else "it")
    if _REVEAL.search(text):
        return BUDGET_REFUSAL
    if _PRIVACY.search(text):
        return PRIVACY_REFUSAL
    if _RULES.search(text):
        return RULES_REFUSAL
    if _MOVE_MONEY.search(text):
        return MONEY_REFUSAL
    if _NO_VOTE.search(text):
        return VOTE_REFUSAL
    if _RULES.search(text):
        return RULES_REFUSAL
    return None


_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CLAIM = re.compile(r"\b(i|i've|i have|we've|we have|done[,!.]? i)\s+(just\s+)?(booked|charged|paid|captured|refunded|raised|"
                    r"increased|changed|set|updated|lifted)\b[^.?!]{0,40}?(\b(caps?|limits?|budgets?|card|booking|room|cabin|lodge|"
                    r"everyone|you)\b|\$)", re.I)


def amounts_in(text: str) -> set[int]:
    """Every "$x" in the text, in cents."""
    out = set()
    for whole, frac in _MONEY.findall(text or ""):
        out.add(int(whole.replace(",", "")) * 100 + int((frac or "0").ljust(2, "0")))
    return out


def planning_amounts_in(text: str) -> set[int]:
    """Explicit user budget phrases, including '300 per person' without a dollar sign."""
    values = amounts_in(text)
    for amount in re.findall(r'(?<![\d/])\b(\d+(?:\.\d{1,2})?)\s*(?:dollars?\b|USD\b|(?:per\s+(?:person|buyer)|each)\b)', text, re.I):
        values.add(round(float(amount) * 100))
    return values


def cents_in(value) -> set[int]:
    """Every integer under a *_cents key, anywhere in a tool result."""
    found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k.endswith("_cents") and isinstance(v, int) and not isinstance(v, bool):
                found.add(abs(v))
            found |= cents_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= cents_in(v)
    return found


def filter_text(text: str, tool_cents: set[int], typed_cents: set[int], private_caps: set[int]) -> tuple[str, str | None]:
    """(text to send, reason it was blocked or None)."""
    if _PAN.search(text):
        return "", "pan_like_digits"
    if _CLAIM.search(text):
        return "", "money_claim"
    for cents in amounts_in(text):
        if cents in private_caps and cents not in tool_cents:
            return "", "budget_value"
        if cents not in tool_cents and cents not in typed_cents:
            return "", "invented_amount"
    return text, None

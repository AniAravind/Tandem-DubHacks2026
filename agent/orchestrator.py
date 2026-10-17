"""Grok tool-calling loop. Grok proposes tool calls; the rules engine (or the fake) decides what actually happens."""
import json
import pathlib

TOOLS = json.loads((pathlib.Path(__file__).parent / "tools.json").read_text())
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
SYSTEM_PROMPT = (pathlib.Path(__file__).parent / "prompts" / "system.md").read_text()


def get_engine():
    """The real RulesEngine once it is on main, else the fake (same execute(tool, args, actor) interface)."""
    try:
        from purchases.rules import RulesEngine
    except ImportError:
        from agent.fakes import FakeRulesEngine
        return FakeRulesEngine()
    return RulesEngine()

"""A scripted stand-in for the xAI client so agent tests never call the network."""
import json
import uuid
from types import SimpleNamespace

from django.core.management import call_command

from agent import fakes
from core.models import Group

TAHOE = uuid.uuid5(uuid.UUID("7a3f1c2e-0000-4000-8000-000000000000"), "group:tahoe")


def seed():
    call_command("seed_demo", stdout=open("/dev/null", "w"))
    fakes.reset(str(TAHOE))
    return Group.objects.get(pk=TAHOE)


def tool_call(name: str, args) -> SimpleNamespace:
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id="call_" + uuid.uuid4().hex[:8], type="function",
                           function=SimpleNamespace(name=name, arguments=raw))


def reply(content=None, calls=None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls or None))])


class ScriptedLLM:
    """Returns the given responses in order; records every request. A response may be a callable(messages) -> reply."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    def __call__(self, messages, tools, **kw):
        self.requests.append(json.loads(json.dumps(messages)))
        if not self.responses:
            return reply("Done.")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(messages) if callable(r) else r

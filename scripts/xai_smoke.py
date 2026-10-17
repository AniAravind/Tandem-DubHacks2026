import json, pathlib
from dotenv import load_dotenv
load_dotenv()
from agent.llm import chat

tools = json.loads(pathlib.Path("agent/tools.json").read_text())
r = chat([{"role": "system", "content": "You are Tandem. Use tools."},
          {"role": "user", "content": "Find a cabin in Tahoe Nov 14-16 2026 for 6 people, about $200 each."}], tools)
print(r.choices[0].message.tool_calls)   # expect search_options(category="rental", group_size=6, ...)

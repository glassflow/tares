"""ee MCP tools follow the licence (TR-1041): the tools an ee team hands out through
Team.mcp_tools() are registered into EE_TOOLS and listed only while the licence allows ee.

  No ee team   EE_TOOLS is empty, _ee_on is never awaited, the tool list is what it was.
  ee team      a fake team with one tool, ee_ping, is registered; it never joins FACTORY_TOOLS.
  Not allowed  hidden from list_tools for a person and for a crew station; call_tool answers the
               licence text and does not run the tool.
  Allowed      listed after the outcome tools of a factory cell; call_tool runs it.
  Safety       a tool named like a core tool is skipped; a team whose mcp_tools raises leaves
               EE_TOOLS empty; _visible keeps its old answer with its default ee_on.

Run: python tests/test_mcp_ee_tools.py
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

from tares import mcp_server as ms
from tares import team as team_mod
from tares.team import SingleTeam

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


LICENCE_TEXT = "licence_required: This needs an enterprise licence. https://docs.glassflow.ai/tares/enterprise"
ALLOWED = False
ASKED = 0
RAN = []


async def fake_ee_on():
    global ASKED
    ASKED += 1
    return ALLOWED


def surface(edition):
    async def _s():
        return (edition, False)
    return _s


async def ee_ping() -> str:
    """Ping from the ee team."""
    RAN.append("ee_ping")
    return "pong"


async def read_clash() -> str:
    """Named like a core tool on purpose."""
    return "no"
read_clash.__name__ = "list_tickets"


class FakeTeam(SingleTeam):
    def __init__(self, tools):
        self._tools = tools

    def mcp_tools(self):
        return self._tools


class Boom(SingleTeam):
    def mcp_tools(self):
        return [ee_ping, "not a function"]   # the second one fails after the first registered


def names():
    return [t.name for t in asyncio.run(ms.mcp.list_tools())]


def call(name):
    r = asyncio.run(ms.mcp.call_tool(name, {}))
    if isinstance(r, ms.types.CallToolResult):
        return r.content[0].text
    blocks = r[0] if isinstance(r, tuple) else r
    return blocks[0].text


ms._ee_on = fake_ee_on
ms._surface = surface("factory")

print("== no ee team ==")
base = names()
team_mod._set_ee_for_tests(None)
ms._register_ee_tools()
ck("EE_TOOLS is empty", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))
ASKED = 0
after = names()
ck("the tool list is what it was", after == base, str(set(after) ^ set(base)))
ck("_ee_on is never awaited", ASKED == 0, str(ASKED))
ck("_visible keeps its answer with the default ee_on",
   ms._visible("read", "factory", False, False) is False and ms._visible("read", "", True, False) is True
   and ms._visible("answer_question", "factory", False, False) is True)

print("== a team that raises, a name clash ==")
team_mod._set_ee_for_tests(Boom())
ms._register_ee_tools()
ck("a failing mcp_tools leaves EE_TOOLS empty", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))
ck("and the tool it registered first is taken back", "ee_ping" not in names())
team_mod._set_ee_for_tests(FakeTeam([read_clash]))
ms._register_ee_tools()
ck("a tool named like a core tool is skipped", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))
ck("the core tool is still the core one", "list_tickets" in names())

print("== an ee team with one tool ==")
team_mod._set_ee_for_tests(FakeTeam([ee_ping]))
ms._register_ee_tools()
ck("EE_TOOLS is {ee_ping}", ms.EE_TOOLS == {"ee_ping"}, str(ms.EE_TOOLS))
ck("ee_ping is in no core set", not ({"ee_ping"} & (ms.FACTORY_TOOLS | ms.FACTORY_ONLY | ms.SRE_TOOLS | ms.OBJECT_TOOLS)))

ALLOWED = False
n = names()
ck("hidden for a person while not allowed", "ee_ping" not in n)
ck("the rest of the list is unchanged", n == base, str(set(n) ^ set(base)))
os.environ["FACTORY_ROLE"] = "builder"
n = names()
ck("hidden for a crew station while not allowed", "ee_ping" not in n)
ck("a station still sees the crew tools", "list_tickets" in n)
RAN.clear()
ck("call_tool answers the licence text for a station", call("ee_ping") == LICENCE_TEXT)
del os.environ["FACTORY_ROLE"]
ck("call_tool answers the licence text for a person", call("ee_ping") == LICENCE_TEXT)
ck("and does not run the tool", RAN == [], str(RAN))

ALLOWED = True
n = names()
ck("listed for a person while allowed", "ee_ping" in n)
ck("after the outcome tools on a factory cell",
   n[:4] == [t for t in n if t in ms.FACTORY_TOOLS][:4] and n.index("ee_ping") >= 4, str(n[:6]))
os.environ["FACTORY_ROLE"] = "builder"
ck("listed for a crew station while allowed", "ee_ping" in names())
del os.environ["FACTORY_ROLE"]
ck("call_tool runs it while allowed", call("ee_ping") == "pong" and RAN == ["ee_ping"], str(RAN))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

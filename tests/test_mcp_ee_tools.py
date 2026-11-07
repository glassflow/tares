"""ee MCP tools follow the licence (TR-1041): the tools an ee team hands out through
Team.mcp_tools() are registered into EE_TOOLS and listed only while the licence allows ee.

  No ee team   EE_TOOLS is empty, _ee_on is never awaited, the tool list is what it was.
  ee team      a fake team with one tool, ee_ping, is registered; it never joins FACTORY_TOOLS.
  Not allowed  hidden from list_tools for a person and for a crew station; call_tool answers the
               licence text and does not run the tool.
  Allowed      listed after the outcome tools of a factory cell; call_tool runs it.
  Safety       a tool named like a core tool is skipped; a team whose mcp_tools raises leaves
               EE_TOOLS empty; a tool that cannot be taken back stays in EE_TOOLS; a tool list
               that raises is survived; _visible keeps its old answer with its default ee_on.
  _ee_on       the real one, with the daemon call stubbed: ee_allowed true and false, any error
               or a body without ee_allowed gives False, the TTL, one answer per token.

Run: python tests/test_mcp_ee_tools.py
"""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

from tares import team as team_mod
from tares.team import SingleTeam

team_mod._set_ee_for_tests(None)   # an installed ee team would put its own tools in the first list
from tares import mcp_server as ms

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


real_ee_on = ms._ee_on
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

real_list, real_remove = ms.mcp._tool_manager.list_tools, ms.mcp.remove_tool
def _list_boom():
    raise RuntimeError("list failed")
ms.mcp._tool_manager.list_tools = _list_boom
team_mod._set_ee_for_tests(FakeTeam([ee_ping]))
try:
    ms._register_ee_tools()
    raised = None
except Exception as e:
    raised = e
finally:
    ms.mcp._tool_manager.list_tools = real_list
ck("a tool list that raises does not break the import", raised is None, repr(raised))
ck("and leaves EE_TOOLS empty", ms.EE_TOOLS == set(), str(ms.EE_TOOLS))

def _remove_boom(name):
    raise RuntimeError("remove failed")
ms.mcp.remove_tool = _remove_boom
team_mod._set_ee_for_tests(Boom())
try:
    ms._register_ee_tools()
finally:
    ms.mcp.remove_tool = real_remove
ck("a tool that cannot be taken back stays in EE_TOOLS", ms.EE_TOOLS == {"ee_ping"}, str(ms.EE_TOOLS))
ALLOWED = False
ck("so the licence still hides it", "ee_ping" not in names())
ms.mcp.remove_tool("ee_ping")   # leave the registry as it was
ms.EE_TOOLS.clear()

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

print("== the real _ee_on, the daemon call stubbed ==")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._body


URLS = []
REPLY = {"r": Resp(200, {"ee_allowed": True})}


class FakeCx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url):
        URLS.append(url)
        r = REPLY["r"]
        if isinstance(r, Exception):
            raise r
        return r


real_cx, real_tok, real_http = ms._cx, ms.AUTH_TOKEN, ms._HTTP
old_ttl = os.environ.get("TARES_MCP_SURFACE_TTL")
ms._cx = lambda *a, **k: FakeCx()
ms._HTTP = False


def ee_on(tok="t1", ttl="30"):
    ms.AUTH_TOKEN = tok
    os.environ["TARES_MCP_SURFACE_TTL"] = ttl
    return asyncio.run(real_ee_on())


try:
    ms._EE_ON.clear()
    REPLY["r"] = Resp(200, {"ee_allowed": True, "valid": False})
    ck("ee_allowed true gives True, whatever else the body says", ee_on() is True)
    ck("it asks the licence route", URLS and URLS[-1].endswith("/api/licence"), str(URLS))
    ms._EE_ON.clear()
    REPLY["r"] = Resp(200, {"ee_allowed": False, "valid": True})
    ck("ee_allowed false gives False, even for a valid key", ee_on() is False)
    for label, reply in (("a 403", Resp(403, {})), ("a 500", Resp(500, {})),
                         ("a body without ee_allowed", Resp(200, {"valid": True})),
                         ("a request that raises", RuntimeError("down"))):
        ms._EE_ON.clear()
        REPLY["r"] = reply
        ck(f"{label} gives False", ee_on() is False)
    ms._EE_ON.clear(); URLS.clear()
    REPLY["r"] = Resp(500, {})
    ee_on(); REPLY["r"] = Resp(200, {"ee_allowed": True})
    ck("an error is not kept: the next look asks again and can say True", ee_on() is True and len(URLS) == 2, str(URLS))
    ms._EE_ON.clear(); URLS.clear()
    a = ee_on("t1"); REPLY["r"] = Resp(200, {"ee_allowed": False}); b = ee_on("t1")
    ck("a second look within the TTL reuses the answer", a is True and b is True and len(URLS) == 1, str(URLS))
    c = ee_on("t2")
    ck("each token has its own answer", c is False and len(URLS) == 2, str(URLS))
    REPLY["r"] = Resp(200, {"ee_allowed": True})
    d = ee_on("t1", ttl="0")
    ck("a TTL of 0 asks every time", d is True and len(URLS) == 3, str(URLS))
    ms._EE_ON.clear(); URLS.clear()
    REPLY["r"] = Resp(200, {"ee_allowed": True})
    ee_on("t1", ttl="junk"); ee_on("t1", ttl="junk")
    ck("a TTL that is not a number falls back to 30 s", len(URLS) == 1, str(URLS))
finally:
    ms._cx, ms.AUTH_TOKEN, ms._HTTP = real_cx, real_tok, real_http
    ms._EE_ON.clear()
    if old_ttl is None:
        os.environ.pop("TARES_MCP_SURFACE_TTL", None)
    else:
        os.environ["TARES_MCP_SURFACE_TTL"] = old_ttl

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

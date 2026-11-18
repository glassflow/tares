"""A station starts lean and never looser than the person's own Claude Code settings.

`launch.launch_plan` and `launch.launch` add, on the machine and for any cell (and for a kept copy of a spec):
 a. `--setting-sources project,local` and `--no-chrome` in the arguments, once, before the prompt, and
    `disableClaudeAiConnectors` in the station's settings;
 b. the person's restrictions (permissions.deny and ask, autoMode.soft_deny and environment of their own
    ~/.claude/settings.json) in the station's settings next to its own, so the station's set is a superset
    of the person's; their allow rules are not copied; a settings file that cannot be read stops the start;
 c. the telemetry variables (OTEL_* and the two CLAUDE_CODE_*TELEMETRY switches; the person's settings env and
    the spec's, the spec's winning) in the environment of the process that starts the station and, for the
    ones that cannot hold a secret, in the station's settings file env, because a session started in a
    running Claude Code background service gets only the file's env. A name or value that may hold a secret
    (headers, key, token, secret, auth, password, credential in a name or in a query or list pair, an address with a user,
    bare or with a scheme) goes in no file and is
    never printed; the kept copy of the spec has no OTEL_*;
 d. the settings file with mode 0600.
Everything runs in a scratch crew home with a fake HOME; the real ~/.claude and ~/factory are never read.

Run: python tests/test_crew_launch_lean.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-launch-lean-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_", "OTEL_"))]:
    del os.environ[_k]
HOME = SCRATCH / "home"
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")
(HOME / ".claude").mkdir(parents=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

from tares.crew import launch as L  # noqa: E402
from tares.crew import stations  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-launch-lean-0123456789"
SECRET = "Bearer fake-otel-header-secret-4242"
PERSON_ENDPOINT = "https://person-collector.example.com:4318"
SPEC_ENDPOINT = "https://crew-collector.example.com:4318"
STARTED = "backgrounded · abcd1234 · crew-reviewer"
_n = [0]

PERSON = {
    "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": PERSON_ENDPOINT, "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization={SECRET}",
            "OTEL_METRICS_EXPORTER": "otlp", "NOT_TELEMETRY": "stays-out"},
    "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm -rf:*)", "Read(~/.ssh/**)", "Bash(gh pr merge:*)"],
                    "ask": ["Bash(git push:*)", "Bash(curl:*)"]},
    "autoMode": {"allow": ["The person allows everything in /tmp"], "soft_deny": ["Never touch the billing account", "$defaults"],
                 "environment": ["The person works from a laptop at home"]},
}


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def person(doc):
    f = HOME / ".claude" / "settings.json"
    if doc is None:
        f.unlink(missing_ok=True)
    elif isinstance(doc, str):
        f.write_text(doc, encoding="utf-8", newline="\n")
    else:
        f.write_text(json.dumps(doc), encoding="utf-8", newline="\n")


def start(station="crew-reviewer", role="reviewer", spec_env=None, tares_up=True, tmp=None):
    """Run launch with a stand-in claude. Returns (error text or None, run_claude's args and keywords, the
    crew home, the client's last plan)."""
    _n[0] += 1
    tmp = tmp or SCRATCH / f"t{_n[0]}"
    if not tmp.exists():
        tmp.mkdir()
        LS.make_crew(tmp, {"token": FAKE_TOKEN})
    kit = LS.make_kit(tmp)
    seen = {}

    def fake(args, claude=None, timeout=None, platform=None, **kw):
        seen["args"], seen["kw"] = [str(a) for a in args], kw
        return subprocess.CompletedProcess(args, 0, STARTED.replace("crew-reviewer", station), None)

    real_run, real_ask = L.cl.run_claude, L._ask
    if spec_env:
        def ask(client, station, role, query):
            doc = real_ask(client, station, role, query)
            doc["env"].update(spec_env)
            return doc
        L._ask = ask
    L.cl.run_claude = fake
    os.environ["TARES_PLUGIN_DIR"] = str(kit)
    tares = LS.FakeTares({"projects": []})
    tares.__enter__()
    if not tares_up:
        tares.__exit__(None, None, None)
    c = TaresClient(CrewPaths.at(tmp / "crew"), env={**os.environ, "TARESD_URL": tares.url})
    err = None
    try:
        c.prime(offline_ok=True)
        errout = io.StringIO()
        try:
            with contextlib.redirect_stderr(errout):
                L.launch(c, station, role, "", "", str(tmp / "repo"), str(tmp / "crew"), "sonnet", "high", "go",
                         claude="claude-stand-in", sleep=lambda _s: None)
        except CrewError as e:
            err = str(e)
        seen["stderr"] = errout.getvalue()
    finally:
        L.cl.run_claude, L._ask = real_run, real_ask
        os.environ.pop("TARES_PLUGIN_DIR", None)
        c.close()
        if tares_up:
            tares.__exit__(None, None, None)
    return err, seen, tmp / "crew"


def settings_in(crew_home, station="crew-reviewer"):
    return json.loads((crew_home / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))


def files_under(crew_home):
    return [p for p in crew_home.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------------------------
print("a. the lean flags and the connectors key")
person(None)
err, seen, crew_home = start()
args = seen.get("args", [])
ck("a start with no settings of the person's goes through", err is None, str(err))
at = args.index("--setting-sources") if "--setting-sources" in args else -1
ck("--setting-sources project,local is in the arguments, once", at > 0 and args[at + 1] == "project,local"
   and args.count("--setting-sources") == 1, str(args))
ck("--no-chrome is in the arguments, once", args.count("--no-chrome") == 1, str(args))
ck("both come after --settings and before the prompt, which stays last",
   args.index("--settings") < at and args.index("--no-chrome") < len(args) - 1 and args[-1] == "go", str(args))
doc = settings_in(crew_home)
ck("the settings carry disableClaudeAiConnectors true", doc.get("disableClaudeAiConnectors") is True, str(sorted(doc)))
ck("the settings do not turn off the bundled skills or strict MCP (the Tares plugin's own server stays)",
   "disableBundledSkills" not in doc and "--strict-mcp-config" not in args, str(args))
ck("with no settings of the person's, the station's permissions are its own, and gh pr merge is denied",
   doc["permissions"].get("deny") == ["Bash(gh pr merge:*)"] and "ask" not in doc["permissions"], str(doc["permissions"]))
ck("the plan's arguments do not repeat the flags when the spec has them already",
   L.lean_args(args).count("--setting-sources") == 1 and L.lean_args(args) == args, str(L.lean_args(args)))
spec_args = ["--settings", "s", "--setting-sources", "user,project,local", "--model", "m"]
fixed = L.lean_args(spec_args)
ck("a spec that carries --setting-sources has it forced to project,local, once",
   fixed.count("--setting-sources") == 1 and fixed[fixed.index("--setting-sources") + 1] == "project,local", str(fixed))
ck("a spec that carries --setting-sources gets --no-chrome too, once, and nothing else moves",
   fixed.count("--no-chrome") == 1 and [a for a in fixed if a != "--no-chrome"] == ["--settings", "s", "--setting-sources", "project,local", "--model", "m"], str(fixed))
eq = L.lean_args(["--settings", "s", "--setting-sources=user,project,local", "--model", "m"])
ck("the --setting-sources=value form is replaced, not left beside the lean one",
   not any(a.startswith("--setting-sources=") for a in eq) and eq.count("--setting-sources") == 1
   and eq[eq.index("--setting-sources") + 1] == "project,local" and eq.count("--no-chrome") == 1, str(eq))
once = L.lean_args(["--settings", "s", "--no-chrome", "--model", "m"])
ck("a spec with --no-chrome and no --setting-sources does not get --no-chrome twice", once.count("--no-chrome") == 1 and "project,local" in once, str(once))
ck("a spec with --setting-sources and no value gets the value", L.lean_args(["--setting-sources"])[:2] == ["--setting-sources", "project,local"], str(L.lean_args(["--setting-sources"])))

# ---------------------------------------------------------------------------------------------
print("b. the person's restrictions are copied in, never replacing the station's")
person(PERSON)
err, seen, crew_home = start()
doc = settings_in(crew_home)
perms, auto = doc["permissions"], doc["autoMode"]
want = {("permissions", "deny"): PERSON["permissions"]["deny"], ("permissions", "ask"): PERSON["permissions"]["ask"],
        ("autoMode", "soft_deny"): PERSON["autoMode"]["soft_deny"], ("autoMode", "environment"): PERSON["autoMode"]["environment"]}
missing = {f"{a}.{b}": [x for x in entries if x not in doc[a].get(b, [])] for (a, b), entries in want.items()}
ck("every deny, ask, soft_deny and environment entry of the person's is in the station's settings (a superset)",
   err is None and not any(missing.values()), str(missing))
ck("the station's own deny survives, once, and still comes first", perms["deny"][0] == "Bash(gh pr merge:*)"
   and perms["deny"].count("Bash(gh pr merge:*)") == 1, str(perms["deny"]))
ck("the station's own allow is untouched", perms["allow"] == L.permissions("reviewer")["allow"], str(perms["allow"]))
ck("the station's autoMode keeps $defaults first and its own sentence in environment",
   auto["soft_deny"][0] == "$defaults" and auto["environment"][0] == "$defaults"
   and any("crew-reviewer" in s for s in auto["environment"]), str(auto["environment"])[:300])
ck("$defaults is not repeated in soft_deny", auto["soft_deny"].count("$defaults") == 1, str(auto["soft_deny"])[:200])
ck("the person's allow rules are not copied", "Bash(ls:*)" not in perms["allow"]
   and "The person allows everything in /tmp" not in auto["allow"], str((perms["allow"], auto["allow"])))
ck("the person's other env (not OTEL) is not copied into the station", "NOT_TELEMETRY" not in json.dumps(doc), str(doc["env"]))
ck("nothing is lost on a second start: the entries are not doubled",
   settings_in(start()[2])["permissions"]["deny"] == perms["deny"], "")

person({"permissions": {"deny": ["Bash(a:*)", 3, None, {"x": 1}], "ask": "Bash(b:*)"}, "autoMode": []})
err, seen, crew_home = start()
doc = settings_in(crew_home)
ck("entries that are not strings and lists that are not lists are left out, not crashed on",
   err is None and doc["permissions"]["deny"] == ["Bash(gh pr merge:*)", "Bash(a:*)"] and "ask" not in doc["permissions"],
   str(doc["permissions"]))

for label, text in (("not valid JSON", "{ nope"), ("a JSON list", None)):
    person(text if text else "[1, 2]")
    err, seen, crew_home = start()
    if label == "not valid JSON":
        ck("a settings file that is not JSON stops the start, names the file, and starts nothing",
           err is not None and "settings.json" in err and "args" not in seen
           and not (crew_home / "stations" / "crew-reviewer.settings.json").exists(), str((err, seen.keys())))
    else:
        ck("a settings file that is a JSON list adds nothing and does not stop the start",
           err is None and settings_in(crew_home)["permissions"]["deny"] == ["Bash(gh pr merge:*)"], str(err))
person("﻿" + json.dumps(PERSON))
err, seen, crew_home = start()
ck("a settings file with a byte order mark is read", err is None and "Bash(rm -rf:*)" in settings_in(crew_home)["permissions"]["deny"], str(err))

# ---------------------------------------------------------------------------------------------
print("c. telemetry reaches the station through its process environment and, without secrets, its settings file")
person(PERSON)
spec_otel = {"OTEL_EXPORTER_OTLP_ENDPOINT": SPEC_ENDPOINT, "OTEL_RESOURCE_ATTRIBUTES": "tares.station=crew-reviewer",
             "CLAUDE_CODE_ENABLE_TELEMETRY": "1"}
err, seen, crew_home = start(spec_env=spec_otel)
extra = seen.get("kw", {}).get("env_extra", {})
doc = settings_in(crew_home)
fenv = doc["env"]
ck("the process gets the spec's OTEL_* variables", extra.get("OTEL_RESOURCE_ATTRIBUTES") == "tares.station=crew-reviewer", str(sorted(extra)))
ck("the process gets the person's OTEL_* variables, the header one too", extra.get("OTEL_METRICS_EXPORTER") == "otlp"
   and extra.get("OTEL_EXPORTER_OTLP_HEADERS") == f"Authorization={SECRET}", str(sorted(extra)))
ck("the spec's value wins over the person's for the same variable", extra.get("OTEL_EXPORTER_OTLP_ENDPOINT") == SPEC_ENDPOINT, str(extra.get("OTEL_EXPORTER_OTLP_ENDPOINT")))
ck("only telemetry variables are added to the process besides FORCE_COLOR (and PWD)",
   not [k for k in extra if not k.startswith(("OTEL_", "CLAUDE_CODE_")) and k not in ("FORCE_COLOR", "PWD")], str(sorted(extra)))
ck("the settings file's env has the endpoint (the spec's), the exporters, the resource attributes and the switch",
   fenv.get("OTEL_EXPORTER_OTLP_ENDPOINT") == SPEC_ENDPOINT and fenv.get("OTEL_METRICS_EXPORTER") == "otlp"
   and fenv.get("OTEL_RESOURCE_ATTRIBUTES") == "tares.station=crew-reviewer"
   and fenv.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1", str(sorted(fenv)))
ck("the settings file's env has no variable that may hold a secret", "OTEL_EXPORTER_OTLP_HEADERS" not in fenv, str(sorted(fenv)))
ck("a variable that is not telemetry does not reach the file from the person's env", "NOT_TELEMETRY" not in fenv, str(sorted(fenv)))
leaks = []
for f in files_under(crew_home):
    body = f.read_bytes().decode("utf-8", "replace")
    if "OTEL_EXPORTER_OTLP_HEADERS" in body or SECRET in body or "person-collector.example.com" in body:
        leaks.append(str(f.relative_to(crew_home)))
ck("no file under the crew home holds the header variable or its value (settings, kept spec, state, logs)", not leaks and len(files_under(crew_home)) > 3, str(leaks))
ck("the kept copy of the spec exists, and is a launch spec without OTEL_*",
   (crew_home / "launch" / "crew-reviewer.json").is_file()
   and not [k for k in json.loads((crew_home / "launch" / "crew-reviewer.json").read_text(encoding="utf-8"))["env"] if k.startswith("OTEL_")], "")

plain, held = L.split_telemetry({
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c.example.com:4318", "OTEL_EXPORTER_OTLP_HEADERS": "a=b",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "a=b", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://u:p@c.example.com",
    "OTEL_API_KEY": "k", "OTEL_X_TOKEN": "t", "OTEL_RESOURCE_ATTRIBUTES": "tares.station=s", "OTEL_METRICS_EXPORTER": "otlp"})
ck("split_telemetry keeps headers, keys, tokens and an address with credentials out of the file",
   sorted(held) == ["OTEL_API_KEY", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                    "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_X_TOKEN"], str(sorted(held)))
ck("split_telemetry lets the endpoint, the exporters and the resource attributes through",
   sorted(plain) == ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_METRICS_EXPORTER", "OTEL_RESOURCE_ATTRIBUTES"], str(sorted(plain)))

# a secret in a value, not only in a name: a query key, a bare user:pw@host, a resource attribute
QUERY, BARE, RES, RES2 = ("https://c.example.com:4318/v1?x=1&api_key=qk-fake-1", "pw-user:pw-fake-2@c.example.com:4318",
                          "tares.station=s,service.auth_token=ra-fake-3", "tares.station=s,db%5Fpassword=ra-fake-4")
plain, held = L.split_telemetry({
    "OTEL_EXPORTER_OTLP_ENDPOINT": QUERY, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": BARE,
    "OTEL_RESOURCE_ATTRIBUTES": RES, "OTEL_X_ATTRS": RES2, "OTEL_EXPORTER_OTLP_CLIENT_KEY": "/etc/otel/client.key",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE": "/etc/otel/client.crt",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://c.example.com:4318/v1/logs?timeout=5&compression=gzip",
    "OTEL_RESOURCE_ATTRIBUTES_PLAIN": "tares.station=s,deployment.environment=prod,service.owner=ops",
    "OTEL_METRICS_EXPORTER": "otlp"})
ck("split_telemetry holds an address with a ?api_key= query, a bare user:pw@host and a secret pair in the resource attributes",
   sorted(held) == ["OTEL_EXPORTER_OTLP_CLIENT_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    "OTEL_RESOURCE_ATTRIBUTES", "OTEL_X_ATTRS"], str(sorted(held)))
FRAG, PAIRBARE, SEMI, ENC, USER_ONLY = ("https://h.example.com/v1?x=1#access_token=frag-fake-5", "service.name=x,ep=u:pair-fake-6@h.example.com",
                                        "a=b;u:semi-fake-7@h.example.com", "tares.station=s,pass%77ord=enc-fake-8", "token-user@c.example.com:4318")
plain2, held2 = L.split_telemetry({"OTEL_A": FRAG, "OTEL_B": PAIRBARE, "OTEL_C": SEMI, "OTEL_D": ENC, "OTEL_E": USER_ONLY,
                                 "OTEL_EXPORTER_OTLP_ENDPOINT": "https://auth.example.com:4318/v1/traces",  # gitleaks:allow
                                 "OTEL_F": "https://h.example.com/v1?x=1#section=top", "OTEL_G": "a=b;c=d", "OTEL_H": "a=b;api_key=semi-fake-9"})
ck("split_telemetry holds a #fragment pair, a bare user:pw@host as a pair's value or after a ;, a secret pair after a ;, an encoded name and user@host",
   sorted(held2) == ["OTEL_A", "OTEL_B", "OTEL_C", "OTEL_D", "OTEL_E", "OTEL_H"], str(sorted(held2)))
ck("split_telemetry does not hold a value only because a secret word is in its host, or a fragment without one",
   sorted(plain2) == ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_F", "OTEL_G"], str(sorted(plain2)))
ck("split_telemetry still lets through a query without a secret name, plain resource attributes and the client certificate path",
   sorted(plain) == ["OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "OTEL_METRICS_EXPORTER",
                     "OTEL_RESOURCE_ATTRIBUTES_PLAIN"], str(sorted(plain)))
person({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": QUERY, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": BARE,
                "OTEL_RESOURCE_ATTRIBUTES": RES, "OTEL_METRICS_EXPORTER": "otlp"}})
err, seen, crew_home = start()
extra = seen.get("kw", {}).get("env_extra", {})
ck("those values reach the process", err is None and "qk-fake-1" in extra.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
   and "pw-fake-2" in extra.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "") and "ra-fake-3" in extra.get("OTEL_RESOURCE_ATTRIBUTES", ""), str(err))
ck("and no file under the crew home holds them, the settings file included",
   not [f for f in files_under(crew_home) if any(x in f.read_bytes().decode("utf-8", "replace") for x in ("qk-fake-1", "pw-fake-2", "ra-fake-3"))]
   and "OTEL_METRICS_EXPORTER" in settings_in(crew_home)["env"], "")
ck("the stderr note names each held variable and never a value", "OTEL_EXPORTER_OTLP_ENDPOINT" in seen["stderr"]
   and "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in seen["stderr"] and "OTEL_RESOURCE_ATTRIBUTES" in seen["stderr"]
   and "OTEL_METRICS_EXPORTER" not in seen["stderr"]
   and not [x for x in ("qk-fake-1", "pw-fake-2", "ra-fake-3") if x in seen["stderr"]], seen["stderr"])
PLAIN_ONLY = {"env": {"OTEL_METRICS_EXPORTER": "otlp"}}
person(PLAIN_ONLY)
ck("with nothing held there is no note", "sends its telemetry without" not in start()[1]["stderr"])

person({"env": {"OTEL_EXPORTER_OTLP_CLIENT_KEY": "/etc/otel/client.key", "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE": "/etc/otel/client.crt"}})
err, seen, crew_home = start()
fenv = settings_in(crew_home)["env"]
ck("an mTLS client key path is held back and said so; the certificate path goes in the file",
   err is None and "OTEL_EXPORTER_OTLP_CLIENT_KEY" not in fenv and fenv.get("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE") == "/etc/otel/client.crt"
   and "OTEL_EXPORTER_OTLP_CLIENT_KEY" in seen["stderr"] and "client key variable holds a file path" in seen["stderr"], seen["stderr"])

# the two telemetry switches come from the person's env too, and no other CLAUDE_CODE_ variable does
person({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1", "CLAUDE_CODE_OTHER_SWITCH": "1"}})
err, seen, crew_home = start()
extra = seen.get("kw", {}).get("env_extra", {})
fenv = settings_in(crew_home)["env"]
ck("the person's CLAUDE_CODE_ENABLE_TELEMETRY and _ENHANCED_TELEMETRY_BETA reach the process and the settings file",
   err is None and extra.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1" and extra.get("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA") == "1"
   and fenv.get("CLAUDE_CODE_ENABLE_TELEMETRY") == "1" and fenv.get("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA") == "1", str((err, sorted(extra), sorted(fenv))))
ck("another CLAUDE_CODE_ variable of the person's does not", "CLAUDE_CODE_OTHER_SWITCH" not in extra and "CLAUDE_CODE_OTHER_SWITCH" not in fenv, str(sorted(extra)))
ck("a spec's switch beats the person's", L.telemetry_env({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}, {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"})
   == {"CLAUDE_CODE_ENABLE_TELEMETRY": "0"})
person(PERSON)

person({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:pw-fake-9@creds.example.com:4318"}})
err, seen, crew_home = start()
body = (crew_home / "stations" / "crew-reviewer.settings.json").read_text(encoding="utf-8")
ck("an endpoint with credentials in its address is handed to the process and written to no file",
   err is None and "pw-fake-9" in seen.get("kw", {}).get("env_extra", {}).get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
   and not [f for f in files_under(crew_home) if "pw-fake-9" in f.read_bytes().decode("utf-8", "replace")], str(err))
person(PERSON)

_n[0] += 1
tmp = SCRATCH / f"t{_n[0]}"
tmp.mkdir()
LS.make_crew(tmp, {"token": FAKE_TOKEN})
start(tmp=tmp)                                    # Tares up: the copy is kept
copy = tmp / "crew" / "launch" / "crew-reviewer.json"
doc = json.loads(copy.read_text(encoding="utf-8"))
doc["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://old-copy.example.com"      # a copy kept by an older engine
copy.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
err, seen, crew_home = start(tmp=tmp, tares_up=False)
extra = seen.get("kw", {}).get("env_extra", {})
body = (crew_home / "stations" / "crew-reviewer.settings.json").read_text(encoding="utf-8")
ck("a start from a kept copy that has OTEL_* gives it to the process and to the settings file",
   err is None and extra.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "https://old-copy.example.com"
   and json.loads(body)["env"].get("OTEL_EXPORTER_OTLP_ENDPOINT") == "https://old-copy.example.com"
   and "OTEL_EXPORTER_OTLP_HEADERS" not in body and SECRET not in body, str((err, sorted(extra))))
ck("a start from a kept copy that has OTEL_* strips it from the file on disk",
   "old-copy.example.com" not in copy.read_text(encoding="utf-8") and "OTEL_" not in copy.read_text(encoding="utf-8"), "")
ck("the lean flags and the connectors key are there from a kept copy too",
   "--no-chrome" in seen.get("args", []) and json.loads(body).get("disableClaudeAiConnectors") is True, str(seen.get("args")))

# launch-plan prints the plan: never the telemetry values
_n[0] += 1
tmp = SCRATCH / f"t{_n[0]}"
tmp.mkdir()
LS.make_crew(tmp, {"token": FAKE_TOKEN})
os.environ["TARES_PLUGIN_DIR"] = str(LS.make_kit(tmp))
tares = LS.FakeTares({"projects": []})
tares.__enter__()
c = TaresClient(CrewPaths.at(tmp / "crew"), env={**os.environ, "TARESD_URL": tares.url})
try:
    c.prime()
    shown = stations.launch_plan(c, c.paths, ["crew-reviewer", "reviewer", "-", "-", "-"])
    plan = L.launch_plan(c, "crew-reviewer", "reviewer", "", "", "")
finally:
    c.close()
    tares.__exit__(None, None, None)
    os.environ.pop("TARES_PLUGIN_DIR", None)
ck("`launch-plan` shows the lean flags and no OTEL_ name or value", "--no-chrome" in shown and "OTEL_" not in shown and "otel" not in shown
   and SECRET not in shown and "person-collector" not in shown, shown[:200])
ck("the plan itself carries the person's OTEL_* for launch to hand on", plan["otel"].get("OTEL_METRICS_EXPORTER") == "otlp", str(plan.get("otel")))
ck("the plan's file part has no header variable", "OTEL_EXPORTER_OTLP_HEADERS" in plan["otel"]
   and "OTEL_EXPORTER_OTLP_HEADERS" not in plan["otel_file"] and "OTEL_METRICS_EXPORTER" in plan["otel_file"], str(sorted(plan["otel_file"])))

# ---------------------------------------------------------------------------------------------
print("c2. auto-memory is off for every station but the orchestrator")
_n[0] += 1
tmp = SCRATCH / f"t{_n[0]}"
tmp.mkdir()
LS.make_crew(tmp, {"token": FAKE_TOKEN})
os.environ["TARES_PLUGIN_DIR"] = str(LS.make_kit(tmp))
tares = LS.FakeTares({"projects": []})
tares.__enter__()
c = TaresClient(CrewPaths.at(tmp / "crew"), env={**os.environ, "TARESD_URL": tares.url})
mem = {}
try:
    c.prime()
    for label, station, role, parent in (("orchestrator", "crew-orchestrator", "orchestrator", ""),
                                         ("reviewer", "crew-reviewer", "reviewer", ""),
                                         ("releaser", "crew-releaser", "releaser", ""),
                                         ("builder", "shop-build-m1", "builder", "crew-orchestrator"),
                                         ("helper of the orchestrator", "crew-orchestrator-h1", "helper", "crew-orchestrator"),
                                         ("helper of a builder", "shop-build-m1-h1", "helper", "shop-build-m1")):
        mem[label] = L.launch_plan(c, station, role, parent, "", "")["settings"]
finally:
    c.close()
    tares.__exit__(None, None, None)
    os.environ.pop("TARES_PLUGIN_DIR", None)
ck("the orchestrator keeps auto-memory: the key is not set", "autoMemoryEnabled" not in mem["orchestrator"], str(sorted(mem["orchestrator"])))
ck("a helper of the orchestrator keeps it too", "autoMemoryEnabled" not in mem["helper of the orchestrator"], "")
for label in ("reviewer", "releaser", "builder", "helper of a builder"):
    ck(f"{label}: autoMemoryEnabled is false", mem[label].get("autoMemoryEnabled") is False, str(mem[label].get("autoMemoryEnabled")))

# ---------------------------------------------------------------------------------------------
print("d. the settings file is for its owner only")
if os.name == "nt":
    print("  skip the mode checks on Windows (the file's access list is the owner's by default)")
else:
    err, seen, crew_home = start()
    f = crew_home / "stations" / "crew-reviewer.settings.json"
    ck("a new settings file has mode 0600", (f.stat().st_mode & 0o777) == 0o600, oct(f.stat().st_mode & 0o777))
    f.chmod(0o644)
    start(tmp=crew_home.parent)
    ck("a settings file that was open to others is 0600 again after the next start", (f.stat().st_mode & 0o777) == 0o600, oct(f.stat().st_mode & 0o777))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

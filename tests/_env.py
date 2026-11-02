"""A clean environment for the processes tests start, and for plugin or Tares code they run
in-process.

A test run inside a tares-factory crew station inherits the station's settings
(FACTORY_STATION, TARES_FACTORY_URL, ...) and the person's saved login ($TARES_HOME/login.json).
Passed on, they make the plugin's shipper report the test's fake sessions to the station's real
Tares as that station. clean_env() and scrub_os_environ() drop them and point TARES_HOME at an
empty temp dir.

Use: sys.path.insert(0, os.path.dirname(__file__)); from _env import clean_env
"""
import os
import tempfile

LEAK_PREFIXES = ("FACTORY_", "TARES_FACTORY_", "CLAUDE_PLUGIN_OPTION_")
LEAK_NAMES = ("TARESD_URL", "TARES_URL", "TARES_AUTH_TOKEN", "TARES_PROJECT", "TARES_HOME")


def _leaks(key: str) -> bool:
    return key in LEAK_NAMES or key.startswith(LEAK_PREFIXES)


def _temp_home() -> str:
    return tempfile.mkdtemp(prefix="tares-test-home-")


def clean_env(**overrides) -> dict:
    """os.environ without the station and login settings, TARES_HOME set to a new empty temp
    dir, then `overrides` applied (a value of None removes the key)."""
    env = {k: v for k, v in os.environ.items() if not _leaks(k)}
    env["TARES_HOME"] = _temp_home()
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def scrub_os_environ() -> None:
    """The same removal applied to os.environ itself, with TARES_HOME set to an empty temp dir,
    for tests that run plugin or Tares code in-process."""
    for k in [k for k in os.environ if _leaks(k)]:
        del os.environ[k]
    os.environ["TARES_HOME"] = _temp_home()

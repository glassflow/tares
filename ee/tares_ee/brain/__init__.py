# Tares enterprise licence, see ee/LICENSE
"""The brain: a cell asks a central lore about the code of the repos it connected (TR-1216).

`lore.py` is the one client every brain call goes through. `repos.py` keeps the connected repos and
their routes. MCP tools build on the client too.
"""

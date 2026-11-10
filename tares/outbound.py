"""The rule for an address the cell is asked to call (TR-842).

One rule for every caller that sends a request to an address someone typed in: a webhook a project
key subscribes, and the connection test of an integration. The address may not point inside the
cell's network (loopback, private ranges, link-local such as cloud metadata). Setting
TARES_WEBHOOK_ALLOW_PRIVATE=1 allows it, for a cell whose services live on its own network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit


class AddressRefused(ValueError):
    """`reason` is "unresolved" (the name did not resolve) or "internal" (it points inside)."""

    def __init__(self, host: str, reason: str):
        super().__init__(f"cannot resolve {host!r}" if reason == "unresolved"
                         else f"{host} is an internal address; use a public URL")
        self.host = host
        self.reason = reason


def allow_private() -> bool:
    return os.environ.get("TARES_WEBHOOK_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def valid_url(url: str) -> str:
    """The url trimmed. Raises ValueError unless it is an http or https address with a host."""
    url = (url or "").strip()
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("the address must be an http or https URL")
    return url


async def refuse_internal(url: str) -> None:
    """Raise AddressRefused when `url`'s host does not resolve or any address it resolves to is
    internal. Returns at once when TARES_WEBHOOK_ALLOW_PRIVATE allows internal addresses."""
    if allow_private():
        return
    host = urlsplit(url).hostname or ""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        raise AddressRefused(host, "unresolved") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise AddressRefused(host, "internal")

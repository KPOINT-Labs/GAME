"""
Guards against incompatible pinned dependency versions.

Most tests mock HTTP, so an httpx/httpcore mismatch (for example httpx 0.25
with httpcore 0.16, which rejects httpx's ``socket_options`` argument) would
otherwise only show up at runtime. ``refresh_access_token`` builds an
``httpx.AsyncClient`` on every call.
"""

import httpx


def test_httpx_sync_client_can_be_created():
    with httpx.Client():
        pass


async def test_httpx_async_client_can_be_created():
    async with httpx.AsyncClient():
        pass

"""Serialize an owned desktop MCP session; replace failed sessions without replay."""
from __future__ import annotations
import asyncio
import logging

CLEANUP_TIMEOUT_SECONDS = 5.0


class DesktopSession:
    def __init__(self, factory):
        self._factory = factory
        self._client = None
        self._lock = asyncio.Lock()
        self._broken = False

    async def __aenter__(self):
        await self._lock.acquire()
        try:
            if self._client is None:
                self._client = self._factory()
                await self._client.__aenter__()
            return self
        except BaseException:
            try:
                await self._dispose()
            finally:
                self._lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None or self._broken:
                await self._dispose()
        finally:
            self._lock.release()
        return False

    async def _dispose(self):
        client, self._client = self._client, None
        self._broken = False
        if client is None:
            return
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                closer = getattr(client, 'close', None)
                if closer is not None:
                    await closer()
                else:
                    await client.__aexit__(None, None, None)
        except Exception:
            logging.getLogger('borg.audit').warning('desktop_session_cleanup_incomplete')
        finally:
            # Client.close can fail while unwinding its session, before reaching
            # transport.close. Always finalize the owned transport separately.
            # StdioTransport.close is idempotent. Give this phase its own bound
            # so an exhausted client-close deadline cannot skip transport cleanup.
            transport = getattr(client, 'transport', None)
            close_transport = getattr(transport, 'close', None)
            if close_transport is not None:
                try:
                    async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                        await close_transport()
                except Exception:
                    logging.getLogger('borg.audit').warning('desktop_transport_cleanup_incomplete')

    async def close(self):
        async with self._lock:
            await self._dispose()

    async def list_tools(self, *args, **kwargs):
        return await self._client.list_tools(*args, **kwargs)

    async def call_tool_mcp(self, *args, **kwargs):
        result = await self._client.call_tool_mcp(*args, **kwargs)
        if result.isError:
            text = '\n'.join(getattr(part, 'text', '') for part in result.content).casefold()
            # A native protocol refusal is returned unchanged. Retire the bad
            # session after this request; a later owner request gets a new one.
            self._broken = any(marker in text for marker in (
                'bridge requests require a successful protocol handshake',
                'connection closed', 'transport closed'))
        return result

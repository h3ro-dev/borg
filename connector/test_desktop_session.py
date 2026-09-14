"""Pure in-memory session tests. No desktop programs or services are invoked."""
import unittest
from types import SimpleNamespace
from desktop_session import DesktopSession

class Stub:
    def __init__(self, error=False):
        self.error = error
        self.calls = 0
        self.opens = 0
        self.closes = 0
    async def __aenter__(self):
        self.opens += 1
        return self
    async def close(self):
        self.closes += 1
    async def call_tool_mcp(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError('synthetic transport failure')
        return SimpleNamespace(isError=False, content=[])
    async def list_tools(self):
        return []

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_connection_is_reused_and_closed(self):
        stub = Stub()
        session = DesktopSession(lambda: stub)
        for _ in range(3):
            async with session:
                await session.call_tool_mcp('permissions', {})
        self.assertEqual(stub.calls, 3)
        self.assertEqual(stub.opens, 1)
        self.assertEqual(stub.closes, 0)
        await session.close()
        await session.close()
        self.assertEqual(stub.closes, 1)

    async def test_failure_is_not_replayed_and_next_request_recovers(self):
        first, second = Stub(error=True), Stub()
        clients = iter([first, second])
        session = DesktopSession(lambda: next(clients))
        with self.assertRaises(RuntimeError):
            async with session:
                await session.call_tool_mcp('permissions', {})
        self.assertEqual(first.calls, 1)
        self.assertEqual(first.closes, 1)
        self.assertEqual(second.calls, 0)
        async with session:
            await session.call_tool_mcp('permissions', {})
        self.assertEqual(second.calls, 1)
        await session.close()

    async def test_handshake_refusal_is_returned_once(self):
        refusal = SimpleNamespace(isError=True, content=[SimpleNamespace(text='Bridge requests require a successful protocol handshake')])
        class Refusing(Stub):
            async def call_tool_mcp(self, *args, **kwargs):
                self.calls += 1
                return refusal
        first, second = Refusing(), Stub()
        clients = iter([first, second])
        session = DesktopSession(lambda: next(clients))
        async with session:
            self.assertIs(await session.call_tool_mcp('permissions', {}), refusal)
        self.assertEqual(first.calls, 1)
        self.assertEqual(first.closes, 1)
        self.assertEqual(second.calls, 0)
        async with session:
            await session.call_tool_mcp('permissions', {})
        await session.close()

    async def test_ordinary_refusal_does_not_reconnect(self):
        refusal = SimpleNamespace(isError=True, content=[SimpleNamespace(text='Exact target unavailable')])
        class Refusing(Stub):
            async def call_tool_mcp(self, *args, **kwargs):
                self.calls += 1
                return refusal
        stub = Refusing()
        session = DesktopSession(lambda: stub)
        async with session:
            self.assertIs(await session.call_tool_mcp('permissions', {}), refusal)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(stub.closes, 0)
        await session.close()

    async def test_concurrent_requests_are_serialized(self):
        import asyncio
        stub = Stub()
        session = DesktopSession(lambda: stub)
        active = 0
        peak = 0
        async def request():
            nonlocal active, peak
            async with session:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                await session.call_tool_mcp('permissions', {})
                active -= 1
        await asyncio.gather(*(request() for _ in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual(stub.calls, 5)
        self.assertEqual(stub.opens, 1)
        await session.close()

    async def test_cancelled_request_releases_session(self):
        import asyncio
        first, second = Stub(), Stub()
        clients = iter([first, second])
        session = DesktopSession(lambda: next(clients))
        started = asyncio.Event()
        async def request():
            async with session:
                started.set()
                await asyncio.sleep(10)
        task = asyncio.create_task(request())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(1):
            async with session:
                await session.call_tool_mcp('permissions', {})
        self.assertEqual(first.closes, 1)
        self.assertEqual(second.calls, 1)
        await session.close()

if __name__ == '__main__':
    unittest.main()

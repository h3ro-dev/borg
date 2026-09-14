"""Synthetic cleanup regressions; no desktop processes or services are started."""
import asyncio
import unittest

from desktop_session import DesktopSession


class Transport:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class Client:
    def __init__(self, failing_close=False):
        self.transport = Transport()
        self.failing_close = failing_close
        self.close_calls = 0

    async def __aenter__(self):
        return self

    async def close(self):
        self.close_calls += 1
        if self.failing_close:
            raise RuntimeError('synthetic client-close failure')
        await self.transport.close()


class FailedCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_close_failure_still_closes_transport(self):
        client = Client(failing_close=True)
        session = DesktopSession(lambda: client)
        async with session:
            pass
        with self.assertLogs('borg.audit', level='WARNING'):
            await session.close()
        self.assertEqual(client.close_calls, 1)
        self.assertGreaterEqual(client.transport.closed, 1)

    async def test_next_request_is_not_blocked_by_failed_close(self):
        first, second = Client(failing_close=True), Client()
        clients = iter([first, second])
        session = DesktopSession(lambda: next(clients))
        async with session:
            pass
        with self.assertLogs('borg.audit', level='WARNING'):
            await session.close()
        async with asyncio.timeout(1):
            async with session:
                pass
        await session.close()
        self.assertGreaterEqual(first.transport.closed, 1)
        self.assertGreaterEqual(second.transport.closed, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Synthetic cleanup tests: no native apps, credentials, or services."""
import asyncio
import unittest
from unittest.mock import patch
import desktop_session as implementation


class Transport:
    def __init__(self, error=False):
        self.closed = 0
        self.error = error

    async def close(self):
        self.closed += 1
        if self.error:
            raise RuntimeError('synthetic transport-close error')


class Client:
    def __init__(self, mode='normal', transport_error=False):
        self.transport = Transport(transport_error)
        self.mode = mode
        self.close_calls = 0

    async def __aenter__(self):
        if self.mode == 'open_error':
            raise RuntimeError('synthetic open error')
        return self

    async def close(self):
        self.close_calls += 1
        if self.mode == 'close_error':
            raise RuntimeError('synthetic client-close error')
        if self.mode == 'timeout':
            await asyncio.sleep(60)
        await self.transport.close()


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_error_still_closes_transport(self):
        client = Client('close_error')
        session = implementation.DesktopSession(lambda: client)
        async with session:
            pass
        with self.assertLogs('borg.audit', level='WARNING'):
            await session.close()
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(client.transport.closed, 1)

    async def test_client_timeout_has_separate_transport_cleanup_budget(self):
        client = Client('timeout')
        session = implementation.DesktopSession(lambda: client)
        async with session:
            pass
        with patch.object(implementation, 'CLEANUP_TIMEOUT_SECONDS', 0.03):
            with self.assertLogs('borg.audit', level='WARNING'):
                async with asyncio.timeout(1):
                    await session.close()
        self.assertEqual(client.transport.closed, 1)

    async def test_transport_error_does_not_lock_next_request(self):
        first, second = Client('close_error', transport_error=True), Client()
        clients = iter([first, second])
        session = implementation.DesktopSession(lambda: next(clients))
        async with session:
            pass
        with self.assertLogs('borg.audit', level='WARNING'):
            await session.close()
        async with asyncio.timeout(1):
            async with session:
                pass
            await session.close()
        self.assertEqual(first.transport.closed, 1)
        self.assertGreaterEqual(second.transport.closed, 1)

    async def test_open_failure_releases_resources_and_lock(self):
        first, second = Client('open_error'), Client()
        clients = iter([first, second])
        session = implementation.DesktopSession(lambda: next(clients))
        with self.assertRaisesRegex(RuntimeError, 'synthetic open error'):
            async with session:
                self.fail('body must not execute after failed open')
        self.assertGreaterEqual(first.transport.closed, 1)
        async with asyncio.timeout(1):
            async with session:
                pass
            await session.close()

    async def test_cancellation_during_client_close_finalizes_transport(self):
        client = Client('timeout')
        session = implementation.DesktopSession(lambda: client)
        async with session:
            pass
        task = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.transport.closed, 1)
        async with asyncio.timeout(1):
            await session.close()

    async def test_repeated_shutdown_does_not_reopen_client(self):
        client = Client()
        session = implementation.DesktopSession(lambda: client)
        async with session:
            pass
        await session.close()
        count = client.transport.closed
        await session.close()
        self.assertEqual(client.transport.closed, count)
        self.assertEqual(client.close_calls, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

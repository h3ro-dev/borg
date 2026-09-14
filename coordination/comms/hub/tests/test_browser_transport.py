import io
import json
from pathlib import Path
import tempfile
import threading
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from comms.hub import cli
from comms.hub.adapters import DurableClient
from comms.hub.client import Client, ClientError, TransportUnavailable
from comms.hub.service import HubService, HubError

OPS = ['browser.' + x for x in ('open', 'act', 'renew', 'close', 'status', 'stop', 'resume', 'reconcile')]

class BrowserTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credential = self.root / 'credential'
        self.credential.write_text('synthetic-token')
        self.gateway = Mock()
        self.gateway.call.return_value = {'ok': True}
        self.store = Mock()
        self.store.call.return_value = {'ordinary': True}

    def client(self, endpoint='http://127.0.0.1:1'):
        return Client(endpoint=endpoint, credential_file=self.credential, outbox_path=self.root / 'outbox.json')

    def service(self, factory=None):
        return HubService(store=self.store, credentials={'authenticated-actor': 'synthetic-token'},
                          browser_gateway_factory=factory)

    def server(self, service):
        server = service.make_server(port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(cleanup)
        return 'http://127.0.0.1:' + str(server.server_port)

    def test_authenticated_http_all_operations(self):
        factory = Mock(return_value=self.gateway)
        service = self.service(factory)
        client = self.client(self.server(service))
        for op in OPS:
            params = {'work_id': 'w', 'attempt_id': 'a'} if op == 'browser.open' else {'lease_id': 'l', 'generation': 1}
            self.assertEqual(client.call(op, params, 'unchanged-id'), {'ok': True})
            self.gateway.call.assert_called_with('authenticated-actor', op, params, 'unchanged-id')
        factory.assert_called_once_with(service)
        self.store.call.assert_not_called()
        self.assertEqual(client.outbox.count(), 0)
        self.assertFalse(client.outbox.path.exists())

    def test_wrong_token_and_actor_injection(self):
        endpoint = self.server(self.service(lambda _: self.gateway))
        for token, extra, params in [('wrong', {}, {}), ('synthetic-token', {'actor': 'other'}, {}),
                                     ('synthetic-token', {}, {'actor': 'other'})]:
            payload = {'operation': 'browser.open', 'params': params, 'request_id': 'r', **extra}
            req = Request(endpoint + '/v1/call', data=json.dumps(payload).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            error.exception.close()
        self.gateway.call.assert_not_called()

    def test_network_failure_never_queues_or_retries(self):
        client = self.client()
        with patch('comms.hub.client.urlopen', side_effect=URLError('sensitive')) as post:
            with self.assertRaises(TransportUnavailable) as error:
                client.call('browser.act', {'text': 'private'})
            self.assertEqual(post.call_count, 1)
        self.assertIn('uncertain', error.exception.message)
        self.assertNotIn('sensitive', str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertTrue(error.exception.request_id)
        self.assertEqual(client.outbox.count(), 0)
        self.assertFalse(client.outbox.path.exists())

    def test_enqueue_forbidden_and_stale_entry_not_dispatched(self):
        client = self.client()
        for enqueue in (client.enqueue, client.outbox.enqueue):
            with self.assertRaises(ClientError) as error:
                enqueue('browser.act', {'text': 'private'}, 'r')
            self.assertEqual(error.exception.code, 'browser_sync_required')
        client.outbox.path.write_text(json.dumps({'version': 1, 'entries': [
            {'request_id': 'stale', 'operation': 'browser.act', 'params': {'text': 'private'}}]}))
        with patch.object(client, '_post') as post:
            report = client.flush()
        post.assert_not_called()
        self.assertEqual(report['failed'][0]['error']['code'], 'browser_sync_required')
        self.assertEqual(client.outbox.count(), 0)
        self.assertNotIn('private', client.outbox.path.read_text())

    def test_nonbrowser_outbox_still_works(self):
        client = self.client()
        with patch.object(client, '_post', side_effect=TransportUnavailable('offline', 'offline')):
            self.assertTrue(client.call('messages.send', {'body': 'ordinary'}, 'ordinary-id')['queued'])
        self.assertEqual(client.outbox.count(), 1)
        with patch.object(client, '_post', return_value={'ok': True}):
            report = client.flush(request_ids={'ordinary-id'})
            # Existing backoff semantics are preserved; force due for this recovery check.
            if not report['sent']:
                with patch('comms.hub.client.time.time', return_value=10**12):
                    report = client.flush()
        self.assertEqual(report['sent'][0]['request_id'], 'ordinary-id')
        self.assertEqual(client.outbox.count(), 0)

    def test_adapters_and_cli_mcp_are_synchronous(self):
        transport = Mock()
        transport.call_sync.return_value = {'ok': True}
        self.assertEqual(DurableClient(transport).call('browser.open', {}, 'r'), {'ok': True})
        transport.call.assert_not_called()
        transport.call_sync.assert_called_once_with('browser.open', {}, request_id='r')
        transport.call_sync.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            DurableClient(transport).call('browser.open', {}, 'r')
        transport.enqueue.assert_not_called()
        client = self.client(self.server(self.service(lambda _: self.gateway)))
        with patch.object(client, 'call_sync', wraps=client.call_sync) as sync:
            direct = cli._handle_direct({'operation': 'browser.open', 'params': {}, 'request_id': 'direct'}, client)
            mcp = cli._handle_mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'inbox_call', 'arguments': {'operation': 'browser.open', 'params': {}, 'request_id': 'mcp'}}}, client)
            self.assertTrue(direct['ok'])
            self.assertNotIn('error', mcp)
            self.assertEqual(sync.call_count, 2)
        schema = cli._tool_schema()
        for op in OPS:
            self.assertIn(op, schema['properties']['operation']['description'])

    def test_disabled_and_lifecycle(self):
        service = self.service()
        with self.assertRaises(HubError) as error:
            service.call('actor', 'browser.open', {}, 'r')
        self.assertEqual(error.exception.code, 'browser_unavailable')
        factory = Mock(return_value=self.gateway)
        service = self.service(factory)
        threads = [threading.Thread(target=service.call, args=('actor', 'browser.status', {}, 'r')) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        factory.assert_called_once_with(service)
        service.close_browser_gateway()
        service.close_browser_gateway()
        self.gateway.close.assert_called_once()
        with self.assertRaises(HubError): service.call('actor', 'browser.open', {}, 'new')
        factory.assert_called_once()
        never = Mock()
        unused = self.service(never)
        unused.close_browser_gateway()
        with self.assertRaises(HubError): unused.call('actor', 'browser.open', {}, 'r')
        never.assert_not_called()

    def test_browser_timeout_preserves_other_calls(self):
        client = self.client()
        for op, timeout in [('browser.status', 120.0), ('agents.list', 10.0)]:
            with patch('comms.hub.client.urlopen', side_effect=URLError('offline')) as post:
                with self.assertRaises(TransportUnavailable): client.call(op, {})
                self.assertEqual(post.call_args.kwargs['timeout'], timeout)
        self.assertEqual(client.timeout, 10.0)

    def test_failure_sanitization_and_failed_factory_not_retried(self):
        class ResourceError(Exception):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code
        module = types.ModuleType('fleet_browser.store')
        module.ResourceError = ResourceError
        for exc, code in [(ResourceError('action_unknown', 'PRIVATE'), 'action_unknown'),
                          (ResourceError('PRIVATE', 'PRIVATE'), 'browser_unavailable'),
                          (ValueError('PRIVATE'), 'browser_unavailable')]:
            factory = Mock(side_effect=exc)
            service = self.service(factory)
            with patch.dict(sys.modules, {'fleet_browser.store': module}):
                with self.assertRaises(HubError) as error:
                    service.call('actor', 'browser.open', {}, 'r')
            self.assertEqual(error.exception.code, code)
            self.assertNotIn('PRIVATE', str(error.exception))
            self.assertIsNone(error.exception.__cause__)
            with self.assertRaises(HubError): service.call('actor', 'browser.open', {}, 'r')
            factory.assert_called_once()

    def test_gateway_failure_and_cleanup_retry_are_sanitized(self):
        service = self.service(lambda _: self.gateway)
        service.call('actor', 'browser.status', {}, 'r')
        self.gateway.call.side_effect = ValueError('PRIVATE')
        with self.assertRaises(HubError) as error: service.call('actor', 'browser.act', {}, 'r')
        self.assertEqual(error.exception.code, 'browser_unavailable')
        self.assertNotIn('PRIVATE', str(error.exception))
        self.gateway.close.side_effect = [ValueError('PRIVATE'), None]
        with self.assertRaises(HubError): service.close_browser_gateway()
        with self.assertRaises(HubError): service.call('actor', 'browser.open', {}, 'r')
        service.close_browser_gateway()
        self.assertEqual(self.gateway.close.call_count, 2)

    def test_initialization_close_race_and_active_call_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def factory(_):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.gateway
        service = self.service(factory)
        errors = []
        def call():
            try: service.call('actor', 'browser.open', {}, 'r')
            except HubError: pass  # Shutdown may win before gateway dispatch.
            except Exception as exc: errors.append(exc)
        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=service.close_browser_gateway)
        closer.start()
        release.set()
        caller.join(2); closer.join(2)
        self.assertFalse(caller.is_alive() or closer.is_alive())
        self.assertEqual(errors, [])
        self.gateway.close.assert_called_once()
        with self.assertRaises(HubError): service.call('actor', 'browser.open', {}, 'r')
        # close must reach the gateway while an act call is still waiting.
        active, cancelled = threading.Event(), threading.Event()
        gateway = Mock()
        def act(*args):
            active.set()
            if not cancelled.wait(2): raise RuntimeError('not cancelled')
            return {'cancelled': True}
        gateway.call.side_effect = act
        gateway.close.side_effect = cancelled.set
        service = self.service(lambda _: gateway)
        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(active.wait(2))
        service.close_browser_gateway()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])

    def test_request_bound_and_gateway_field_validation_boundary(self):
        client = self.client(self.server(self.service(lambda _: self.gateway)))
        with self.assertRaises(ClientError): client.call('browser.act', {'text': 'x' * (128 * 1024)})
        self.gateway.call.assert_not_called()
        # Inbox message body/limit policy must not replace gateway validation.
        params = {'body': 'x' * 17000, 'limit': 200}
        client.call('browser.act', params, 'r')
        self.gateway.call.assert_called_once_with('authenticated-actor', 'browser.act', params, 'r')
        with self.assertRaises(ClientError): client.call('browser.unknown', {}, 'r')
        self.assertEqual(client.outbox.count(), 0)

    def test_cli_command_and_stdio_reject_actor_injection(self):
        endpoint = self.server(self.service(lambda _: self.gateway))
        config = self.root / 'client.json'
        config.write_text(json.dumps({'endpoint': endpoint, 'credential_file': str(self.credential),
                                      'outbox_path': str(self.root / 'cli-outbox.json')}))
        params = self.root / 'params.json'
        params.write_text('{}')
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['call', '--config', str(config), '--operation', 'browser.open',
                                       '--params-file', str(params), '--request-id', 'cli-id']), 0)
        self.assertEqual(json.loads(output.getvalue()), {'ok': True})
        self.gateway.call.assert_called_once_with('authenticated-actor', 'browser.open', {}, 'cli-id')
        self.assertFalse((self.root / 'cli-outbox.json').exists())
        self.gateway.reset_mock()
        client = self.client(endpoint)
        direct = cli._handle_direct({'operation': 'browser.open', 'params': {}, 'request_id': 'r', 'actor': 'other'}, client)
        self.assertFalse(direct['ok'])
        mcp = cli._handle_mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'inbox_call', 'arguments': {'operation': 'browser.open', 'params': {},
                                              'request_id': 'r', 'actor': 'other'}}}, client)
        self.assertTrue(mcp['result']['isError'])
        self.gateway.call.assert_not_called()

if __name__ == '__main__': unittest.main()

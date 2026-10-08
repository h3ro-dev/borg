import contextlib
import importlib.machinery
import io
import json
from pathlib import Path
import unittest
from unittest import mock

shim = importlib.machinery.SourceFileLoader('test_schema_shim', str(Path(__file__).resolve().parents[1] / 'bin/ollama-schema-shim')).load_module()


class ShimReplyTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(shim.Handler)
        handler.path = '/v1/chat/completions'
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        handler.headers = {'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}')
        return handler

    def test_disconnect_logs_once_and_never_replies_twice(self):
        for failure in (BrokenPipeError, ConnectionResetError):
            handler = self.handler()
            handler.wfile.write.side_effect = failure()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                handler._reply(200, {'ok': True})
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            self.assertIn('client-gone code=200 path=/v1/chat/completions bytes=', stderr.getvalue())
            self.assertEqual(handler.send_response.call_count, 1)

    def test_native_errors_invalid_reason_and_empty_content_reply_502(self):
        for response in ({'error': 'out of memory'}, {'done_reason': 'load'},
                         {'done_reason': 'stop', 'message': {'content': '  '}}, {}):
            handler = self.handler()
            with mock.patch.object(shim, 'post_upstream', return_value=response), mock.patch.object(shim, 'tee'):
                handler.do_POST()
            handler.send_response.assert_called_once_with(502)
            payload = json.loads(handler.wfile.write.call_args.args[0])
            self.assertTrue(payload['error'])

    def test_stop_and_length_preserve_valid_content_and_reason(self):
        for reason in ('stop', 'length'):
            out = shim.to_openai({'done_reason': reason, 'message': {'content': '{}'}}, 'qwen')
            self.assertEqual(out['choices'][0]['finish_reason'], reason)
            self.assertEqual(out['choices'][0]['message']['content'], '{}')

    def test_upstream_timeout_precedes_feed_timeout(self):
        self.assertEqual(shim.post_upstream.__defaults__, (150,))


if __name__ == '__main__':
    unittest.main()

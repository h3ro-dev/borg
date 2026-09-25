"""Portable durable-job launch with synthetic commands and isolated stores."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from job_tools import JobStore, _job_command


class JobShellTests(unittest.TestCase):
    def test_preserves_zsh_login_behavior_when_available(self):
        with patch('job_tools.os.path.isfile', return_value=True), patch('job_tools.os.access', return_value=True):
            self.assertEqual(_job_command('printf unchanged'), ['/bin/zsh', '-lc', 'printf unchanged'])

    def test_posix_fallback_without_zsh(self):
        with patch('job_tools.os.path.isfile', side_effect=lambda path: path == '/bin/sh'), patch('job_tools.os.access', return_value=True):
            self.assertEqual(_job_command("printf '%s' 'two words'"),
                             ['/bin/sh', '-c', "printf '%s' 'two words'"])

    def test_nonexecutable_zsh_is_not_selected(self):
        with patch('job_tools.os.path.isfile', return_value=True), patch('job_tools.os.access', side_effect=lambda path, mode: path == '/bin/sh'):
            self.assertEqual(_job_command('true')[0], '/bin/sh')

    def test_missing_shell_refuses_before_job_artifacts_or_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            with patch('job_tools.os.path.isfile', return_value=False), patch('job_tools.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(ToolError, 'capability unavailable'):
                    store.start('true')
                spawn.assert_not_called()
            self.assertEqual(list(store.jobs.iterdir()), [])

    def test_real_posix_fallback_produces_durable_output(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            original = __import__('os').path.isfile
            try:
                with patch('job_tools.os.path.isfile', side_effect=lambda path: False if path == '/bin/zsh' else original(path)):
                    job = store.start("printf '%s' 'portable job'", timeout_ms=0)
                deadline = time.monotonic() + 5
                status = store.status(job['job_id'])
                while status['state'] == 'running' and time.monotonic() < deadline:
                    time.sleep(0.01)
                    status = store.status(job['job_id'])
                self.assertEqual(status['state'], 'succeeded')
                self.assertEqual(status['returncode'], 0)
                self.assertEqual(store.read_output(job['job_id'], stream='stdout')['output'], 'portable job')
                self.assertNotIn('command', status)
            finally:
                store.close_all()


if __name__ == '__main__':
    unittest.main()

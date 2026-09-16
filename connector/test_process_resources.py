import resource
import unittest
from unittest.mock import patch

from process_resources import configure_descriptor_limit, descriptor_status


class DescriptorTests(unittest.TestCase):
    def test_soft_limit_is_raised_without_changing_hard_limit(self):
        with patch('process_resources.resource.getrlimit', side_effect=[(256, 65536), (8192, 65536)]), \
             patch('process_resources.resource.setrlimit') as change, \
             patch('process_resources.os.listdir', return_value=['0', '1', '2', '3']):
            result = configure_descriptor_limit()
        change.assert_called_once_with(resource.RLIMIT_NOFILE, (8192, 65536))
        self.assertEqual(result['remaining_descriptors'], 8189)
        self.assertEqual(result['state'], 'AVAILABLE')

    def test_existing_larger_limit_is_preserved(self):
        with patch('process_resources.resource.getrlimit', return_value=(16384, 65536)), \
             patch('process_resources.resource.setrlimit') as change:
            configure_descriptor_limit()
        change.assert_not_called()

    def test_hard_limit_is_respected_and_pressure_visible(self):
        with patch('process_resources.resource.getrlimit', return_value=(256, 256)), \
             patch('process_resources.resource.setrlimit') as change, \
             patch('process_resources.os.listdir', return_value=['0', '1', '2', '3']):
            result = configure_descriptor_limit()
        change.assert_not_called()
        self.assertEqual(result['state'], 'DEGRADED')

    def test_permission_failure_keeps_native_limits_and_reports_error(self):
        with patch('process_resources.resource.getrlimit', return_value=(256, 65536)), \
             patch('process_resources.resource.setrlimit', side_effect=PermissionError), \
             patch('process_resources.os.listdir', side_effect=OSError):
            result = configure_descriptor_limit()
        self.assertEqual(result['configuration_error'], 'PermissionError')
        self.assertEqual(result['soft_limit'], 256)
        self.assertEqual(result['state'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()

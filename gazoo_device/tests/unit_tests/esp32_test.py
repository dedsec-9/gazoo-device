# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for esp32 module."""
from unittest import mock

from gazoo_device.auxiliary_devices import esp32
from gazoo_device.base_classes import espressif_esp32_device
from gazoo_device.tests.unit_tests.utils import fake_device_test_case
import immutabledict


_FAKE_DEVICE_ID = "esp32-detect"
_FAKE_DEVICE_ADDRESS = "/dev/bus/usb/001/002"
_ESP32_PERSISTENT_PROPERTIES = immutabledict.immutabledict({
    "os": "FreeRTOS",
    "platform": "ESP32",
    "serial_number": "FT2BSR6O",
    "name": "esp32_detect",
    "device_type": "esp32",
})


class ESP32DeviceTests(fake_device_test_case.FakeDeviceTestCase):
  """Unit tests for base class ESP32."""

  def setUp(self):
    super().setUp()
    self.setup_fake_device_requirements()
    self.device_config["persistent"]["console_port_name"] = _FAKE_DEVICE_ADDRESS
    self.uut = esp32.ESP32(self.mock_manager,
                           self.device_config,
                           log_directory=self.artifacts_directory)

  def test_001_esp32_attributes(self):
    """Verifies esp32 attributes."""
    self._test_get_detection_info(_FAKE_DEVICE_ADDRESS,
                                  esp32.ESP32,
                                  _ESP32_PERSISTENT_PROPERTIES)

  @mock.patch.object(espressif_esp32_device.os.path, "exists")
  def test_003_is_connected_true(self, mock_exists):
    """Verifies is_connected works as expected."""
    mock_exists.return_value = True
    self.assertTrue(esp32.ESP32.is_connected(self.device_config))


if __name__ == "__main__":
  fake_device_test_case.main()

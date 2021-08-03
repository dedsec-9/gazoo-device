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

"""Unit tests for device_power_default capability."""
from unittest import mock

from gazoo_device import errors
from gazoo_device import manager
from gazoo_device.auxiliary_devices import cambrionix
from gazoo_device.capabilities import device_power_default
from gazoo_device.switchboard import switchboard
from gazoo_device.tests.unit_tests.utils import unit_test_case


class DevicePowerDefaultTests(unit_test_case.UnitTestCase):
  """Unit tests for device_power_default capability."""

  def setUp(self):
    super().setUp()
    self.name = "test_device-1234"
    self.mock_manager = mock.MagicMock(spec=manager.Manager)
    self.mock_switchboard = mock.MagicMock(spec=switchboard.SwitchboardDefault)
    self.port_num = 3
    self.add_time_mocks()
    self.props = {
        "persistent": {
            "name": self.name
        },
        "optional": {
            "device_usb_hub_name": "cambrionix-1234",
            "device_usb_port": self.port_num
        }
    }
    self.mock_manager.create_device.return_value = mock.MagicMock(
        spec=cambrionix.Cambrionix)
    self.wait_for_bootup_complete = mock.MagicMock()
    self.uut = device_power_default.DevicePowerDefault(
        device_name=self.name,
        create_device_func=self.mock_manager.create_device,
        hub_type="cambrionix",
        props=self.props,
        settable=True,
        hub_name_prop="device_usb_hub_name",
        port_prop="device_usb_port",
        wait_for_bootup_complete_fn=self.wait_for_bootup_complete,
        switchboard_inst=self.mock_switchboard,
        change_triggers_reboot=False)

  def test_001_off(self):
    """Verifies device_power.off calls switch_power."""
    self.uut.off()
    self.uut._hub.switch_power.power_off.assert_called_once_with(self.port_num)
    self.mock_switchboard.close_all_transports.assert_called_once()

  def test_002_power_on(self):
    """Verifies device_power.on calls switch_power."""
    self.uut.on()
    self.uut._hub.switch_power.power_on.assert_called_once_with(self.port_num)
    self.mock_switchboard.open_all_transports.assert_called_once()

  def test_003_on_with_no_wait_true(self):
    """Verifies wait_for_boot_up_complete is skipped if no_wait is False."""
    self.uut.on(no_wait=True)
    self.uut._hub.switch_power.power_on.assert_called_once_with(self.port_num)
    self.wait_for_bootup_complete.assert_not_called()

  def test_004_power_cycle(self):
    """Verifies device_power.power_cycle calls off and on methods."""
    self.uut.cycle()
    self.uut._hub.switch_power.power_off.assert_called_once()
    self.uut._hub.switch_power.power_on.assert_called_once()
    self.wait_for_bootup_complete.assert_called_once()

  def test_005_off_change_triggers_reboot_true(self):
    """Verifies off calls switch_power and does not close transports."""
    self.uut._change_triggers_reboot = True
    self.uut.off()
    self.uut._hub.switch_power.power_off.assert_called_once_with(self.port_num)
    self.mock_switchboard.close_all_transports.assert_not_called()
    self.wait_for_bootup_complete.assert_called_once()

  def test_006_power_on_change_trigger_reboot_true(self):
    """Verifies on calls switch_power and does not open transports."""
    self.uut._change_triggers_reboot = True
    self.uut.on()
    self.uut._hub.switch_power.power_on.assert_called_once_with(self.port_num)
    self.mock_switchboard.open_all_transports.assert_not_called()
    self.wait_for_bootup_complete.assert_called_once()

  def test_007_missing_device_usb_hub_name_property(self):
    """Verifies capability raises a error if device_usb_hub_name is not set."""
    err_msg = (f"{self.name} properties device_usb_hub_name are unset. "
               "If device is connected to cambrionix, set them")
    self.props["optional"]["device_usb_hub_name"] = None

    self.uut = device_power_default.DevicePowerDefault(
        device_name=self.name,
        create_device_func=self.mock_manager.create_device,
        hub_type="cambrionix",
        props=self.props,
        settable=True,
        hub_name_prop="device_usb_hub_name",
        port_prop="device_usb_port",
        wait_for_bootup_complete_fn=self.wait_for_bootup_complete,
        switchboard_inst=self.mock_switchboard,
        change_triggers_reboot=False)
    with self.assertRaisesRegex(errors.DeviceError, err_msg):
      self.uut.health_check()

  def test_008_missing_manager(self):
    """Verifies capability raises a error if manager is not set."""
    err_msg = f"{self.name} failed to create cambrionix."
    self.mock_manager.create_device.side_effect = errors.DeviceError(
        "failed to create cambrionix")
    self.uut = device_power_default.DevicePowerDefault(
        device_name=self.name,
        create_device_func=self.mock_manager.create_device,
        hub_type="cambrionix",
        props=self.props,
        settable=True,
        hub_name_prop="device_usb_hub_name",
        port_prop="device_usb_port",
        wait_for_bootup_complete_fn=self.wait_for_bootup_complete,
        switchboard_inst=self.mock_switchboard,
        change_triggers_reboot=False)
    with self.assertRaisesRegex(errors.DeviceError, err_msg):
      self.uut.health_check()


if __name__ == "__main__":
  unit_test_case.main()
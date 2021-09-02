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

"""Pigweed RPC transport class."""
import fcntl
import queue
import select
import threading
import types
from typing import Any, Collection, Dict, Optional, Tuple
from gazoo_device import errors
from gazoo_device import gdm_logger
from gazoo_device.switchboard.transports import transport_base
import serial

# TODO(b/181734752): Remove conditional imports of Pigweed
try:
  # pylint: disable=g-import-not-at-top
  # pytype: disable=import-error
  import pw_rpc
  from pw_rpc import callback_client
  from pw_hdlc import rpc
  from pw_hdlc import decode
  from pw_protobuf_compiler import python_protos
  # pytype: enable=import-error
  PIGWEED_IMPORT = True
except ImportError:
  pw_rpc = None
  callback_client = None
  rpc = None
  decode = None
  python_protos = None
  PIGWEED_IMPORT = False

_STDOUT_ADDRESS = 1
_DEFAULT_ADDRESS = ord("R")
_JOIN_TIMEOUT_SEC = 1  # seconds
_SELECT_TIMEOUT_SEC = 0.1  # seconds
logger = gdm_logger.get_logger()


class PwHdlcRpcClient:
  """Pigweed HDLC RPC Client.

  Adapted from https://pigweed.googlesource.com/pigweed/pigweed/+/refs/heads/master/pw_hdlc/py/pw_hdlc/rpc.py. # pylint: disable=line-too-long
  """

  def __init__(self,
               serial_instance: serial.Serial,
               protobufs: Collection[types.ModuleType]):
    """Creates an RPC client configured to communicate using HDLC.

    Args:
      serial_instance: Serial interface instance.
      protobufs: Proto modules.
    """
    if not PIGWEED_IMPORT:
      raise errors.DependencyUnavailableError(
          "Pigweed python packages are not available in this environment.")

    protos = python_protos.Library.from_paths(protobufs)
    client_impl = callback_client.Impl()
    channels = rpc.default_channels(serial_instance.write)
    self._client = pw_rpc.Client.from_modules(client_impl,
                                              channels,
                                              protos.modules())
    self._serial = serial_instance
    self._stop_event = threading.Event()
    self._worker = None
    self.log_queue = queue.Queue()

  def is_alive(self) -> bool:
    """Returns true if the worker thread has started."""
    return self._worker is not None and self._worker.is_alive()

  def start(self) -> None:
    """Creates and starts the worker thread if it hasn't been created."""
    if self._stop_event.is_set():
      self._stop_event.clear()
    if self._worker is None:
      self._worker = threading.Thread(target=self.read_and_process_data)
      self._worker.start()

  def close(self) -> None:
    """Sets the threading event and joins the worker thread."""
    self._stop_event.set()
    if self._worker is not None:
      self._worker.join(timeout=_JOIN_TIMEOUT_SEC)
      if self._worker.is_alive():
        raise errors.DeviceError(
            f"The child thread failed to join after {_JOIN_TIMEOUT_SEC} seconds"
            )
      self._worker = None

  def rpcs(self, channel_id: Optional[int] = None) -> Any:
    """Returns object for accessing services on the specified channel.

    Args:
      channel_id: None or RPC channel id.

    Returns:
      RPC instance over the specificed channel.
    """
    if channel_id is None:
      return next(iter(self._client.channels())).rpcs
    return self._client.channel(channel_id).rpcs

  def read_and_process_data(self) -> None:
    """Continuously reads and handles HDLC frames."""
    decoder = decode.FrameDecoder()
    while not self._stop_event.is_set():
      readfds, _, _ = select.select([self._serial], [], [], _SELECT_TIMEOUT_SEC)
      for fd in readfds:
        try:
          data = fd.read()
        except Exception:  # pylint: disable=broad-except
          logger.exception(
              "Exception occurred when reading in PwHdlcRpcClient thread.")
          data = None
        if data:
          for frame in decoder.process_valid_frames(data):
            self._handle_frame(frame)

  def _handle_rpc_packet(self, frame: Any) -> None:
    """Handler for processing HDLC frame.

    Args:
      frame: HDLC frame packet.
    """
    if not self._client.process_packet(frame.data):
      logger.error(f"Packet not handled by RPC client: {frame.data}")

  def _push_to_log_queue(self, frame: Any) -> None:
    """Pushes the HDLC log in frame into the log queue.

    Args:
      frame: HDLC frame packet.
    """
    self.log_queue.put(frame.data + b"\n")

  def _handle_frame(self, frame: Any) -> None:
    """Private method for processing HDLC frame.

    Args:
      frame: HDLC frame packet.
    """
    try:
      if not frame.ok():
        return
      if frame.address == _DEFAULT_ADDRESS:
        self._handle_rpc_packet(frame)
      elif frame.address == _STDOUT_ADDRESS:
        self._push_to_log_queue(frame)
      else:
        logger.warning(f"Unhandled frame for address {frame.address}: {frame}")
    except Exception:  # pylint: disable=broad-except
      logger.exception("Exception occurred in PwHdlcRpcClient.")


class PigweedRPCTransport(transport_base.TransportBase):
  """Performs transport communication using the Pigweed RPC to end devices."""

  def __init__(self,
               comms_address: str,
               protobufs: Collection[types.ModuleType],
               baudrate: int,
               auto_reopen: bool = True,
               open_on_start: bool = True):
    super().__init__(
        auto_reopen=auto_reopen,
        open_on_start=open_on_start)
    self.comms_address = comms_address
    self._protobufs = protobufs
    self._serial = serial.Serial()
    self._serial.port = comms_address
    self._serial.baudrate = baudrate
    self._serial.timeout = 0.01
    self._hdlc_client = PwHdlcRpcClient(self._serial, protobufs)

  def is_open(self) -> bool:
    """Returns True if the PwRPC transport is connected to the target.

    Returns:
      True if transport is open, False otherwise.
    """
    return self._serial.isOpen() and self._hdlc_client.is_alive()

  def _close(self) -> None:
    """Closes the PwRPC transport."""
    self._hdlc_client.close()
    fcntl.flock(self._serial.fileno(), fcntl.LOCK_UN)
    self._serial.close()

  def _open(self) -> None:
    """Opens the PwRPC transport."""
    self._serial.open()
    fd = self._serial.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
    self._hdlc_client.start()

  def _read(self, size: int, timeout: float) -> bytes:
    """Returns Pigweed log from the HDLC channel 1.

    Args:
      size: Not used.
      timeout: Maximum seconds to wait to read bytes.

    Returns:
      bytes read from transport or None if no bytes were read
    """
    # Retrieving logs from queue doesn't support size configuration.
    del size
    try:
      return self._hdlc_client.log_queue.get(timeout=timeout)
    except queue.Empty:
      return b""

  def _write(self, data: str, timeout: Optional[float] = None) -> int:
    """Dummy method for Pigweed RPC.

    Declared to pass the inheritance check of TransportBase.

    Args:
      data: Not used.
      timeout: Not used.

    Returns:
      int: Not used.
    """
    return 0

  def rpc(self,
          service_name: str,
          event_name: str,
          **kwargs: Dict[str, Any]) -> Tuple[bool, bytes]:
    """RPC call to the Matter endpoint with given service and event name.

    Args:
      service_name: PwRPC service name.
      event_name: Event name in the given service instance.
      **kwargs: Arguments for the event method.

    Returns:
      (RPC ack value, RPC encoded payload in bytes)
    """
    client_channel = self._hdlc_client.rpcs().chip.rpc
    service = getattr(client_channel, service_name)
    event = getattr(service, event_name)
    ack, payload = event(**kwargs)
    return ack.ok(), payload.SerializeToString()

  def echo_rpc(self, msg: str) -> Tuple[bool, str]:
    """Calls the Echo RPC endpoint.

    Sends a message to the echo endpoint and returns the response back. Uses a
    different namespace (pw.rpc) than the rest of Matter endpoints (chip.rpc).
    Only used by the Pigweed Echo example app:
    https://github.com/project-chip/connectedhomeip/tree/master/examples/pigweed-app/esp32#chip-esp32-pigweed-example-application

    Args:
      msg: Echo message to send.

    Returns:
      (RPC ack value, Echo message)
    """
    client_channel = self._hdlc_client.rpcs().pw.rpc
    echo_service = client_channel.EchoService
    ack, payload = echo_service.Echo(msg=msg)
    return ack.ok(), payload.msg

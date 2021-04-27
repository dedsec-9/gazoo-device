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
import threading
import time
import types
from typing import Any, Callable, Dict, Iterable, List, Union
from gazoo_device import errors
from gazoo_device import gdm_logger
from gazoo_device.switchboard.transports import transport_base
import serial

# TODO(b/185956488): Remove conditional imports of Pigweed
try:
  # pylint: disable=g-import-not-at-top
  import pw_rpc
  from pw_rpc import callback_client
  from pw_hdlc import rpc
  from pw_hdlc import decode
  from pw_protobuf_compiler import python_protos
  PIGWEED_IMPORT = True
except ImportError:
  PIGWEED_IMPORT = False

_STDOUT_ADDRESS = 1
_DEFAULT_ADDRESS = ord("R")
_JOIN_TIMEOUT_SEC = 1  # seconds
logger = gdm_logger.get_logger()


class PwHdlcRpcClient:
  """Pigweed HDLC RPC Client.

  Adapted from https://pigweed.googlesource.com/pigweed/pigweed/+/refs/heads/master/pw_hdlc/py/pw_hdlc/rpc.py. # pylint: disable=line-too-long
  """

  def __init__(self,
               read: Callable[[], bytes],
               write: Callable[[bytes], int],
               paths_or_modules: Union[Iterable["python_protos.PathOrModule"],
                                       "python_protos.Library"]):
    """Creates an RPC client configured to communicate using HDLC.

    Args:
      read: Function that reads bytes; e.g serial_device.read.
      write: Function that writes bytes; e.g serial_device.write.
      paths_or_modules: Paths to .proto files or proto modules.
    """
    if not PIGWEED_IMPORT:
      raise errors.DependencyUnavailableError(
          "Pigweed python packages are not available in this environment.")

    if isinstance(paths_or_modules, python_protos.Library):
      self.protos = paths_or_modules
    else:
      self.protos = python_protos.Library.from_paths(paths_or_modules)

    client_impl = callback_client.Impl()
    channels = rpc.default_channels(write)
    self.client = pw_rpc.Client.from_modules(client_impl,
                                             channels,
                                             self.protos.modules())
    self.frame_handlers = {
        _DEFAULT_ADDRESS: self._handle_rpc_packet,
        _STDOUT_ADDRESS: lambda frame: rpc.write_to_file(frame.data)}
    self.read = read
    self._stop_event = threading.Event()
    self._worker = None

  def is_alive(self) -> bool:
    """Return true if the worker thread has started."""
    return self._worker is not None and self._worker.is_alive()

  def start(self):
    """Create and start the worker thread if it hasn't been created."""
    if self._stop_event.is_set():
      self._stop_event.clear()
    if self._worker is None:
      self._worker = threading.Thread(target=self.read_and_process_data,
                                      args=(self.read, self.frame_handlers))

      self._worker.start()

  def close(self):
    """Set the threading event and join the worker thread."""
    self._stop_event.set()
    if self._worker is not None:
      self._worker.join(timeout=_JOIN_TIMEOUT_SEC)
      if self._worker.is_alive():
        raise errors.DeviceError(
            "The child thread failed to join after {} seconds"
            .format(_JOIN_TIMEOUT_SEC))
      self._worker = None

  def rpcs(self, channel_id: int = None) -> Any:
    """Returns object for accessing services on the specified channel."""
    if channel_id is None:
      return next(iter(self.client.channels())).rpcs
    return self.client.channel(channel_id).rpcs

  def _handle_rpc_packet(self, frame: "Frame"):
    """Handler for processing HDLC frame."""
    if not self.client.process_packet(frame.data):
      logger.error("Packet not handled by RPC client: %s", frame.data)

  def read_and_process_data(self,
                            read: Callable[[], bytes],
                            frame_handlers: Any):
    """Continuously reads and handles HDLC frames."""
    decoder = decode.FrameDecoder()
    while not self._stop_event.is_set():
      try:
        data = read()
      except Exception:  # pylint: disable=broad-except
        logger.exception("Exception occurred when reading in "
                         "PwHdlcRpcClient thread.")
        data = None
      if data:
        for frame in decoder.process_valid_frames(data):
          self._handle_frame(frame, frame_handlers)
      else:
        # TODO(b/184718613): Refactor to non-blocking IO.
        time.sleep(0.01)

  def _handle_frame(self,
                    frame: "Frame",
                    frame_handlers: Dict[int, Callable[["Frame"], None]]):
    """Private method for processing HDLC frame.

    Args:
      frame: HDLC frame packet.
      frame_handlers: Handler for processing HDLC frame.
    """
    try:
      if not frame.ok():
        return
      try:
        frame_handlers[frame.address](frame)
      except KeyError:
        logger.warning("Unhandled frame for address %d: %s",
                       frame.address, frame)
    except Exception:  # pylint: disable=broad-except
      logger.exception("Exception occurred in PwHdlcRpcClient"
                       "frame handler %s.",
                       frame_handlers[frame.address].__name__)


class PigweedRPCTransport(transport_base.TransportBase):
  """Perform transport communication using the Pigweed RPC to end devices."""

  def __init__(self,
               comms_address: str,
               protobufs: List[types.ModuleType],
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
    self._hdlc_client = PwHdlcRpcClient(
        lambda: self._serial.read(4096),
        self._serial.write,
        protobufs)

  def is_open(self) -> bool:
    """Returns True if the PwRPC transport is connected to the target.

    Returns:
      True if transport is open, False otherwise.
    """
    return self._serial.isOpen() and self._hdlc_client.is_alive()

  def _close(self) -> None:
    """Closes the PwRPC transport."""
    fcntl.flock(self._serial.fileno(), fcntl.LOCK_UN)
    self._serial.close()
    self._hdlc_client.close()

  def _open(self) -> None:
    """Opens the PwRPC transport."""
    self._serial.open()
    self._hdlc_client.start()
    fd = self._serial.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

  def _read(self, size: int, timeout: float) -> str:
    """Returns bytes read up to max_bytes within timeout in seconds specified.

    Args:
      size: maximum number of bytes to read within timeout seconds.
      timeout: maximum seconds to wait to read bytes.

    Returns:
      bytes read from transport or None if no bytes were read
    """
    # TODO(b/182709921): Add PwRPC transport _read logic.
    return None

  def _write(self, data: str, timeout: float = None) -> int:
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
          **kwargs: Dict[str, Any]) -> bool:
    """RPC call to the specific endpoint with given service and event name.

    Args:
      service_name: PwRPC service name.
      event_name: Event name in the given service instance.
      **kwargs: Arguments for the event method.

    Returns:
      True if pwrpc succeeded, false otherwise.
    """
    client_channel = self._hdlc_client.rpcs().chip.rpc
    service = getattr(client_channel, service_name)
    event = getattr(service, event_name)
    ack, state = event(**kwargs)
    payload = state.on if event_name == "Get" else None
    return ack.ok(), payload
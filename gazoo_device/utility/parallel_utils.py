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

"""Utilities for interacting with devices in parallel.

Usage example:

  def my_custom_hello_world_function(manager_inst: manager.Manager,
                                     device_name: str, some_arg: int) -> str:
    '''Example function which executes a custom action on a device.

    Args:
      manager_inst: A Manager instance which can be used for device creation.
        A Manager instance is always provided. You do not need to pass one as an
        argument to the function in the CallSpec.
      device_name: Name of the device to use. Must be specified in the CallSpec.
      some_arg: An example of an argument. Must be specified in the CallSpec.
    '''
    device = manager_inst.create_device(device_name)
    try:
      shell_response = device.shell(f"echo 'Hello world {some_arg}'")
    finally:
      device.close()
    return shell_response

  call_specs = [
      parallel_utils.CallSpec(parallel_utils.reboot, "device-1234"),
      parallel_utils.CallSpec(parallel_utils.reboot, "device-2345",
                              no_wait=True, method="shell"),
      parallel_utils.CallSpec(parallel_utils.factory_reset, "device-3456"),
      parallel_utils.CallSpec(parallel_utils.upgrade, "device-4567",
                              build_number=1234, build_branch="1.0"),
      parallel_utils.CallSpec(parallel_utils.upgrade, "device-5678",
                              build_file="/some/file/path.zip"),
      parallel_utils.CallSpec(my_custom_hello_world_function, "device-6789",
                              1),
      parallel_utils.CallSpec(my_custom_hello_world_function, "device-7890",
                              some_arg=2),
  ]

  results, _ = parallel_utils.execute_concurrently(
      call_specs, timeout=300, raise_on_process_error=True)

Results are returned in the same order as call specs. For the hypothetical
example above, results would be
  [None, None, None, None, None, "Hello world 1", "Hello world 2"].

If you need more granular control over exceptions raised in parallel processes,
set raise_on_process_error to False. For example:

  def custom_function_raises(
      manager_inst: manager.Manager, some_arg: int) -> NoReturn:
    raise RuntimeError(f"Demo of exception handling {some_arg}")

  results, errors = parallel_utils.execute_concurrently(
      call_specs = [
          parallel_utils.CallSpec(custom_function_raises, 1),
          parallel_utils.CallSpec(custom_function_raises, 2),
      ],
      timeout=15,
      raise_on_process_error=False)

In this case results will be ["< No result received >",
                              "< No result received >"].
Errors will be [("RuntimeError", "Demo of exception handling 1"),
                ("RuntimeError", "Demo of exception handling 2")].

Logging behavior:
  Parallel process GDM logger logs are sent to the main process.
  Device logs (from device instances created in parallel processes) are stored
  in new individual device log files.
"""
import dataclasses
import multiprocessing
import os
import queue
import time
from typing import Any, Callable, List, Optional, Sequence, Tuple

from gazoo_device import errors
from gazoo_device import gdm_logger
from gazoo_device import manager
from gazoo_device.utility import common_utils
from gazoo_device.utility import multiprocessing_utils
import immutabledict

NO_RESULT = "< No result received >"
TIMEOUT_PROCESS = 600.0
_TIMEOUT_TERMINATE_PROCESS = 3
_QUEUE_READ_TIMEOUT = 1
_AnySerializable = Any


@dataclasses.dataclass(init=False)
class CallSpec:
  """Specifies a call to be executed in a parallel process.

  The function will be called in a parallel process as follows:
    return_value = function(<ManagerInstance>, *args, **kwargs)

  A Manager instance is always provided as the first argument to the function,
  followed by *args and **kwargs. The Manager instance will be closed
  automatically after the function returns.

  If the function is performing a device action, it is expected to create a
  device instance using the provided Manager instance (the device name to create
  should be included in the function's arguments), use the device instance to
  perform some action (possibly parameterized by args and kwargs), and close the
  device instance before returning. The return_value of the function will be
  returned to the main process.

  Attributes:
    function: Function to call in the parallel process. The function and its
      return value must be serializable. Prefer module-level functions. In
      particular, lambdas and inner (nested) functions are not serializable.
      Other limitations:
      - For devices communicating over UART or serial: ensure that access to
        device communication is mutually exclusive. In particular, make sure
        that device communication (`<device>.switchboard`) is closed in the main
        process before issuing a parallel action on it and do not execute
        simultaneous parallel actions on the same device.
        `<device>.reset_capability("switchboard")` can be used to close device
        communication, and it will be automatically reopened on the next access.
      - Do not modify GDM device configs (detection, set-prop) in parallel. This
        can result in a race condition.
    args: Positional arguments to the function. Must be serializable. In
      particular, Manager and device instances as well their instance methods
      are not serializable.
    kwargs: Keyword arguments to the function. Must be serializable.
  """
  function: Callable[..., _AnySerializable]
  args: Tuple[_AnySerializable, ...]
  kwargs: immutabledict.immutabledict[str, _AnySerializable]

  def __init__(self, function: Callable[..., _AnySerializable],
               *args: _AnySerializable, **kwargs: _AnySerializable):
    self.function = function
    self.args = args
    self.kwargs = immutabledict.immutabledict(kwargs)


def _process_wrapper(
    return_queue: multiprocessing.Queue,
    error_queue: multiprocessing.Queue,
    logging_queue: multiprocessing.Queue,
    process_id: str,
    call_spec: CallSpec) -> None:
  """Executes the provided function in a parallel process."""
  gdm_logger.initialize_child_process_logging(logging_queue)
  short_description = f"{call_spec.function.__name__} in process {os.getpid()}"
  full_description = f"{call_spec} in process {os.getpid()}"
  logger = gdm_logger.get_logger()
  logger.debug(f"Starting execution of {full_description}...")
  manager_inst = manager.Manager()
  try:
    return_value = call_spec.function(
        manager_inst, *call_spec.args, **call_spec.kwargs)
    return_queue.put((process_id, return_value))
    logger.debug(f"Execution of {short_description} succeeded.")
  except Exception as e:  # pylint: disable=broad-except
    error_queue.put((process_id, (type(e).__name__, str(e))))
    logger.warning(f"Execution of {short_description} raised an error: {e!r}.")
  finally:
    manager_inst.close()


def _read_all_from_queue(queue_inst: multiprocessing.Queue) -> List[Any]:
  """Reads and returns everything currently present in the queue."""
  queue_contents = []
  while True:
    try:
      queue_contents.append(
          queue_inst.get(block=True, timeout=_QUEUE_READ_TIMEOUT))
    except queue.Empty:
      break
  return queue_contents


def execute_concurrently(
    call_specs: Sequence[CallSpec],
    timeout=TIMEOUT_PROCESS,
    raise_on_process_error=True
) -> Tuple[List[Any], List[Optional[Tuple[str, str]]]]:
  """Concurrently executes function calls in parallel processes.

  Args:
    call_specs: Specifications for each of the parallel executions.
    timeout: Time to wait before terminating all of the parallel processes.
    raise_on_process_error: If True, raise an error if any of the parallel
      processes encounters an error. If False, return a list of errors which
      occurred in the parallel processes along with the received results.

  Returns:
    A tuple of (parallel_process_return_values, parallel_process_errors).
    The order of return values and errors corresponds to the order of provided
    call_specs. parallel_process_return_values will contain return values of the
    functions executed in parallel. If a parallel process fails, the
    corresponding entry in the return value list will be NO_RESULT.
    Errors are only returned if raise_on_process_error is False.
    Each error is specified as a tuple of (error_type, error_message).
    If a parallel process succeeds (there's no error), the corresponding entry
    in the error list will be None.

  Raises:
    ParallelUtilsError: If raise_on_process_error is True and any of the
      parallel processes encounters an error.
  """
  return_queue = multiprocessing_utils.get_context().Queue()
  error_queue = multiprocessing_utils.get_context().Queue()
  gdm_logger.switch_to_multiprocess_logging()
  logging_queue = gdm_logger.get_logging_queue()

  processes = []
  for proc_id, call_spec in enumerate(call_specs):
    processes.append(
        multiprocessing_utils.get_context().Process(
            target=_process_wrapper,
            args=(return_queue, error_queue, logging_queue, proc_id, call_spec),
            ))

  deadline = time.time() + timeout
  for process in processes:
    common_utils.run_before_fork()
    process.start()
    common_utils.run_after_fork_in_parent()

  for process in processes:
    remaining_timeout = max(0, deadline - time.time())  # ensure timeout >= 0
    process.join(timeout=remaining_timeout)
    if process.is_alive():
      process.terminate()
      process.join(timeout=_TIMEOUT_TERMINATE_PROCESS)
      if process.is_alive():
        process.kill()
        process.join(timeout=_TIMEOUT_TERMINATE_PROCESS)

  proc_results = [NO_RESULT] * len(call_specs)
  for proc_id, result in _read_all_from_queue(return_queue):
    proc_results[proc_id] = result

  proc_errors = [None] * len(call_specs)
  for proc_id, error_type_and_message in _read_all_from_queue(error_queue):
    proc_errors[proc_id] = error_type_and_message

  # We might not receive any results from a process if it times out or dies
  # unexpectedly. Mark such cases as errors.
  for proc_id in range(len(call_specs)):
    if proc_results[proc_id] == NO_RESULT and proc_errors[proc_id] is None:
      proc_errors[proc_id] = (
          errors.ResultNotReceivedError.__name__,
          "Did not receive any results from the process.")

  if raise_on_process_error and any(proc_errors):
    raise errors.ParallelUtilsError(
        f"Encountered errors in parallel processes: {proc_errors}")

  return proc_results, proc_errors


def factory_reset(manager_inst: manager.Manager, device_name: str) -> None:
  """Convenience function for factory resetting devices in parallel."""
  device = manager_inst.create_device(device_name)
  try:
    device.factory_reset()
  finally:
    device.close()


def reboot(manager_inst: manager.Manager, device_name: str,
           *reboot_args: Any, **reboot_kwargs: Any) -> None:
  """Convenience function for rebooting devices in parallel."""
  device = manager_inst.create_device(device_name)
  try:
    device.reboot(*reboot_args, **reboot_kwargs)
  finally:
    device.close()


def upgrade(manager_inst: manager.Manager, device_name: str,
            *upgrade_args: Any, **upgrade_kwargs: Any) -> None:
  """Convenience function for upgrading devices in parallel."""
  device = manager_inst.create_device(device_name)
  try:
    device.flash_build.upgrade(*upgrade_args, **upgrade_kwargs)
  finally:
    device.close()

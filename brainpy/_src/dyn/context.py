"""
Context for brainpy computation.

This context defines all shared data used in all modules in a computation.
"""

from typing import Any
from typing import Union, Callable, Optional, Dict

import jax
import jax.numpy as jnp
import numpy as np
from brainpy import check
from brainpy import math as bm
from brainpy._src.dyn.base import DynamicalSystemNS
from brainpy._src.math.delayvars import ROTATE_UPDATE, CONCAT_UPDATE
from brainpy._src.math.environment import get_dt
from brainpy._src.math.object_transform.base import dyn_dict
from brainpy._src.tools.dicts import DotDict
from brainpy.check import is_integer, jit_error_checking
from jax.lax import stop_gradient

__all__ = [
  'Delay',
  'share',
]


class Delay(DynamicalSystemNS):
  """Delay variable which has a fixed delay length.

    The data in this delay variable is arranged as::

         delay = 0             [ data
         delay = 1               data
         delay = 2               data
         ...                     ....
         ...                     ....
         delay = length-1        data
         delay = length          data ]

    Parameters
    ----------
    target: Variable
      The initial delay data.
    length: int
      The delay data length.
    before_t0: Any
      The delay data. It can be a Python number, like float, int, boolean values.
      It can also be arrays. Or a callable function or instance of ``Connector``.
      Note that ``initial_delay_data`` should be arranged as the following way::

         delay = 1             [ data
         delay = 2               data
         ...                     ....
         ...                     ....
         delay = length-1        data
         delay = length          data ]
    method: str
      The method used for updating delay.

    """

  data: Optional[bm.Variable]
  length: int

  def __init__(
      self,
      target: bm.Variable,
      length: int = 0,
      before_t0: Union[float, int, bool, bm.Array, jax.Array, Callable] = None,
      entries: Optional[Dict] = None,
      name: str = None,
      method: str = ROTATE_UPDATE,
  ):

    super().__init__(name=name)
    if method is None:
      if self.mode.is_a(bm.NonBatchingMode):
        method = ROTATE_UPDATE
      elif self.mode.is_parent_of(bm.TrainingMode):
        method = CONCAT_UPDATE
      else:
        method = ROTATE_UPDATE
    assert method in [ROTATE_UPDATE, CONCAT_UPDATE]
    self.method = method

    # target
    self.target = target
    if not isinstance(target, bm.Variable):
      raise ValueError(f'Must be an instance of brainpy.math.Variable. But we got {type(target)}')

    # delay length
    self.length = is_integer(length, allow_none=False, min_bound=0)

    # delay data
    if before_t0 is not None:
      assert isinstance(before_t0, (int, float, bool, bm.Array, jax.Array, Callable))
    self._before_t0 = before_t0
    if length > 0:
      self._init_data(length)
    else:
      self.data = None

    # other info
    self._access_to_step = dict()
    for entry, value in entries.items():
      self.register_entry(entry, value)

  def register_entry(
      self,
      entry: str,
      delay_time: Optional[Union[float, bm.Array, Callable]] = None,
      delay_step: Optional[Union[int, bm.Array, Callable]] = None,
  ) -> 'Delay':
    """Register an entry to access the data.

    Args:
      entry (str): The entry to access the delay data.
      delay_step: The delay step of the entry (must be an integer, denoting the delay step).
      delay_time: The delay time of the entry (can be a float).

    Returns:
      Return the self.
    """
    if entry in self._access_to_step:
      raise KeyError(f'Entry {entry} has been registered.')

    if delay_time is not None:
      if delay_step is not None:
        raise ValueError('Provide either "delay_time" or "delay_step". Both you have given both.')
      if callable(delay_time):
        delay_time = bm.as_jax(delay_time(self.delay_target_shape))
        delay_step = jnp.asarray(delay_time / bm.get_dt(), dtype=bm.get_int())
      elif isinstance(delay_time, float):
        delay_step = int(delay_time / bm.get_dt())
      else:
        delay_step = jnp.asarray(bm.as_jax(delay_time) / bm.get_dt(), dtype=bm.get_int())

    # delay steps
    if delay_step is None:
      delay_type = 'none'
    elif isinstance(delay_step, int):
      delay_type = 'homo'
    elif isinstance(delay_step, (bm.Array, jax.Array, np.ndarray)):
      if delay_step.size == 1 and delay_step.ndim == 0:
        delay_type = 'homo'
      else:
        delay_type = 'heter'
        delay_step = bm.Array(delay_step)
    elif callable(delay_step):
      delay_step = delay_step(self.delay_target_shape)
      delay_type = 'heter'
    else:
      raise ValueError(f'Unknown "delay_steps" type {type(delay_step)}, only support '
                       f'integer, array of integers, callable function, brainpy.init.Initializer.')
    if delay_type == 'heter':
      if delay_step.dtype not in [jnp.int32, jnp.int64]:
        raise ValueError('Only support delay steps of int32, int64. If your '
                         'provide delay time length, please divide the "dt" '
                         'then provide us the number of delay steps.')
      if self.delay_target_shape[0] != delay_step.shape[0]:
        raise ValueError(f'Shape is mismatched: {self.delay_target_shape[0]} != {delay_step.shape[0]}')
    if delay_type == 'heter':
      max_delay_step = int(max(delay_step))
    elif delay_type == 'homo':
      max_delay_step = delay_step
    else:
      max_delay_step = None

    # delay variable
    if max_delay_step is not None:
      if self.length < max_delay_step:
        self._init_data(max_delay_step)
        self.length = max_delay_step
    self._access_to_step[entry] = delay_step
    return self

  def at(self, entry: str, *indices) -> bm.Array:
    """Get the data at the given entry.

    Args:
      entry (str): The entry to access the data.
      *indices:

    Returns:
      The data.
    """
    assert isinstance(entry, str)
    if entry not in self._access_to_step:
      raise KeyError(f'Does not find delay entry "{entry}".')
    delay_step = self._access_to_step[entry]
    if delay_step is None:
      return self.target.value
    else:
      if self.data is None:
        return self.target.value
      else:
        if isinstance(delay_step, slice):
          return self.retrieve(delay_step, *indices)
        elif np.ndim(delay_step) == 0:
          return self.retrieve(delay_step, *indices)
        else:
          if len(indices) == 0 and len(delay_step) == self.target.shape[0]:
            indices = (jnp.arange(delay_step.size),)
          return self.retrieve(delay_step, *indices)

  @property
  def delay_target_shape(self):
    """The data shape of the delay target."""
    return self.target.shape

  def __repr__(self):
    name = self.__class__.__name__
    return (f'{name}(num_delay_step={self.length}, '
            f'delay_target_shape={self.delay_target_shape}, '
            f'update_method={self.method})')

  def _check_delay(self, delay_len):
    raise ValueError(f'The request delay length should be less than the '
                     f'maximum delay {self.length}. '
                     f'But we got {delay_len}')

  def retrieve(self, delay_step, *indices):
    """Retrieve the delay data according to the delay length.

    Parameters
    ----------
    delay_step: int, ArrayType
      The delay length used to retrieve the data.
    """
    assert delay_step is not None
    if check.is_checking():
      jit_error_checking(jnp.any(delay_step > self.length), self._check_delay, delay_step)

    if self.method == ROTATE_UPDATE:
      i = share.load('i')
      delay_idx = (i + delay_step) % (self.length + 1)
      delay_idx = stop_gradient(delay_idx)

    elif self.method == CONCAT_UPDATE:
      delay_idx = delay_step

    else:
      raise ValueError(f'Unknown updating method "{self.method}"')

    # the delay index
    if hasattr(delay_idx, 'dtype') and not jnp.issubdtype(delay_idx.dtype, jnp.integer):
      raise ValueError(f'"delay_len" must be integer, but we got {delay_idx}')
    indices = (delay_idx,) + tuple(indices)

    # the delay data
    return self.data[indices]

  def update(self, latest_value: Optional[Union[bm.Array, jax.Array]] = None) -> None:
    """Update delay variable with the new data.
    """
    if self.data is not None:
      # get the latest target value
      if latest_value is None:
        latest_value = self.target.value

      # update the delay data at the rotation index
      if self.method == ROTATE_UPDATE:
        i = share.load('i')
        idx = bm.as_jax((i - 1) % (self.length + 1))
        self.data[idx] = latest_value

      # update the delay data at the first position
      elif self.method == CONCAT_UPDATE:
        if self.length >= 2:
          self.data.value = bm.vstack([latest_value, self.data[1:]])
        else:
          self.data[0] = latest_value

  def reset_state(self, batch_size: int = None):
    """Reset the delay data.
    """
    # initialize delay data
    if self.data is not None:
      self._init_data(self.length, batch_size)

  def _init_data(self, length, batch_size: int = None):
    if batch_size is not None:
      if self.target.batch_size != batch_size:
        raise ValueError(f'The batch sizes of delay variable and target variable differ '
                         f'({self.target.batch_size} != {batch_size}). '
                         'Please reset the target variable first, because delay data '
                         'depends on the target variable. ')

    if self.target.batch_axis is None:
      batch_axis = None
    else:
      batch_axis = self.target.batch_axis + 1
    self.data = bm.Variable(jnp.zeros((length + 1,) + self.target.shape, dtype=self.target.dtype),
                            batch_axis=batch_axis)
    # update delay data
    self.data[0] = self.target.value
    if isinstance(self._before_t0, (bm.Array, jax.Array, float, int, bool)):
      self.data[1:] = self._before_t0
    elif callable(self._before_t0):
      self.data[1:] = self._before_t0((length,) + self.target.shape, dtype=self.target.dtype)


class _ShareContext(DynamicalSystemNS):
  def __init__(self):
    super().__init__()

    # Shared data across all nodes at current time step.
    # -------------

    self._arguments = DotDict()
    self._delays: Dict[str, Delay] = dyn_dict()

  @property
  def dt(self):
    if 'dt' in self._arguments:
      return self._arguments['dt']
    else:
      return get_dt()

  @dt.setter
  def dt(self, dt):
    self.set_dt(dt)

  def set_dt(self, dt: Union[int, float]):
    self._arguments['dt'] = dt

  def load(self, key, value: Any = None):
    """Get the shared data by the ``key``.

    Args:
      key (str): the key to indicate the data.
      value (Any): the default value when ``key`` is not defined in the shared.
    """
    if key == 'dt':
      return self.dt
    if key in self._arguments:
      return self._arguments[key]
    if key in self._delays:
      return self._delays[key]
    if value is None:
      raise KeyError(f'Cannot found shared data of {key}.')
    else:
      return value

  def save(self, *args, **kwargs) -> None:
    """Save shared arguments in the global context."""
    assert len(args) % 2 == 0
    for i in range(0, len(args), 2):
      identifier = args[i * 2]
      data = args[i * 2 + 1]
      if isinstance(data, Delay):
        if identifier in self._delays:
          raise ValueError(f'{identifier} has been used. Please assign another name.')
        self._delays[identifier] = data
      else:
        self._arguments[identifier] = data
    for identifier, data in kwargs.items():
      if isinstance(data, Delay):
        if identifier in self._delays:
          raise ValueError(f'{identifier} has been used. Please assign another name.')
        self._delays[identifier] = data
      else:
        self._arguments[identifier] = data

  def get_shargs(self) -> DotDict:
    """Get all shared arguments in the global context."""
    return self._arguments.copy()

  def clear_delays(self, *delays) -> None:
    """Clear all delay variables in this global context."""
    if len(delays):
      for d in delays:
        self._delays.pop(d)
    else:
      self._delays.clear()

  def clear_shargs(self, *args) -> None:
    """Clear all shared arguments in the global context."""
    if len(args) > 0:
      for a in args:
        self._arguments.pop(a)
    else:
      self._arguments.clear()

  def clear(self) -> None:
    """Clear all shared data in this computation context."""
    self._arguments.clear()
    self._delays.clear()

  def __call__(self, *args, **kwargs):
    return self.update(*args, **kwargs)

  def update(self, *args, **kwargs):
    for delay in self._delays.values():
      delay.update()

  def reset(self, batch_size: int = None):
    self.reset_state(batch_size=batch_size)

  def reset_state(self, batch_size: int = None):
    for delay in self._delays.values():
      delay.reset_state(batch_size)


share = _ShareContext()

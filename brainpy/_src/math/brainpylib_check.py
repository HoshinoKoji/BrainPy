from jax.lib import xla_client
import taichi as ti
import os


taichi_path = ti.__path__[0]
taichi_c_api_install_dir = os.path.join(taichi_path, '_lib', 'c_api')
import ctypes
try:
  ctypes.CDLL(taichi_c_api_install_dir + '/lib/libtaichi_c_api.so')
except OSError:
  print('taichi aot custom call, Only support linux now.')

# Register the CPU XLA custom calls
try:
  import brainpylib
  from brainpylib import cpu_ops

  for _name, _value in cpu_ops.registrations().items():
    xla_client.register_custom_call_target(_name, _value, platform="cpu")
except ImportError:
  cpu_ops = None
  brainpylib = None

# Register the GPU XLA custom calls
try:
  from brainpylib import gpu_ops

  for _name, _value in gpu_ops.registrations().items():
    xla_client.register_custom_call_target(_name, _value, platform="gpu")
except ImportError:
  gpu_ops = None

# check brainpy and brainpylib version consistency
_minimal_brainpylib_version = '0.1.10'
if brainpylib is not None:
  if brainpylib.__version__ < _minimal_brainpylib_version:
    raise SystemError(f'This version of brainpy needs brainpylib >= {_minimal_brainpylib_version}.')
  if hasattr(brainpylib, 'check_brainpy_version'):
    brainpylib.check_brainpy_version()

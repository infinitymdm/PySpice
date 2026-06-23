# (c) Janez Puhan, Arpad Buermen. Imported from PyOPUS project.
from .. import _hspice_read

def hspice_read(filename, debug=0):
  """
  Wrapper for PyOPUS-based binary HSPICE raw result reader.
  """
  return _hspice_read.hspice_read(filename, debug)

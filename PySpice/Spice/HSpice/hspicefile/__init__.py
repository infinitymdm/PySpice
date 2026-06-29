from typing import Union, List, Tuple
try:
  from .. import _hspice_read
except ImportError:
  _hspice_read = None


def hspice_read(filename, debug=0) -> Union[List[Tuple], None]:
  """
  Read an HSPICE raw results file.

  Parameters:
    filename: Path to the HSPICE raw file to read.
    debug: Debug level passed to the reader.

  Returns:
    A list containing a single tuple of:
      - sweeps (tuple):
        - sweep_name (str or None): Name of sweep parameter.
        - sweep_values (np.ndarray or None): 1D array of sweep parameter values.
        - data_list (list of dict): List of dicts mapping variable names to
          1D np.ndarray of values. Key names are lowercase with prefix "v(" stripped.
      - scale_name (str): Name of independent variable.
      - None (placeholder)
      - title (str): Plot title.
      - date (str): File creation date/time string.
      - None (placeholder)

  Raises:
    ImportError: If the optional _hspice_read extension is unavailable.
  """
  if _hspice_read is None:
    raise ImportError(
      "HSpice read extension _hspice_read is not compiled/available."
    )
  return _hspice_read.hspice_read(filename, debug)


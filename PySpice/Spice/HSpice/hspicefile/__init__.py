try:
    from .. import _hspice_read
except ImportError:
    _hspice_read = None


def hspice_read(filename, debug=0):
    """
    Wrapper for PyOPUS-based binary HSPICE raw result reader.
    """
    if _hspice_read is None:
        raise ImportError(
            "HSpice read extension _hspice_read is not compiled/available."
        )
    return _hspice_read.hspice_read(filename, debug)

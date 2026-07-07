import logging

from ..SimulatorBase import Simulator
from .Server import HSpiceServer

_module_logger = logging.getLogger(__name__)


class HSpiceSimulator(Simulator):
    _logger = _module_logger.getChild("HSpiceSimulator")
    SIMULATOR = "hspice"

    def __init__(self, **kwargs):
        server_kwargs = {
            x: kwargs[x]
            for x in ("spice_command", "concurrency_limit", "timeout")
            if x in kwargs
        }
        self._spice_server = HSpiceServer(**server_kwargs)

    @property
    def version(self):
        return ""

    def customise(self, simulation):
        # Add post option to simulation options if not present
        if "post" not in simulation._options and "POST" not in simulation._options:
            simulation.options(post=1)

    def run(self, simulation, *args, **kwargs):
        raw_file = self._spice_server(spice_input=str(simulation))
        raw_file.simulation = simulation
        return raw_file.to_analysis()

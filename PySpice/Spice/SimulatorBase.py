###################################################################################################
#
# PySpice - A Spice Package for Python
# Copyright (C) 2021 Fabrice Salvaire
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
####################################################################################################

__all__ = ['Simulator']

import logging

from ..Config import ConfigInstall
from .Simulation import Simulation

_module_logger = logging.getLogger(__name__)

class Simulator:

    """Base class to implement a simulator.

    """

    _logger = _module_logger.getChild('Simulator')

    #: Define the default simulator
    DEFAULT_SIMULATOR = None
    if ConfigInstall.OS.on_windows:
        DEFAULT_SIMULATOR = 'ngspice-shared'
    else:
        DEFAULT_SIMULATOR = 'ngspice-shared'

    SIMULATOR = None   # for subclass
    _SIMULATOR_CLASSES = {}

    @classmethod
    def register_simulator_class(cls, name, sub_cls):
        cls._SIMULATOR_CLASSES[name] = sub_cls

    @classmethod
    def factory(cls, *args, **kwargs):
        """Factory to instantiate a simulator.

        By default, it instantiates the simulator defined in :obj:`DEFAULT_SIMULATOR`, however you
        can set the simulator using the :obj:`simulator` parameter.

        Available simulators are:

        * :code:`ngspice` **alias for shared**
        * :code:`ngspice-shared` **DEFAULT**
        * :code:`ngspice-subprocess`
        * :code:`xyce` **alias for serial**
        * :code:`xyce-serial`
        * :code:`xyce-parallel`
        * :code:`hspice`

        Return a :obj:`PySpice.Spice.Simulator` subclass.

        """
        simulator = kwargs.pop('simulator', cls.DEFAULT_SIMULATOR)

        if simulator not in cls._SIMULATOR_CLASSES:
            raise NameError(f"Unknown simulator {simulator}")

        sub_cls = cls._SIMULATOR_CLASSES[simulator]

        obj = sub_cls(*args, simulator=simulator, **kwargs)
        obj._AS_SIMULATOR = simulator
        return obj

    def __getstate__(self):
        # Pickle: protection for cffi
        return self.__class__.__name__

    def simulation(self, circuit, **kwargs):
        """Create a new simulation for the circuit.

        Return a :obj:`PySpice.Spice.Simulation` instance`

        """
        # Note: simulation is simulator dependent, thus subclass this method if needed
        return Simulation(self, circuit, **kwargs)

    @property
    def name(self):
        return self._AS_SIMULATOR

    @property
    def version(self):
        raise NotImplementedError

    def customise(self, simulation):
        """Customise the simulation"""
        pass

    def run(self, simulation):
        """Run the simulation and return the waveforms."""
        raise NotImplementedError

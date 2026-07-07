import logging

import numpy as np

from PySpice.Probe.WaveForm import (AcAnalysis, DcAnalysis, OperatingPoint,
                                    TransientAnalysis, WaveForm)
from PySpice.Unit import u_A, u_Degree, u_Hz, u_s, u_V


class AnalysisList(list):
    def __init__(self, analyses, measurements=None):
        super().__init__(analyses)
        self._measurements = measurements or {}

    @property
    def measurements(self):
        return self._measurements

    def __getattr__(self, name):
        measurements = self.__dict__.get("_measurements", {})
        if name in measurements:
            return measurements[name]
        if name.lower() in measurements:
            return measurements[name.lower()]
        raise AttributeError(name)

    def __getitem__(self, item):
        if isinstance(item, (int, slice)):
            return super().__getitem__(item)
        if isinstance(item, str):
            if item in self._measurements:
                return self._measurements[item]
            if item.lower() in self._measurements:
                return self._measurements[item.lower()]
            raise IndexError(item)
        raise KeyError(item)


_module_logger = logging.getLogger(__name__)


class HSpiceRawFile:
    def __init__(
        self,
        data,
        simulation=None,
        measurements=None,
        op_nodes=None,
        op_branches=None,
        analysis_type=None,
    ):
        self.data = data
        self._simulation = simulation
        self.measurements = measurements or {}
        self.op_nodes = op_nodes or {}
        self.op_branches = op_branches or {}
        self._analysis_type = analysis_type

    @property
    def simulation(self):
        return self._simulation

    @simulation.setter
    def simulation(self, value):
        self._simulation = value

    def to_analysis(self):
        if self.data is None and self._analysis_type == "o":
            # This is an operating point simulation!
            nodes = [
                WaveForm.from_unit_values(name, u_V(np.array([val])))
                for name, val in self.op_nodes.items()
            ]
            branches = [
                WaveForm.from_unit_values(name, u_A(np.array([val])))
                for name, val in self.op_branches.items()
            ]
            return OperatingPoint(
                simulation=self.simulation, nodes=nodes, branches=branches
            )

        # data is a list of sweeps returned by hspice_read
        # data[0] is sweeps tuple (sweep, sweepValues, dataList)
        sweeps = self.data[0][0]
        scale_name_outer = self.data[0][1]
        scale_name_inner, sweep_values, data_list = sweeps

        if len(data_list) == 0:
            analysis_type = self._analysis_type
            if (
                not analysis_type
                and self.simulation
                and hasattr(self.simulation, "_analyses")
            ):
                analyses_keys = set(self.simulation._analyses.keys())
                if "ac" in analyses_keys:
                    analysis_type = "a"
                elif "dc" in analyses_keys:
                    analysis_type = "s"
                elif "op" in analyses_keys:
                    analysis_type = "o"
                elif "tran" in analyses_keys:
                    analysis_type = "t"

            analysis_type = analysis_type or "t"

            if analysis_type == "a":
                analysis = AcAnalysis(
                    simulation=self.simulation,
                    frequency=WaveForm.from_array("frequency", np.array([])),
                    nodes=[],
                    branches=[],
                    internal_parameters=[],
                )
            elif analysis_type == "s":
                analysis = DcAnalysis(
                    simulation=self.simulation,
                    sweep=WaveForm.from_array("sweep", np.array([])),
                    nodes=[],
                    branches=[],
                    internal_parameters=[],
                )
            elif analysis_type == "o":
                analysis = OperatingPoint(
                    simulation=self.simulation,
                    nodes=[],
                    branches=[],
                )
            else:
                analysis = TransientAnalysis(
                    simulation=self.simulation,
                    time=WaveForm.from_array("time", np.array([])),
                    nodes=[],
                    branches=[],
                    internal_parameters=[],
                )
            analysis._measurements = dict(self.measurements)
            return analysis

        # Derive analysis type and scale unit once from the parser-provided name.
        _sn_upper = scale_name_outer.upper()
        if _sn_upper == "TIME":
            _analysis_type_outer = "transient"
            _scale_unit_outer = u_s
        elif _sn_upper in ("FREQUENCY", "HERTZ"):
            _analysis_type_outer = "ac"
            _scale_unit_outer = u_Hz
        else:
            _analysis_type_outer = "dc"
            if _sn_upper == "VOLTS":
                _scale_unit_outer = u_V
            elif _sn_upper == "AMPS":
                _scale_unit_outer = u_A
            elif _sn_upper == "DEG_C":
                _scale_unit_outer = u_Degree
            else:
                _scale_unit_outer = None

        analyses = []
        for res_dict in data_list:
            # Resolve the dict key using the parser-provided scale_name_outer.
            _key_lower = scale_name_outer.lower()
            if _key_lower in res_dict:
                scale_name = _key_lower
            else:
                scale_name = next(
                    (k for k in res_dict if k.lower() == _key_lower), None
                )
                if scale_name is None:
                    raise KeyError(
                        f"Independent variable '{scale_name_outer}' not found in sweep dict "
                        f"(keys: {list(res_dict.keys())})"
                    )
            scale_values = res_dict[scale_name]

            analysis_type = _analysis_type_outer
            scale_unit = _scale_unit_outer

            # Build WaveForms
            abscissa_name = scale_name_outer.lower()
            if analysis_type == "ac":
                abscissa_name = "frequency"

            if scale_unit:
                abscissa_waveform = WaveForm.from_unit_values(
                    abscissa_name, scale_unit(scale_values)
                )
            else:
                abscissa_waveform = WaveForm.from_array(abscissa_name, scale_values)

            nodes = []
            branches = []

            for k, v in res_dict.items():
                if k == scale_name:
                    continue

                # Determine if it's a current or voltage
                if k.lower().startswith("i("):
                    # Simplify name: strip i( and trailing )
                    simplified_name = k[2:]
                    if simplified_name.endswith(")"):
                        simplified_name = simplified_name[:-1]
                    branches.append(
                        WaveForm.from_unit_values(
                            simplified_name, u_A(v), abscissa=abscissa_waveform
                        )
                    )
                else:
                    simplified_name = k
                    nodes.append(
                        WaveForm.from_unit_values(
                            simplified_name, u_V(v), abscissa=abscissa_waveform
                        )
                    )

            if analysis_type == "transient":
                analysis = TransientAnalysis(
                    simulation=self.simulation,
                    time=abscissa_waveform,
                    nodes=nodes,
                    branches=branches,
                    internal_parameters=[],
                )
            elif analysis_type == "ac":
                analysis = AcAnalysis(
                    simulation=self.simulation,
                    frequency=abscissa_waveform,
                    nodes=nodes,
                    branches=branches,
                    internal_parameters=[],
                )
            else:
                analysis = DcAnalysis(
                    simulation=self.simulation,
                    sweep=abscissa_waveform,
                    nodes=nodes,
                    branches=branches,
                    internal_parameters=[],
                )

            analysis._measurements = dict(self.measurements)
            analyses.append(analysis)

        if len(analyses) == 1:
            return analyses[0]
        return AnalysisList(analyses, dict(self.measurements))

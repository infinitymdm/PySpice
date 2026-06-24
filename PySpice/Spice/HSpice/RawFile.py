import logging
import numpy as np
from PySpice.Unit import u_V, u_A, u_s, u_Hz
from PySpice.Probe.WaveForm import TransientAnalysis, DcAnalysis, AcAnalysis, WaveForm, OperatingPoint

_module_logger = logging.getLogger(__name__)

class HSpiceRawFile:
    def __init__(self, data, simulation=None, measurements=None, op_nodes=None, op_branches=None):
        self.data = data
        self._simulation = simulation
        self.measurements = measurements or {}
        self.op_nodes = op_nodes or {}
        self.op_branches = op_branches or {}

    @property
    def simulation(self):
        return self._simulation

    @simulation.setter
    def simulation(self, value):
        self._simulation = value

    def to_analysis(self):
        if self.data is None:
            # This is an operating point simulation!
            nodes = [WaveForm.from_unit_values(name, u_V(np.array([val]))) for name, val in self.op_nodes.items()]
            branches = [WaveForm.from_unit_values(name, u_A(np.array([val]))) for name, val in self.op_branches.items()]
            return OperatingPoint(
                simulation=self.simulation,
                nodes=nodes,
                branches=branches
            )

        # data is a list of sweeps returned by hspice_read
        # data[0] is sweeps tuple (sweep, sweepValues, dataList)
        sweeps = self.data[0][0]
        scale_name_outer, sweep_values, data_list = sweeps

        if len(data_list) != 1:
            raise ValueError(f"Expected exactly 1 table in data_list, but got {len(data_list)}.")

        # data_list is a list of dictionaries, each dict represents a table.
        # Typically there is only 1 table if no outer sweep.
        res_dict = data_list[0]

        # Let's find the scale/abscissa variable (typically TIME or FREQUENCY or HERTZ)
        scale_name = None
        for k in res_dict.keys():
            if k.upper() in ('TIME', 'FREQUENCY', 'HERTZ'):
                scale_name = k
                break

        if scale_name is None:
            # Check if there is another scale variable (e.g. dc sweep)
            scale_name = list(res_dict.keys())[0]

        scale_values = res_dict[scale_name]

        # Determine analysis type
        if scale_name.upper() == 'TIME':
            analysis_type = 'transient'
            scale_unit = u_s
        elif scale_name.upper() in ('FREQUENCY', 'HERTZ'):
            analysis_type = 'ac'
            scale_unit = u_Hz
        else:
            analysis_type = 'dc'
            scale_unit = None

        # Build WaveForms
        abscissa_name = scale_name.lower()
        if analysis_type == 'ac':
            abscissa_name = 'frequency'

        if scale_unit:
            abscissa_waveform = WaveForm.from_unit_values(abscissa_name, scale_unit(scale_values))
        else:
            abscissa_waveform = WaveForm.from_array(abscissa_name, scale_values)

        nodes = []
        branches = []

        for k, v in res_dict.items():
            if k == scale_name:
                continue

            # Determine if it's a current or voltage
            if k.lower().startswith('i('):
                # Simplify name: strip i( and trailing )
                simplified_name = k[2:]
                if simplified_name.endswith(')'):
                    simplified_name = simplified_name[:-1]
                branches.append(WaveForm.from_unit_values(simplified_name, u_A(v), abscissa=abscissa_waveform))
            else:
                simplified_name = k
                nodes.append(WaveForm.from_unit_values(simplified_name, u_V(v), abscissa=abscissa_waveform))

        if analysis_type == 'transient':
            analysis = TransientAnalysis(
                simulation=self.simulation,
                time=abscissa_waveform,
                nodes=nodes,
                branches=branches,
                internal_parameters=[]
            )
        elif analysis_type == 'ac':
            analysis = AcAnalysis(
                simulation=self.simulation,
                frequency=abscissa_waveform,
                nodes=nodes,
                branches=branches,
                internal_parameters=[]
            )
        else:
            analysis = DcAnalysis(
                simulation=self.simulation,
                sweep=abscissa_waveform,
                nodes=nodes,
                branches=branches,
                internal_parameters=[]
            )

        analysis._measurements = self.measurements
        return analysis

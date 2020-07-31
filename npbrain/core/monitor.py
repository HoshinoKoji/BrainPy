# -*- coding: utf-8 -*-

import numpy as np
from numba import typed, types, prange

from npbrain.core.neuron import Neurons
from npbrain.core.synapse import Synapses
from npbrain.utils import helper, profile

__all__ = [
    'Monitor',
    'SpikeMonitor',
    'StateMonitor',

    'raster_plot',
    'firing_rate',
]


class Monitor(object):
    """Base monitor class.

    """

    def __init__(self, target):
        self.target = target
        self.update_state = helper.autojit(self.update_state)

    def init_state(self, *args, **kwargs):
        raise NotImplementedError()


class SpikeMonitor(Monitor):
    """Monitor class to record spikes.

    Parameters
    ----------
    target : Neurons
        The neuron group to monitor.
    """

    def __init__(self, target):
        # check `variables`
        self.vars = ('index', 'time')
        num = target.state.shape[1]

        # check `target`
        assert isinstance(target, Neurons), 'Cannot monitor spikes in synapses.'

        # fake initialization
        self.index = []
        self.time = []

        @helper.autojit('void(f8[:, :], ListType(f8), ListType(i8), f8)')
        def update_state(neu_state, mon_time, mon_index, t):
            for idx in prange(num):
                if neu_state[-3, idx] > 0.:
                    mon_index.append(idx)
                    mon_time.append(t)

        self.update_state = update_state

        # super class initialization
        super(SpikeMonitor, self).__init__(target)

    def init_state(self, length):
        if profile.is_numba_bk():
            self.index = typed.List.empty_list(types.int64)
            self.time = typed.List.empty_list(types.float64)
        else:
            self.index = []
            self.time = []


class StateMonitor(Monitor):
    """Monitor class to record states.

    Parameters
    ----------
    target : Neurons, Synapses
        The object to monitor.
    vars : str, list, tuple
        The variable need to be recorded for the ``target``.
    """

    def __init__(self, target, vars=None):
        # check `variables`
        if vars is None:
            if isinstance(target, Neurons):
                vars = ['V']
            elif isinstance(target, Synapses):
                vars = ['g_out']
            else:
                raise ValueError('When `vars=None`, NumpyBrain only supports the recording '
                                 'of "V" for Neurons and "g" for Synapses.')
        if isinstance(vars, str):
            vars = [vars]
        assert isinstance(vars, (list, tuple))
        vars = tuple(vars)
        for var in vars:
            if var not in target.var2index:
                raise ValueError('Variable "{}" is not in target "{}".'.format(var, target))
        self.vars = vars

        # fake initialization
        for k in self.vars:
            setattr(self, k, np.zeros((1, 1)))
        self.state = []

        # function of update state
        @helper.autojit('void(f8[:, :], UniTuple(f8[:, :], {}), i4[:], i4)'.format(len(vars)))
        def record_neu_state(obj_state, mon_states, vars_idx, i):
            for j, index in enumerate(vars_idx):
                v = obj_state[index]
                mon_states[j][i] = v

        @helper.autojit('void(UniTuple(f8[:, :], 3), UniTuple(f8[:, :], {}), i4[:, :], i4)'.format(len(vars)))
        def record_syn_state(obj_state, mon_states, vars_idx, i):
            for j, index in enumerate(vars_idx):
                index = vars_idx[j]
                v = obj_state[index[0]][index[1]]
                mon_states[j][i] = v

        # variable2index and update_state function
        if isinstance(target, Neurons):
            self.update_state = record_neu_state
            var_idxs = np.array([target.var2index[v] for v in self.vars])
            self.target_index_by_vars = lambda: var_idxs
        elif isinstance(target, Synapses):
            self.update_state = record_syn_state
            if 'g_in' not in vars and 'g_out' not in vars:
                var_idxs = np.array([target.var2index[v] for v in self.vars])
                self.target_index_by_vars = lambda: var_idxs
            else:
                self.target_index_by_vars = lambda: np.array([target.var2index[v] for v in self.vars])
        else:
            raise ValueError('Unknown type.')

        # super class initialization
        super(StateMonitor, self).__init__(target)

    def init_state(self, length):
        assert isinstance(length, int)

        vars_idx = self.target_index_by_vars()
        mon_states = []
        for i, k in enumerate(self.vars):
            index = vars_idx[i]
            if isinstance(self.target, Synapses):
                v = self.target.state
                for idx in index:
                    v = v[idx]
            else:
                v = self.target.state[index]
            shape = (length,) + v.shape
            state = np.zeros(shape)
            setattr(self, k, state)
            mon_states.append(state)
        self.state = tuple(mon_states)


def raster_plot(mon, times=None):
    """Get spike raster plot which displays the spiking activity
    of a group of neurons over time.

    Parameters
    ----------
    mon : Monitor
        The monitor which record spiking activities.
    times : None, numpy.ndarray
        The time steps.

    Returns
    -------
    raster_plot : tuple
        Include (neuron index, spike time).
    """
    if isinstance(mon, StateMonitor):
        elements = np.where(mon.spike > 0.)
        index = elements[1]
        if hasattr(mon, 'spike_time'):
            time = mon.spike_time[elements]
        else:
            assert times is not None, 'Must provide "times" when StateMonitor has no "spike_time" attribute.'
            time = times[elements[0]]
    elif isinstance(mon, SpikeMonitor):
        index = np.array(mon.index)
        time = np.array(mon.time)
    else:
        raise ValueError
    return index, time


def firing_rate(mon, width, window='gaussian'):
    """Calculate the mean firing rate over in a neuron group.

    This method is adopted from Brian2.

    The firing rate in trial :math:`k` is the spike count :math:`n_{k}^{sp}`
    in an interval of duration :math:`T` divided by :math:`T`:

    .. math::

        v_k = {n_k^{sp} \\over T}

    Parameters
    ----------
    mon : StateMonitor
        The monitor which record spiking activities.
    width : int, float
        The width of the ``window`` in millisecond.
    window : str
        The window to use for smoothing. It can be a string to chose a
        predefined window:

        - `flat`: a rectangular,
        - `gaussian`: a Gaussian-shaped window.

        For the `Gaussian` window, the `width` parameter specifies the
        standard deviation of the Gaussian, the width of the actual window
        is `4 * width + dt`.
        For the `flat` window, the width of the actual window
        is `2 * width/2 + dt`.

    Returns
    -------
    rate : numpy.ndarray
        The population rate in Hz, smoothed with the given window.
    """
    # rate
    assert hasattr(mon, 'spike'), 'Must record the "spike" of the neuron group to get firing rate.'
    rate = np.sum(mon.spike, axis=1)

    # window
    dt = profile.get_dt()
    if window == 'gaussian':
        width1 = 2 * width / dt
        width2 = int(np.round(width1))
        window = np.exp(-np.arange(-width2, width2 + 1) ** 2 / (width1 ** 2 / 2))
    elif window == 'flat':
        width1 = int(width / 2 / dt) * 2 + 1
        window = np.ones(width1)
    else:
        raise ValueError('Unknown window type "{}".'.format(window))
    window = np.float_(window)

    return np.convolve(rate, window / sum(window), mode='same')

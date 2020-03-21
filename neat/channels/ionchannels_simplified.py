import sympy as sp
import numpy as np
import scipy.optimize as so

import os
import copy

CONC_DICT = {'na': 10., # mM
             'k': 54.4, # mM
             'ca': 1e-4, # 1e-4
            }

TEMP_DEFAULT = 36.

E_ION_DICT = {'na': 50.,
             'k': -85.,
             'ca': 50.,
            }


class _func(object):
    def __init__(self, eval_func_aux, eval_func_vtrap, e_trap):
        self.eval_func_aux = eval_func_aux
        self.eval_func_vtrap = eval_func_vtrap
        self.e_trap = e_trap

    def __call__(self, *args):
        vv = args[0]
        if isinstance(vv, float):
            if np.abs(vv - self.e_trap) < 0.001:
                return self.eval_func_vtrap(*args)
            else:
                return self.eval_func_aux(*args)
        else:
            fv_return = np.zeros_like(vv)
            bool_vtrap = np.abs(vv - self.e_trap) < 0.0001
            inds_vtrap = np.where(bool_vtrap)
            args_ = [a[inds_vtrap] for a in args]
            fv_return[inds_vtrap] = self.eval_func_vtrap(*args_)
            inds = np.where(np.logical_not(bool_vtrap))
            args_ = [a[inds] for a in args]
            fv_return[inds] = self.eval_func_aux(*args_)
            return fv_return


def _insert_function_prefixes(string, prefix='np',
                              functions=['exp', 'sin', 'cos', 'tan', 'pi']):
    """
    Prefix all occurences in the input `string` of the functions in the
    `functions` list with the provided `prefix`.

    Parameters
    ----------
    string: string
        the input string
    prefix: string, optional
        the prefix that is put before each function. Defaults to `'np'`
    functions: list of strings, optional
        the list of functions that will be prefixed. Defaults to
        `['exp', 'sin', 'cos', 'tan', 'pi']`

    Returns
    -------
    string

    Examples
    --------
    >>> _insert_function_prefixes('5. * exp(0.) + 3. * cos(pi)')
    '5. * np.exp(0.) + 3. * np.cos(pi)'
    """
    for func_name in functions:
        numpy_string = ''
        while len(string) > 0:
            ind = string.find(func_name)
            if ind == -1:
                numpy_string += string
                string = ''
            else:
                numpy_string += string[0:ind] + prefix + '.' + func_name
                string = string[ind+len(func_name):]
        string = numpy_string
    return string


class CallDict(dict):
    """
    Callable dictionary, items are supposed to be callables
    that all accept an identical argument list
    """
    def __call__(self, *args):
        """
        Calls dictionary items (supposed to be callable)
        """
        return {k: f(*args) for k, f in self.items()}


class IonChannelSimplified(object):
    """
    Base ion channel class that implements linearization and code generation for
    NEURON (.mod-files) and C++.

    Userdefined ion channels should inherit from this class and implement the
    `define()` function, where the specific attributes of the ion channel are set.

    The ion channel current is of the form
    .. math:: i_{chan} = \overline{g} \, p_o(x_1, \hdots x_n) \, (e - v)
    where $p_o$ is the open probability defined as a function of a number of
    state variables. State variables evolve according to
    .. math:: \dot{x}_i = f_i(x_i, v, c_1, \hdots, c_k)
    with $c_1, \hdots, c_n$ the (optional set of concentrations the ion channel
    depends on). There are two canonical ways to define $f_i$, either based on
    reaction rates $alpha$ and $beta$:
    .. math:: \dot{x}_i = \alpha_i(v) \, (1 - x_i) - \beta_i{v} \, x_i,
    or based on an asymtotic value $x_i^{\infty}$  and time-scale $\tau_i$
    .. math:: \dot{x}_i = \frac{x_i^{\infty}(v) - x_i}{\tau_i(v)}.
    `IonChannel` accepts handles either description. For the former description,
    dicts `self.alpha` and `self.beta` must be defined with as keys the names
    of every state variable in the open probability. Similarly, for the latter
    description, dicts `self.tauinf` and `self.varinf` must be defined with as
    keys the name of every state variable.

    The user **must** define the following attributes in the `define()` function:

    Attributes
    ----------
    **Obligatory**
    p_open: str
        The open probability of the ion channel.
    alpha, beta: dict {str: str}
        dictionary of the rate function for each state variables. Keys must
        correspond to the name of every state variable in `p_open`, values must
        be formulas written as strings with `v` and possible ion as variabels
    tauinf, varinf: dict {str: str}
        state variable time scale and asymptotic activation level. Keys must
        correspond to the name of every state variable in `p_open`, values must
        be formulas written as strings with `v` and possible ion as variabels
    **Optional**
    ion: str ('na', 'ca', 'k' or '')
        The ion to which the ion channel is permeable
    conc: set of str (containing 'na', 'ca', 'k') or dict of {str: float}
        The concentrations the ion channel activation depends on. Can be a set
        of ions or a dict with the ions as keys and default values as float.
    q10: str
        Temperature dependence of the state variable rate functions. Must
        contain the variable 'temp' for the temperature in ``[deg C]``
    temp: float
        The temperature at which the ion channel is evaluated.
    e: float
        Reversal of the ion channel in ``[mV]``. functions that need it allow
        the default value to be overwritten with a keyword argument. If nothing
        is provided, will take a default reversal for `self.ion`. If no ion
        is provided, will given an error if functions that need `e` are called
        without specifying it.

    Examples
    --------
    ```
    class Na_Ta(IonChannelSimplified):
        def define(self):
            # from (Colbert and Pan, 2002), Used in (Hay, 2011)
            self.ion = 'na'
            # concentrations the ion channel depends on
            self.conc = {}
            # define channel open probability
            self.p_open = 'h * m ** 3'
            # define activation functions
            self.alpha, self.beta = {}, {}
            self.alpha['m'] =  '0.182 * (v + 38.) / (1. - exp(-(v + 38.) / 6.))' # 1/ms
            self.beta['m']  = '-0.124 * (v + 38.) / (1. - exp( (v + 38.) / 6.))' # 1/ms
            self.alpha['h'] = '-0.015 * (v + 66.) / (1. - exp( (v + 66.) / 6.))' # 1/ms
            self.beta['h']  =  '0.015 * (v + 66.) / (1. - exp(-(v + 66.) / 6.))' # 1/ms
            # temperature factor for time-scale
            self.q10 = 2.95
    ```
    """
    def __init__(self):
        """
        Will give an ``AttributeError`` if initialized as is. Should only be
        initialized from its' derived classes that implement specific ion
        channel types.
        """
        # define the channel based on user specified state variables and activations
        self.define()

        # ion that carries the channel current
        if not hasattr(self, 'ion'):
            self.ion = ''

        # temperature factor, if it exist
        if not hasattr(self, 'q10'):
            self.q10 = '1.'
        self.q10 = sp.sympify(self.q10, evaluate=False)
        # sympy temperature symbols
        assert len(self.q10.free_symbols) <= 1
        self.sp_t = list(self.q10.free_symbols)[0] if len(self.q10.free_symbols) > 0 else \
                    sp.symbols('temp')

        # the voltage variable
        self.sp_v = sp.symbols('v')

        # extract the state variables
        self.p_open = sp.sympify(self.p_open)
        self.statevars = list(self.p_open.free_symbols)

        # construct the rate functions
        if 'alpha' in self.__dict__ and 'beta' in self.__dict__:
            for svar in self.statevars:
                key = str(svar)
                self.alpha[svar] = sp.sympify(self.alpha[key], evaluate=False)
                self.beta[svar] = sp.sympify(self.beta[key], evaluate=False)
            # convert to internal representation
            self.varinf, self.tauinf = {}, {}
            for svar in self.statevars:
                self.varinf[svar] = self.alpha[svar] / (self.alpha[svar] + self.beta[svar])
                self.tauinf[svar] = (1./self.q10) / (self.alpha[svar] + self.beta[svar])
        elif 'tauinf' in self.__dict__ and 'varinf' in self.__dict__:
            for svar in self.statevars:
                key = str(svar)
                self.varinf[svar] = sp.sympify(self.varinf[key], evaluate=False)
                self.tauinf[svar] = sp.sympify(self.tauinf[key], evaluate=False) / self.q10
        else:
            raise AttributeError("Necessary attributes not defined, define either " + \
                                 "`alpha` and `beta` or `tauinf` and `varinf`.")

        # set the right hand side of the differential equation for
        # state variables
        self.fstatevar = {}
        for svar in self.statevars:
            self.fstatevar[svar] = (-svar + self.varinf[svar]) / self.tauinf[svar]

        # concentrations the ion channel depends on
        if not hasattr(self, 'conc'):
            # if concentration ions are not defined, attempt to extract them from
            # the state variable functions
            self.conc = {}
            for key, expr in self.fstatevar.items():
                self.conc |= expr.free_symbols # set union
            # remove everything that is not a concentration
            self.conc -= set(self.statevars)
            self.conc -= {self.sp_v, self.sp_t}
        # if no default concentrations are defined, default values are taken
        # from default concentration values
        if not hasattr(self.conc, 'values'):
            self.conc = {sp.symbols(str(ion)): CONC_DICT[str(ion)] for ion in self.conc}
        # sympy concentration symbols
        self.sp_c = [ion for ion in self.conc]

        # default parameters
        self.default_params = {}
        self.default_params[str(self.sp_t)] = self.t if 't' in self.__dict__ else \
                                              TEMP_DEFAULT
        try:
            self.default_params['e'] = self.e if 'e' in self.__dict__ else \
                                       E_ION_DICT[self.ion]
        except KeyError:
            warnings.warn('No default reversal potential defined.')

        # set the lambda functions for efficient numpy evaluation
        self._lambdifyChannel()

    def __getstate__(self):
        """
        remove lambdified functions from dict as they can not be pickled
        """
        d = dict(self.__dict__)

        del d['f_statevar']
        del d['f_varinf']
        del d['f_tauinf']
        del d['f_p_open']
        del d['dp_dx'], d['df_dv'], d['df_dx'], d['df_dc']

        return d

    def __setstate__(self, s):
        """
        since lambdified functions were not pickled we need to restore them
        """
        self.__dict__ = s
        self._lambdifyChannel()

    def setDefaultParams(self, **kwargs):
        """
        **kwargs
            Default values for temperature, reversal
        """
        self.default_params.update(kwargs)

    def _substituteDefaults(self, expr):
        """
        Substitute default values in input expression

        Parameters
        ----------
        expr: sympy expression
        """
        for param, val in self.default_params.items():
            expr = expr.subs(sp.symbols(param), val)
        return expr

    def _lambdifyChannel(self):
        """
        Create lambda functions based on sympy expression for relevant ion
        channel functions
        """
        # arguments for lambda function
        args = [self.sp_v] + self.statevars + self.sp_c
        args_ = [self.sp_v] + self.sp_c

        # lambdified open probability
        self.f_p_open = sp.lambdify(args, self.p_open)
        # storatestate variable function
        self.f_statevar = CallDict()
        self.f_varinf, self.f_tauinf = CallDict(), CallDict()
        # storage of derivatives
        self.dp_dx = CallDict()
        self.df_dv, self.df_dx, self.df_dc = CallDict(), CallDict(), CallDict()

        for svar, f_svar in self.fstatevar.items():
            f_svar = self._substituteDefaults(f_svar)
            varinf = self._substituteDefaults(self.varinf[svar])
            tauinf = self._substituteDefaults(self.tauinf[svar])

            # state variable function
            self.f_statevar = sp.lambdify(args, f_svar)

            # state variable activation & timescale
            self.f_varinf[svar] = sp.lambdify(args_, varinf)
            self.f_tauinf[svar] = sp.lambdify(args_, tauinf)

            # derivatives of open probability to state variables
            self.dp_dx[svar] = sp.lambdify(args, sp.diff(self.p_open, svar, 1))

            # derivatives of state variable function to voltage
            self.df_dv[svar] = sp.lambdify(args, sp.diff(f_svar, self.sp_v, 1))

            # derivatives of state variable function to state variable
            self.df_dx[svar] = sp.lambdify(args, sp.diff(f_svar, svar, 1))

            # derivatives of state variable function to concentrations
            self.df_dc[svar] = CallDict({c: sp.lambdify(args, sp.diff(f_svar, c, 1)) \
                                         for c in self.sp_c})

    def _argsAsList(self, v, w_statevar=True, **kwargs):
        """
        Converts arguments to list for lambdified functions
        """
        arg_list = [v]

        if w_statevar:
            for svar in self.statevars:
                key = str(svar)
                try:
                    arg_list.append(kwargs[key])
                except KeyError:
                    # state variable is not in kwargs
                    # set default value based on voltage
                    arg_list.append(self.f_varinf[svar](v))

        for c in self.sp_c:
            key = str(c)
            try:
                arg_list.append(kwargs[key])
            except KeyError:
                # ion is not in kwargs
                # set stored default value
                arg_list.append(self.conc[c])

        return arg_list

    def computePOpen(self, v, **kwargs):
        """
        Compute the open probability of the ion channel

        Parameters
        ----------
        v: float or `np.ndarray` of float
            The voltage at which to evaluate the open probability
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        float or `np.ndarray` of float
            The open probability
        """
        args = self._argsAsList(v, **kwargs)
        return self.f_p_open(*args)

    def computeDerivatives(self, v, **kwargs):
        """
        Compute:
        (i) the derivatives of the open probability to the state variables
        (ii) The derivatives of state functions to the voltage
        (iii) The derivatives of state functions to the state variables

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage at which to evaluate the open probability
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        tuple of three floats or three `np.ndarray`s of float
            The derivatives
        """
        args = self._argsAsList(v, **kwargs)
        return self.dp_dx(*args), self.df_dv(*args), self.df_dx(*args)

    def computeDerivativesConc(self, v, **kwargs):
        """
        Compute the derivatives of the state functions to the concentrations

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage at which to evaluate the open probability
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        tuple of three floats or three `np.ndarray`s of float
            The derivatives
        """
        args = self._argsAsList(v, **kwargs)
        return self.df_dc(*args)

    def computeVarinf(self, v):
        """
        Compute the asymptotic values for the state variables at a given
        activation level

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage at which to evaluate the open probability

        Returns
        -------
        dict of `np.ndarray` of dict of float
            The asymptotic activations, items are of same type (and shape) as `v`
        """
        args = self._argsAsList(v, w_statevar=False, **{})
        return self.f_varinf(*args)

    def computeTauinf(self, v):
        """
        Compute the time-scales for the state variables at a given
        activation level

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage at which to evaluate the open probability

        Returns
        -------
        dict of `np.ndarray` of dict of float
            The asymptotic activations, items are of same type (and shape) as `v`
        """
        args = self._argsAsList(v, w_statevar=False, **{})
        return self.f_tauinf(*args)

    def computeLinear(self, v, freqs, **kwargs):
        """
        Combute the contributions of the state variables to the linearized
        channel current

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage ``[mV]`` at which to evaluate the open probability
        freqs float, complex, or `np.ndarray` of float or complex:
            The frequencies ``[Hz]`` at which to evaluate the linearized contribution
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        float, complex or `np.ndarray` of float or complex
            The linearized current. Shape is dimension of `freqs` followed by
            the dimensions of `v`.
        """
        dp_dx, df_dv, df_dx = self.computeDerivatives(v, **kwargs)

        # broadcast frequencies
        if isinstance(freqs, np.ndarray) and isinstance(v, np.ndarray):
            freqs.reshape(freqs.shape+tuple([1]*v.ndim))

        lin_f = np.zeros_like(freqs)
        for svar, dp_dx_ in dp_dx.items():
            df_dv_ = df_dv[svar] * 1e3 # convert to 1 / s
            df_dx_ = df_dx[svar] * 1e3 # convert to 1 / s
            # add to the impedance contribution
            lin_f += dp_dx_ * df_dv_ / (freqs - df_dx_)
        return lin_f

    def computeLinearConc(self, v, freqs, ion, **kwargs):
        """
        Combute the contributions of the state variables to the linearized
        channel current

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage ``[mV]`` at which to evaluate the open probability
        freqs: float, complex, or `np.ndarray` of float or complex:
            The frequencies ``[Hz]`` at which to evaluate the linearized contribution
        ion: str
            The ion name for which to compute the linearized contribution
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        float, complex or `np.ndarray` of float or complex
            The linearized current. Shape is dimension of `freqs` followed by
            the dimensions of `v`.
        """
        dp_dx, df_dv, df_dx = self.computeDerivatives(v, **kwargs)
        df_dc = self.computeDerivativesConc(v, **kwargs)

        # broadcast frequencies
        if isinstance(freqs, np.ndarray) and isinstance(v, np.ndarray):
            freqs.reshape(freqs.shape+tuple([1]*v.ndim))

        lin_f = np.zeros_like(freqs)
        for svar, dp_dx_ in dp_dx.items():
            df_dc_ = df_dc[svar][ion] * 1e3 # convert to 1 / s
            df_dx_ = df_dx[svar]      * 1e3 # convert to 1 / s
            # add to the impedance contribution
            lin_f += dp_dx_ * df_dc_ / (freqs - df_dx_)
        return lin_f

    def _getReversal(self, e):
        if e is None:
            try:
                e = self.default_params['e']
            except KeyError:
                raise KeyError('No default reversal defined, provide value for `e`.')
        return e

    def computeLinSum(self, v, freqs, e=None, **kwargs):
        """
        Combute the linearized channel current contribution
        (without concentributions from the concentration - see `computeLinConc()`)

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage ``[mV]`` at which to evaluate the open probability
        freqs: float, complex, or `np.ndarray` of float or complex:
            The frequencies ``[Hz]`` at which to evaluate the linearized contribution
        e: float or `None`
            The reversal potential of the channel. Defaults to the value stored
            in `self.default_params['e']` if not provided.
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        float, complex or `np.ndarray` of float or complex
            The linearized current. Shape is dimension of `freqs` followed by
            the dimensions of `v`.
        """
        e = self._getReversal(e)
        return (e - v) * self.computeLinear(v, freqs, **kwargs) - \
               self.computePOpen(v, **kwargs)

    def computeLinConc(self, v, freqs, ion, e=None, **kwargs):
        """
        Combute the linearized channel current contribution from the concentrations

        Parameters
        ----------
        v: float or `np.ndarray`
            The voltage ``[mV]`` at which to evaluate the open probability
        freqs: float, complex, or `np.ndarray` of float or complex:
            The frequencies ``[Hz]`` at which to evaluate the linearized contribution
        ion: str
            The ion name for which to compute the linearized contribution
        e: float or `None`
            The reversal potential of the channel. Defaults to the value stored
            in `self.default_params['e']` if not provided.
        **kwargs
            Optional values for the state variables and concentrations.
            Broadcastable to `v` if provided

        Returns
        -------
        float, complex or `np.ndarray` of float or complex
            The linearized current. Shape is dimension of `freqs` followed by
            the dimensions of `v`.
        """
        e = self._getReversal(e)
        return (e - v) * self.computeLinearConc(v, freqs, ion, **kwargs)


    def writeModFile(self, path, g=0., e=0.):
        """
        Writes a modfile of the ion channel for simulations with neuron
        """
        cname =  self.__class__.__name__
        sv = [str(svar) for svar in self.statevars]
        cs = [str(conc) for conc in self.conc]

        modname = 'I' + cname + '.mod'
        fname = os.path.join(path, modname)

        file = open(fname, 'w')

        file.write(': This mod file is automaticaly generated by the ' +
                    '``neat.channels.ionchannels`` module\n\n')

        file.write('NEURON {\n')
        file.write('    SUFFIX I%s\n'%cname)
        if self.ion == '':
            file.write('    NONSPECIFIC_CURRENT i' + '\n')
        else:
            file.write('    USEION %s WRITE i%s\n'%(self.ion, self.ion))
        for c in cs:
            file.write('    USEION %s READ %si\n'%(c, c))
        file.write('    RANGE  g, e' + '\n')

        taustring = 'tau_' + ', tau_'.join(sv)
        varstring = '_inf, '.join(sv) + '_inf'
        file.write('    GLOBAL %s, %s\n'%(varstring, taustring))
        file.write('    THREADSAFE' + '\n')
        file.write('}\n\n')

        file.write('PARAMETER {\n')
        file.write('    g = ' + str(g*1e-6) + ' (S/cm2)' + '\n')
        file.write('    e = ' + str(e) + ' (mV)' + '\n')
        for ion in cs:
            file.write('    ' + ion + 'i (mM)' + '\n')
        file.write('    celsius (degC)\n')
        file.write('}\n\n')

        file.write('UNITS {\n')
        file.write('    (mA) = (milliamp)' + '\n')
        file.write('    (mV) = (millivolt)' + '\n')
        file.write('    (mM) = (milli/liter)' + '\n')
        file.write('}\n\n')

        file.write('ASSIGNED {\n')
        file.write('    i%s (mA/cm2)\n'%self.ion)
        for var in sv:
            file.write('    %s_inf      \n'%var)
            file.write('    tau_%s (ms) \n'%var)
        file.write('    v (mV)' + '\n')
        file.write('    %s (degC)\n'%(self.sp_t))
        file.write('}\n\n')

        file.write('STATE {\n')
        for var in sv:
            file.write('    %s\n'%var)
        file.write('}\n\n')

        calcstring = 'i%s = g * (%s) * (v - e)'%(self.ion, sp.printing.ccode(self.p_open))

        file.write('BREAKPOINT {\n')
        file.write('    SOLVE states METHOD cnexp' + '\n')
        file.write('    %s\n'%calcstring)
        file.write('}\n\n')

        concstring = 'i, '.join(cs)
        if len(cs) > 0:
            concstring = ', ' + concstring
            concstring += 'i'

        file.write('INITIAL {\n')
        file.write('    rates(v%s)\n'%concstring)
        for var in sv:
            file.write('    %s = %s_inf\n'%(var,var))
        file.write('}\n\n')

        file.write('DERIVATIVE states {\n')
        file.write('    rates(v%s)\n'%concstring)
        for var in sv:
            file.write('    %s\' = (%s_inf - %s) /  tau_%s \n'%(var,var,var,var))
        file.write('}\n\n')

        # substitution for common neuron names
        repl_pairs = [(str(c), str(c)+'i') for c in self.conc]

        file.write('PROCEDURE rates(v%s) {\n'%concstring)
        file.write('    %s = celsius\n'%str(self.sp_t))
        for var, svar in zip(sv, self.statevars):
            vi = sp.printing.ccode(self.varinf[svar])
            ti = sp.printing.ccode(self.tauinf[svar])
            for repl_pair in repl_pairs:
                vi = vi.replace(*repl_pair)
                ti = ti.replace(*repl_pair)
            file.write('    %s_inf = %s\n'%(var, vi))
            file.write('    tau_%s = %s\n'%(var, ti))
        file.write('}\n\n')

        file.close()

        return modname

    def writeCPPCode(self, path, e_rev):
        """
        Warning: concentration dependent ion channels get constant concentrations
        substituted for c++ simulation
        """

        fcc = open(os.path.join(path, 'Ionchannels.cc'), 'a')
        fh = open(os.path.join(path, 'Ionchannels.h'), 'a')
        # fstruct = open('cython_code/channelstruct.h', 'a')


        fh.write('class ' + self.__class__.__name__ + ': public IonChannel{' + '\n')
        fh.write('private:' + '\n')
        # fh.write('    double m_g_bar = 0.0, m_e_rev = %.8f;\n'%e_rev)
        for ind, varname in np.ndenumerate(self.statevars):
                fh.write('    double m_' + sp.printing.ccode(varname) +';\n')
        for ind, varname in np.ndenumerate(self.statevars):
                fh.write('    double m_' + sp.printing.ccode(varname) + '_inf, m_tau_' + sp.printing.ccode(varname) + ';\n')
        for ind, varname in np.ndenumerate(self.statevars):
                fh.write('    double m_v_' + sp.printing.ccode(varname) + '= 10000.;\n')
        fh.write('    double m_p_open_eq = 0.0, m_p_open = 0.0;\n')
        fh.write('public:' + '\n')
        fh.write('    void calcFunStatevar(double v) override;' + '\n')
        fh.write('    double calcPOpen() override;' + '\n')
        fh.write('    void setPOpen() override;' + '\n')
        fh.write('    void setPOpenEQ(double v) override;' + '\n')
        fh.write('    void advance(double dt) override;' + '\n')
        fh.write('    double getCond() override;' + '\n')
        fh.write('    double getCondNewton() override;' + '\n')
        fh.write('    double f(double v) override;' + '\n')
        fh.write('    double DfDv(double v) override;' + '\n')
        fh.write('    void setfNewtonConstant(double* vs, int v_size) override;' + '\n')
        fh.write('    double fNewton(double v) override;' + '\n')
        fh.write('    double DfDvNewton(double v) override;' + '\n')
        fh.write('};' + '\n')

        fcc.write('void ' + self.__class__.__name__ + '::calcFunStatevar(double v){' + '\n')
        for ind, varname in np.ndenumerate(self.statevars):
            tauinf = self.tauinf[varname]
            varinf = self.varinf[varname]
            varinf_ = self._substituteDefaults(varinf)
            tauinf_ = self._substituteDefaults(tauinf)
            fcc.write('    m_' + sp.printing.ccode(varname) + '_inf = ' + sp.printing.ccode(varinf_) + ';' + '\n')
            # if self.varinf.shape[1] == 2 and ind == (0,0):
            if str(varname) == 'm':
                # instantaneous approximation possible if statevar is activation (denoted by 'm')
                fcc.write('    if(m_instantaneous)' + '\n')
                fcc.write('        m_tau_' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(sp.Float(1e-5))  + ';\n')
                fcc.write('    else' + '\n')
                fcc.write('        m_tau_' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(tauinf_) + ';\n')
            else:
                fcc.write('    m_tau_' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(tauinf_) + ';\n')
        fcc.write('}' + '\n')

        fcc.write('double ' + self.__class__.__name__ + '::calcPOpen(){' + '\n')
        expr = copy.deepcopy(self.p_open)
        for ind, varname in np.ndenumerate(self.statevars):
            symb = sp.symbols('m_' + sp.printing.ccode(varname))
            expr = expr.subs(varname, symb)
        fcc.write('    return ' + sp.printing.ccode(expr) + ';' + '\n')
        fcc.write('}' + '\n')

        fcc.write('void ' + self.__class__.__name__ + '::setPOpen(){' + '\n')
        fcc.write('    m_p_open = calcPOpen();' + '\n')
        fcc.write('}' + '\n')

        fcc.write('void ' + self.__class__.__name__ + '::setPOpenEQ(double v){' + '\n')
        fcc.write('    calcFunStatevar(v);' + '\n')
        fcc.write('')
        expr = copy.deepcopy(self.p_open)
        for ind, varname in np.ndenumerate(self.statevars):
            symb = sp.symbols('m_' + sp.printing.ccode(varname) + '_inf')
            expr = expr.subs(varname, symb)
            fcc.write('    m_' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(symb) + ';\n')
        fcc.write('    m_p_open_eq =' + sp.printing.ccode(expr) + ';' + '\n')
        fcc.write('}' + '\n')

        fcc.write('void ' + self.__class__.__name__ + '::advance(double dt){' + '\n')
        for ind, varname in np.ndenumerate(self.statevars):
            tauinf = self.tauinf[varname]
            varinf = self.varinf[varname]
            varname_ = 'm_' + sp.printing.ccode(varname)
            varname_inf = 'm_' + sp.printing.ccode(varname) + '_inf'
            varname_tau = 'm_tau_' + sp.printing.ccode(varname)
            propname = 'p0_' + sp.printing.ccode(varname)
            # fcc.write('    ' + varname + ' += dt * (' + varname_inf + ' - ' + varname + ') / ' + varname_tau + ';' + '\n')
            fcc.write('    double ' + propname + ' = exp(-dt / ' + varname_tau + ');\n')
            fcc.write('    ' + varname_ + ' *= ' + propname + ' ;\n')
            fcc.write('    ' + varname_ + ' += (1. - ' + propname + ' ) *  ' + varname_inf + ';\n')
        fcc.write('}' + '\n')


        # self.exp_aux = np.exp(-dt/self.tauinf_aux)
        # # advance the variables
        # self.sv *= self.exp_aux
        # self.sv += (1.-self.exp_aux) * self.svinf_aux

        fcc.write('double ' + self.__class__.__name__ + '::getCond(){' + '\n')
        fcc.write('    return m_g_bar * (m_p_open - m_p_open_eq);' + '\n')
        fcc.write('}' + '\n')

        fcc.write('double ' + self.__class__.__name__ + '::getCondNewton(){' + '\n')
        fcc.write('    return m_g_bar;' + '\n')
        fcc.write('}' + '\n')

        # function for temporal integration
        fcc.write('double ' + self.__class__.__name__ + '::f(double v){' + '\n')
        fcc.write('    return (m_e_rev - v);' + '\n')
        fcc.write('}' + '\n')

        fcc.write('double ' + self.__class__.__name__ + '::DfDv(double v){' + '\n')
        fcc.write('    return -1.;' + '\n')
        fcc.write('}' + '\n')

        # set voltage values to evaluate at constant voltage during newton iteration
        fcc.write('void ' + self.__class__.__name__ + '::setfNewtonConstant(double* vs, int v_size){' + '\n')
        fcc.write('    if(v_size != %d)'%len(self.statevars) + '\n')
        fcc.write('        cerr << "input arg [vs] has incorrect size, ' + \
                  'should have same size as number of channel state variables" << endl' + ';\n')
        for ii, var in enumerate(self.statevars):
            fcc.write('    m_v_' + sp.printing.ccode(var) + ' = vs[%d]'%ii + ';\n')
        fcc.write('}' + '\n')

        # functions for solving Newton iteration
        fcc.write('double ' + self.__class__.__name__ + '::fNewton(double v){' + '\n')
        p_o = self.p_open
        for ind, varname in np.ndenumerate(self.statevars):
            v_var = sp.symbols('v_' + str(varname))
            # substitute voltage symbol in the activation
            varinf_ = self._substituteDefaults(self.varinf[varname]).subs(self.sp_v, v_var)
            # assign dynamic or fixed voltage to the activation
            fcc.write('    double ' + sp.printing.ccode(v_var) + ';\n')
            fcc.write('    if(m_' + sp.printing.ccode(v_var) + ' > 1000.){' + '\n')
            fcc.write('        ' + sp.printing.ccode(v_var) + ' = v' + ';\n')
            fcc.write('    } else{' + '\n')
            fcc.write('        ' + sp.printing.ccode(v_var) + ' = m_' + sp.printing.ccode(v_var) + ';\n')
            fcc.write('    }' + '\n')
            fcc.write('    double ' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(varinf_) + ';\n')
            # p_o = p_o.subs(self.statevars[ind], varinf_)

        fcc.write('    return (m_e_rev - v) * (' + sp.printing.ccode(p_o) + ' - m_p_open_eq)' + ';\n')
        fcc.write('}' + '\n')

        fcc.write('double ' + self.__class__.__name__ + '::DfDvNewton(double v){' + '\n')
        p_o = self.p_open
        # compute partial derivatives
        dp_o = np.zeros_like(self.statevars)
        for ind, varname in np.ndenumerate(self.statevars):
            dp_o[ind] = sp.diff(p_o, varname, 1)
        # print derivatives
        for ind, varname in np.ndenumerate(self.statevars):
            v_var = sp.symbols('v_' + str(varname))
            # substitute voltage symbol in the activation
            varinf_ = self._substituteDefaults(self.varinf[varname]).subs(self.sp_v, v_var)
            dvarinf_dv = sp.diff(varinf_, v_var, 1)
            # compute derivative
            fcc.write('    double ' + sp.printing.ccode(v_var) + ';\n')
            fcc.write('    double d' + sp.printing.ccode(varname) + '_dv;\n')
            fcc.write('    if(m_' + sp.printing.ccode(v_var) + ' > 1000.){' + '\n')
            fcc.write('        ' + sp.printing.ccode(v_var) + ' = v' + ';\n')
            fcc.write('        d' + sp.printing.ccode(varname) + '_dv = ' + sp.printing.ccode(dvarinf_dv) + ';\n')
            fcc.write('    } else{' + '\n')
            fcc.write('        ' + sp.printing.ccode(v_var) + ' = m_' + sp.printing.ccode(v_var) + ';\n')
            fcc.write('        d' + sp.printing.ccode(varname) + '_dv = 0;\n')
            fcc.write('    }' + '\n')
            fcc.write('    double ' + sp.printing.ccode(varname) + ' = ' + sp.printing.ccode(varinf_) + ';\n')


        expr_str = '+'.join([sp.printing.ccode(dp_o_) + ' * d' + sp.printing.ccode(varname) + '_dv' for dp_o_, varname in zip(dp_o, self.statevars)])

        fcc.write('    return -1. * (' + sp.printing.ccode(p_o) + ' - m_p_open_eq) + (' + expr_str + ') * (m_e_rev - v)' + ';\n')
        fcc.write('}' + '\n')




        fh.write('\n')
        fcc.write('\n')
        # fstruct.write('    ' + self.__class__.__name__ + ' ' + self.__class__.__name__ + '_;' + '\n')

        fh.close()
        fcc.close()
        # fstruct.close()


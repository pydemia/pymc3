"""Sequential Monte Carlo sampler also known as
Adaptive Transitional Markov Chain Monte Carlo sampler.

Runs on any pymc3 model.

Created on March, 2016

Various significant updates July, August 2016

Made pymc3 compatible November 2016
Renamed to SMC and further improvements March 2017

@author: Hannes Vasyura-Bathke
"""
import numpy as np
import pymc3 as pm
from tqdm import tqdm

import theano

from ..model import modelcontext
from ..vartypes import discrete_types
from ..theanof import inputvars, make_shared_replacements, join_nonshared_inputs
import numpy.random as nr

from .metropolis import MultivariateNormalProposal
from .arraystep import metrop_select
from ..backends import smc_text as atext

__all__ = ['SMC', 'sample_smc']

proposal_dists = {'MultivariateNormal': MultivariateNormalProposal}


def choose_proposal(proposal_name, scale=1.):
    """Initialize and select proposal distribution.

    Parameters
    ----------
    proposal_name : string
        Name of the proposal distribution to initialize
    scale : float or :class:`numpy.ndarray`

    Returns
    -------
    class:`pymc3.Proposal` Object
    """
    return proposal_dists[proposal_name](scale)


class SMC(atext.ArrayStepSharedLLK):
    """Adaptive Transitional Markov-Chain Monte-Carlo sampler class.

    Creates initial samples and framework around the (C)ATMIP parameters

    Parameters
    ----------
    vars : list
        List of variables for sampler
    out_vars : list
        List of output variables for trace recording. If empty unobserved_RVs are taken.
    n_steps : int
        The number of steps of a Markov Chain. If `tune_interval > 0` `n_steps` will be used for
        the first and last stages, and the number of steps of the intermediate states will be
        determined automatically. Otherwise, if `tune_interval = 0`,  `n_steps` will be used for
        all stages.
    scaling : float
        Factor applied to the proposal distribution i.e. the step size of the Markov Chain. Only
        works if `tune_interval=0` otherwise it will be determined automatically
    p_acc_rate : float
        Probability of not accepting a step. Used to compute `n_steps` when `tune_interval > 0`.
        It should be between 0 and 1.
    covariance : :class:`numpy.ndarray`
        (chains x chains)
        Initial Covariance matrix for proposal distribution, if None - identity matrix taken
    likelihood_name : string
        name of the :class:`pymc3.deterministic` variable that contains the model likelihood.
        Defaults to 'l_like__'
    proposal_name :
        Type of proposal distribution, see smc.proposal_dists.keys() for options
    tune_interval : int
        Number of steps to tune for. If tune=0 no tunning will be used. Default 10. SMC tunes two
        related quantities, the scaling of the proposal distribution (i.e. the step size of Markov
        Chain) and the number of steps of a Markov Chain (i.e. `n_steps`).
    threshold : float
        Determines the change of beta from stage to stage, i.e.indirectly the number of stages,
        the higher the value of threshold the higher the number of stage. Defaults to 0.5.
        It should be between 0 and 1.
    check_bound : boolean
        Check if current sample lies outside of variable definition speeds up computation as the
        forward model wont be executed. Default: True
    model : :class:`pymc3.Model`
        Optional model for sampling step. Defaults to None (taken from context).
    random_seed : int
        Optional to set the random seed.  Necessary for initial population.

    References
    ----------
    .. [Ching2007] Ching, J. and Chen, Y. (2007).
        Transitional Markov Chain Monte Carlo Method for Bayesian Model Updating, Model Class
        Selection, and Model Averaging. J. Eng. Mech., 10.1061/(ASCE)0733-9399(2007)133:7(816),
        816-832. `link <http://ascelibrary.org/doi/abs/10.1061/%28ASCE%290733-9399
        %282007%29133:7%28816%29>`__
    """
    default_blocked = True

    def __init__(self, vars=None, out_vars=None, n_steps=25, scaling=1., p_acc_rate=0.001,
                 covariance=None, likelihood_name='l_like__', proposal_name='MultivariateNormal',
                 tune_interval=10, threshold=0.5, check_bound=True, model=None, random_seed=-1):

        if random_seed != -1:
            nr.seed(random_seed)

        model = modelcontext(model)

        if vars is None:
            vars = model.vars

        vars = inputvars(vars)

        if out_vars is None:
            if not any(likelihood_name == RV.name for RV in model.unobserved_RVs):
                pm._log.info('Adding model likelihood to RVs!')
                with model:
                    llk = pm.Deterministic(likelihood_name, model.logpt)
            else:
                pm._log.info('Using present model likelihood!')

            out_vars = model.unobserved_RVs

        out_varnames = [out_var.name for out_var in out_vars]

        if covariance is None and proposal_name == 'MultivariateNormal':
            self.covariance = np.eye(sum(v.dsize for v in vars))
            scale = self.covariance
        elif covariance is None:
            scale = np.ones(sum(v.dsize for v in vars))
        else:
            scale = covariance

        self.vars = vars
        self.proposal_name = proposal_name
        self.proposal_dist = choose_proposal(self.proposal_name, scale=scale)
        self.scaling = np.atleast_1d(scaling)
        self.check_bnd = check_bound
        self.tune_interval = tune_interval
        self.steps_until_tune = tune_interval
        self.population = [model.test_point]
        self.n_steps = n_steps
        self.n_steps_final = n_steps
        self.p_acc_rate = p_acc_rate
        self.stage_sample = 0
        self.accepted = 0
        self.beta = 0
        #self.sjs = 1
        self.stage = 0
        self.chain_index = 0
        self.threshold = threshold
        self.likelihood_name = likelihood_name
        self._llk_index = out_varnames.index(likelihood_name)
        self.discrete = np.concatenate([[v.dtype in discrete_types] * (v.dsize or 1) for v in vars])
        self.any_discrete = self.discrete.any()
        self.all_discrete = self.discrete.all()

        shared = make_shared_replacements(vars, model)
        self.logp_forw = logp_forw(out_vars, vars, shared)
        self.check_bnd = logp_forw([model.varlogpt], vars, shared)

        super(SMC, self).__init__(vars, out_vars, shared)

    def astep(self, q0):
        if self.stage == 0:
            l_new = self.logp_forw(q0)
            q_new = q0

        else:
            if not self.steps_until_tune and self.tune_interval:

                # Tune scaling parameter
                acc_rate = self.accepted / float(self.tune_interval)
                self.scaling = tune(acc_rate)
                # compute n_steps
                if self.accepted == 0:
                    acc_rate = 1 / float(self.tune_interval)
                self.n_steps = 1 + (np.ceil(np.log(self.p_acc_rate) /
                                            np.log(1 - acc_rate)).astype(int))
                # Reset counter
                self.steps_until_tune = self.tune_interval
                self.accepted = 0
                self.stage_sample = 0

            if not self.stage_sample:
                self.proposal_samples_array = self.proposal_dist(self.n_steps)

            delta = self.proposal_samples_array[self.stage_sample, :] * self.scaling

            if self.any_discrete:
                if self.all_discrete:
                    delta = np.round(delta, 0)
                    q0 = q0.astype(int)
                    q = (q0 + delta).astype(int)
                else:
                    delta[self.discrete] = np.round(delta[self.discrete], 0).astype(int)
                    q = q0 + delta
                    q = q[self.discrete].astype(int)
            else:
                q = q0 + delta

            l0 = self.chain_previous_lpoint[self.chain_index]

            if self.check_bnd:
                varlogp = self.check_bnd(q)

                if np.isfinite(varlogp):
                    logp = self.logp_forw(q)
                    q_new, accepted = metrop_select(
                        self.beta * (logp[self._llk_index] - l0[self._llk_index]), q, q0)

                    if accepted:
                        self.accepted += 1
                        l_new = logp
                        self.chain_previous_lpoint[self.chain_index] = l_new
                    else:
                        l_new = l0
                else:
                    q_new = q0
                    l_new = l0

            else:
                logp = self.logp_forw(q)
                q_new, accepted = metrop_select(
                    self.beta * (logp[self._llk_index] - l0[self._llk_index]), q, q0)

                if accepted:
                    self.accepted += 1
                    l_new = logp
                    self.chain_previous_lpoint[self.chain_index] = l_new
                else:
                    l_new = l0

            self.steps_until_tune -= 1
            self.stage_sample += 1

            # reset sample counter
            if self.stage_sample == self.n_steps:
                self.stage_sample = 0

        return q_new, l_new

    def calc_beta(self):
        """Calculate next tempering beta and importance weights based on current beta and sample
        likelihoods.

        Returns
        -------
        beta(m+1) : scalar, float
            tempering parameter of the next stage
        beta(m) : scalar, float
            tempering parameter of the current stage
        weights : :class:`numpy.ndarray`
            Importance weights (floats)
        """
        low_beta = old_beta = self.beta
        up_beta = 2.
        rN = int(len(self.likelihoods) * self.threshold)

        while up_beta - low_beta > 1e-6:
            new_beta = (low_beta + up_beta) / 2.
            weights_un = np.exp((new_beta - old_beta) * (self.likelihoods - self.likelihoods.max()))

            weights = weights_un / np.sum(weights_un)
            ESS = int(1 / np.sum(weights ** 2))
            #ESS = int(1 / np.max(weights))
            if ESS == rN:
                break
            elif ESS < rN:
                up_beta = new_beta
            else:
                low_beta = new_beta

        return new_beta, old_beta, weights#, np.mean(weights_un)

    def calc_covariance(self):
        """Calculate trace covariance matrix based on importance weights.

        Returns
        -------
        cov : :class:`numpy.ndarray`
            weighted covariances (NumPy > 1.10. required)
        """
        cov = np.cov(self.array_population, aweights=self.weights.ravel(), bias=False, rowvar=0)
        if np.isnan(cov).any() or np.isinf(cov).any():
            raise ValueError('Sample covariances not valid! Likely "chains" is too small!')
        return np.atleast_2d(cov)

    def select_end_points(self, mtrace, chains):
        """Read trace results (variables and model likelihood) and take end points for each chain
        and set as start population for the next stage.

        Parameters
        ----------
        mtrace : :class:`.base.MultiTrace`

        Returns
        -------
        population : list
            of :func:`pymc3.Point` dictionaries
        array_population : :class:`numpy.ndarray`
            Array of trace end-points
        likelihoods : :class:`numpy.ndarray`
            Array of likelihoods of the trace end-points
        """
        array_population = np.zeros((chains, self.ordering.size))
        n_steps = len(mtrace)

        # collect end points of each chain and put into array
        for var, slc, shp, _ in self.ordering.vmap:
            slc_population = mtrace.get_values(varname=var, burn=n_steps - 1, combine=True)
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(slc_population).T
            else:
                array_population[:, slc] = slc_population
        # get likelihoods
        likelihoods = mtrace.get_values(varname=self.likelihood_name,
                                        burn=n_steps - 1, combine=True)

        # map end array_endpoints to dict points
        population = [self.bij.rmap(row) for row in array_population]

        return population, array_population, likelihoods

    def get_chain_previous_lpoint(self, mtrace, chains):
        """Read trace results and take end points for each chain and set as previous chain result
        for comparison of metropolis select.

        Parameters
        ----------
        mtrace : :class:`.base.MultiTrace`

        Returns
        -------
        chain_previous_lpoint : list
            all unobservedRV values, including dataset likelihoods
        """
        array_population = np.zeros((chains, self.lordering.size))
        n_steps = len(mtrace)
        for _, slc, shp, _, var in self.lordering.vmap:
            slc_population = mtrace.get_values(varname=var, burn=n_steps - 1, combine=True)
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(slc_population).T
            else:
                array_population[:, slc] = slc_population

        return [self.lij.rmap(row) for row in array_population[self.resampling_indexes, :]]

    def mean_end_points(self):
        """Calculate mean of the end-points and return point.

        Returns
        -------
        Dictionary of trace variables
        """
        return self.bij.rmap(self.array_population.mean(axis=0))

    def resample(self, chains):
        """Resample pdf based on importance weights. based on Kitagawas deterministic resampling
        algorithm.

        Returns
        -------
        outindex : :class:`numpy.ndarray`
            Array of resampled trace indexes
        """
        parents = np.arange(chains)
        N_childs = np.zeros(chains, dtype=int)

        cum_dist = np.cumsum(self.weights)
        u = (parents + np.random.rand()) / chains
        j = 0
        for i in parents:
            while u[i] > cum_dist[j]:
                j += 1

            N_childs[j] += 1

        indx = 0
        outindx = np.zeros(chains, dtype=int)
        for i in parents:
            if N_childs[i] > 0:
                for j in range(indx, (indx + N_childs[i])):
                    outindx[j] = parents[i]

            indx += N_childs[i]

        return outindx


def sample_smc(samples=1000, chains=100, step=None, start=None, homepath=None, stage=0, cores=1,
               progressbar=False, model=None, random_seed=-1, rm_flag=True, **kwargs):
    """Sequential Monte Carlo sampling

    Samples the parameter space using a `chains` number of parallel Metropolis chains.
    Once finished, the sampled traces are evaluated:

    (1) Based on the likelihoods of the final samples, chains are weighted
    (2) the weighted covariance of the ensemble is calculated and set as new proposal distribution
    (3) the variation in the ensemble is calculated and also the next tempering parameter (`beta`)
    (4) New `chains` Markov chains are seeded on the traces with high weight for a given number of
        iterations, the iterations can be computed automatically.
    (5) Repeat until `beta` > 1.

    Parameters
    ----------
    samples : int
        The number of samples to draw from the posterior (i.e. last stage). Defaults to 1000.
    chains : int
        Number of chains used to store samples in backend.
    step : :class:`SMC`
        SMC initialization object
    start : List of dictionaries
        with length of (`chains`). Starting points in parameter space (or partial point)
        Defaults to random draws from variables (defaults to empty dict)
    homepath : string
        Result_folder for storing stages, will be created if not existing.
    stage : int
        Stage where to start or continue the calculation. It is possible to continue after
        completed stages (`stage` should be the number of the completed stage + 1). If None the
        start will be at `stage=0`.
    cores : int
        The number of cores to be used in parallel. Be aware that Theano has internal
        parallelization. Sometimes this is more efficient especially for simple models.
        `chains / cores` has to be an integer number!
    progressbar : bool
        Flag for displaying a progress bar
    model : :class:`pymc3.Model`
        (optional if in `with` context) has to contain deterministic variable name defined under
        `step.likelihood_name` that contains the model likelihood
    random_seed : int or list of ints
        A list is accepted, more if `cores` is greater than one.
    rm_flag : bool
        If True existing stage result folders are being deleted prior to sampling.

    References
    ----------
    .. [Minson2013] Minson, S. E. and Simons, M. and Beck, J. L., (2013),
        Bayesian inversion for finite fault earthquake source models I- Theory and algorithm.
        Geophysical Journal International, 2013, 194(3), pp.1701-1726,
        `link <https://gji.oxfordjournals.org/content/194/3/1701.full>`__
    """

    model = modelcontext(model)

    if random_seed != -1:
        nr.seed(random_seed)

    if homepath is None:
        raise TypeError('Argument `homepath` should be path to result_directory.')

    if cores > 1:
        if not (chains / float(cores)).is_integer():
            raise TypeError('chains / cores has to be a whole number!')

    if start is not None:
        if len(start) != chains:
            raise TypeError('Argument `start` should have dicts equal the '
                            'number of chains (`chains`)')
        else:
            step.population = start

    if not any(step.likelihood_name in var.name for var in model.deterministics):
        raise TypeError('Model (deterministic) variables need to contain a variable {} as defined '
                        'in `step`.'.format(step.likelihood_name))

    stage_handler = atext.TextStage(homepath)

    if progressbar and cores > 1:
        progressbar = False

    if stage == 0:
        # continue or start initial stage
        step.stage = stage
        draws = 1
    else:
        step = stage_handler.load_atmip_params(stage, model=model)
        draws = step.n_steps

    stage_handler.clean_directory(stage, None, rm_flag)

    x_chains = stage_handler.recover_existing_results(stage, draws, chains, step)

    step.resampling_indexes = np.arange(chains)
    step.proposal_samples_array = step.proposal_dist(chains)
    step.population = _initial_population(samples, chains, model, step.vars)

    with model:
        while step.beta < 1:
            if step.stage == 0:
                # Initial stage
                pm._log.info('Sample initial stage: ...')
                draws = 1
            else:
                draws = step.n_steps

            pm._log.info('Beta: %f Stage: %i' % (step.beta, step.stage))

            # Metropolis sampling intermediate stages
            x_chains = stage_handler.clean_directory(step.stage, x_chains, rm_flag)
            sample_args = {'draws': draws,
                           'step': step,
                           'stage_path': stage_handler.stage_path(step.stage),
                           'progressbar': progressbar,
                           'model': model,
                           'n_jobs': cores,
                           'x_chains': x_chains,
                           'chains': chains}

            _iter_parallel_chains(**sample_args)

            mtrace = stage_handler.load_multitrace(step.stage)

            step.population, step.array_population, step.likelihoods = step.select_end_points(
                mtrace, chains)

            step.beta, step.old_beta, step.weights = step.calc_beta()
            #step.beta, step.old_beta, step.weights, sj = step.calc_beta()
            #step.sjs *= sj

            if step.beta > 1.:
                pm._log.info('Beta > 1.: %f' % step.beta)
                step.beta = 1.
                stage_handler.dump_atmip_params(step)
                if stage == -1:
                    x_chains = []
                else:
                    x_chains = None
            else:
                step.covariance = step.calc_covariance()
                step.proposal_dist = choose_proposal(step.proposal_name, scale=step.covariance)
                step.resampling_indexes = step.resample(chains)
                step.chain_previous_lpoint = step.get_chain_previous_lpoint(mtrace, chains)

                stage_handler.dump_atmip_params(step)

                step.stage += 1

        # Metropolis sampling final stage
        pm._log.info('Sample final stage')
        step.stage = -1
        x_chains = stage_handler.clean_directory(step.stage, x_chains, rm_flag)
        weights_un = np.exp((1 - step.old_beta) * (step.likelihoods - step.likelihoods.max()))
        step.weights = weights_un / np.sum(weights_un)
        step.covariance = step.calc_covariance()
        step.proposal_dist = choose_proposal(step.proposal_name, scale=step.covariance)
        step.resampling_indexes = step.resample(chains)
        step.chain_previous_lpoint = step.get_chain_previous_lpoint(mtrace, chains)

        x_chains = nr.randint(0, chains, size=samples)

        sample_args['draws'] = step.n_steps_final
        sample_args['step'] = step
        sample_args['stage_path'] = stage_handler.stage_path(step.stage)
        sample_args['x_chains'] = x_chains
        _iter_parallel_chains(**sample_args)

        stage_handler.dump_atmip_params(step)

        #model.marginal_likelihood = step.sjs
        return stage_handler.create_result_trace(step.stage,
                                                 step=step,
                                                 model=model)


def _initial_population(samples, chains, model, variables):
    """
    Create an initial population from the prior
    """
    population = []
    init_rnd = {}
    start = model.test_point
    for v in variables:
        if pm.util.is_transformed_name(v.name):
            trans = v.distribution.transform_used.forward_val
            init_rnd[v.name] = trans(v.distribution.dist.random(size=chains, point=start))
        else:
            init_rnd[v.name] = v.random(size=chains, point=start)

    for i in range(chains):
        population.append(pm.Point({v.name: init_rnd[v.name][i] for v in variables}, model=model))

    return population


def _sample(draws, step=None, start=None, trace=None, chain=0, progressbar=True, model=None,
            random_seed=-1, chain_idx=0):

    sampling = _iter_sample(draws, step, start, trace, chain, model, random_seed, chain_idx)

    if progressbar:
        sampling = tqdm(sampling, total=draws)

    try:
        for strace in sampling:
            pass

    except KeyboardInterrupt:
        pass

    return chain


def _iter_sample(draws, step, start=None, trace=None, chain=0, model=None, random_seed=-1, chain_idx=0):
    """
    Modified from :func:`pymc3.sampling._iter_sample` to be more efficient with SMC algorithm.
    """
    model = modelcontext(model)
    draws = int(draws)
    if draws < 1:
        raise ValueError('Argument `draws` should be above 0.')

    if start is None:
        start = {}

    if random_seed != -1:
        nr.seed(random_seed)

    try:
        step = pm.step_methods.CompoundStep(step)
    except TypeError:
        pass

    point = pm.Point(start, model=model)
    step.chain_index = chain
    trace.setup(draws, chain_idx)
    for i in range(draws):
        point, out_list = step.step(point)
        trace.record(out_list)
        yield trace


def _work_chain(work):
    """Wrapper function for parallel execution of _sample i.e. the Markov Chains.

    Parameters
    ----------
    work : List
        Containing all the information that is unique for each Markov Chain
        i.e. [:class:'SMC', chain_number(int), sampling index(int), start_point(dictionary)]

    Returns
    -------
    chain : int
        Index of chain that has been sampled
    """
    return _sample(*work)


def _iter_parallel_chains(draws, step, stage_path, progressbar, model, n_jobs, chains,
                          x_chains=None):
    """Do Metropolis sampling over all the x_chains with each chain being sampled 'draws' times.
    Parallel execution according to n_jobs.
    """
    if x_chains is None:
        x_chains = range(chains)

    chain_idx = range(0, len(x_chains))
    pm._log.info('Initializing chain traces ...')

    max_int = np.iinfo(np.int32).max

    random_seeds = nr.randint(1, max_int, size=len(x_chains))
    pm._log.info('Sampling ...')

    work = [(draws,
             step,
             step.population[step.resampling_indexes[chain]],
             atext.TextChain(stage_path, model=model),
             chain,
             False,
             model,
             rseed,
             chain_idx) for chain, rseed, chain_idx in zip(x_chains, random_seeds, chain_idx)]

    if draws < 10:
        chunksize = n_jobs
    else:
        chunksize = 1

    p = atext.paripool(_work_chain, work, chunksize=chunksize, nprocs=n_jobs)

    if n_jobs == 1 and progressbar:
        p = tqdm(p, total=len(x_chains))

    for _ in p:
        pass


def tune(acc_rate):
    """Tune adaptively based on the acceptance rate.

    Parameters
    ----------
    acc_rate: float
        Acceptance rate of the Metropolis sampling

    Returns
    -------
    scaling: float
    """
    # a and b after Muto & Beck 2008 .
    a = 1. / 9
    b = 8. / 9
    return (a + b * acc_rate) ** 2


def logp_forw(out_vars, vars, shared):
    """Compile Theano function of the model and the input and output variables.

    Parameters
    ----------
    out_vars : List
        containing :class:`pymc3.Distribution` for the output variables
    vars : List
        containing :class:`pymc3.Distribution` for the input variables
    shared : List
        containing :class:`theano.tensor.Tensor` for depended shared data
    """
    out_list, inarray0 = join_nonshared_inputs(out_vars, vars, shared)
    f = theano.function([inarray0], out_list)
    f.trust_input = True
    return f

# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Affero General Public License v3, see <https://www.gnu.org/licenses/>.

from copy import deepcopy
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Union
from warnings import warn

import numpy as np
import torch
from torch import Tensor, nn

from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.types import Shape, Array
from sbi.utils import del_entries, optimize_potential_fn, rejection_sample
from sbi.utils.torchutils import (
    ScalarFloat,
    atleast_2d,
    ensure_theta_batched,
    atleast_2d_float32_tensor,
)

from sbi.vi.sampling import (
    importance_resampling,
    independent_mh,
    random_direction_slice_sampler,
)

from tqdm import tqdm


class LikelihoodBasedPosterior(NeuralPosterior):
    r"""Posterior $p(\theta|x)$ with `log_prob()` and `sample()` methods, obtained with
    SNLE.<br/><br/>
    SNLE trains a neural network to approximate the likelihood $p(x|\theta)$. The
    `SNLE_Posterior` class wraps the trained network such that one can directly evaluate
    the unnormalized posterior log probability $p(\theta|x) \propto p(x|\theta) \cdot
    p(\theta)$ and draw samples from the posterior with MCMC.<br/><br/>
    The neural network itself can be accessed via the `.net` attribute.
    """

    def __init__(
        self,
        method_family: str,
        neural_net: nn.Module,
        prior,
        x_shape: torch.Size,
        sample_with: str = "mcmc",
        mcmc_method: str = "slice_np",
        mcmc_parameters: Optional[Dict[str, Any]] = None,
        rejection_sampling_parameters: Optional[Dict[str, Any]] = None,
        vi_parameters: Optional[Dict[str, Any]] = None,
        device: str = "cpu",
    ):
        """
        Args:
            method_family: One of snpe, snl, snre_a or snre_b.
            neural_net: A classifier for SNRE, a density estimator for SNPE and SNL.
            prior: Prior distribution with `.log_prob()` and `.sample()`.
            x_shape: Shape of the simulated data. It can differ from the
                observed data the posterior is conditioned on later in the batch
                dimension. If it differs, the additional entries are interpreted as
                independent and identically distributed data / trials. I.e., the data is
                assumed to be generated based on the same (unknown) model parameters or
                experimental condations.
            sample_with: Method to use for sampling from the posterior. Must be one of
                [`mcmc` | `rejection`].
            mcmc_method: Method used for MCMC sampling, one of `slice_np`, `slice`,
                `hmc`, `nuts`. Currently defaults to `slice_np` for a custom numpy
                implementation of slice sampling; select `hmc`, `nuts` or `slice` for
                Pyro-based sampling.
            mcmc_parameters: Dictionary overriding the default parameters for MCMC.
                The following parameters are supported: `thin` to set the thinning
                factor for the chain, `warmup_steps` to set the initial number of
                samples to discard, `num_chains` for the number of chains,
                `init_strategy` for the initialisation strategy for chains; `prior`
                will draw init locations from prior, whereas `sir` will use Sequential-
                Importance-Resampling using `init_strategy_num_candidates` to find init
                locations.
            rejection_sampling_parameters: Dictionary overriding the default parameters
                for rejection sampling. The following parameters are supported:
                `proposal` as the proposal distribtution (default is the prior).
                `max_sampling_batch_size` as the batchsize of samples being drawn from
                the proposal at every iteration. `num_samples_to_find_max` as the
                number of samples that are used to find the maximum of the
                `potential_fn / proposal` ratio. `num_iter_to_find_max` as the number
                of gradient ascent iterations to find the maximum of that ratio. `m` as
                multiplier to that ratio.
            vi_parameters: Dictionary overriding the default parameters for Variational
                Inference TODO write docstring when fixed
            device: Training device, e.g., cpu or cuda:0.
        """

        kwargs = del_entries(locals(), entries=("self", "__class__"))
        super().__init__(**kwargs)

        self._purpose = (
            "It provides MCMC to .sample() from the posterior and "
            "can evaluate the _unnormalized_ posterior density with .log_prob()."
        )

    def log_prob(
        self, theta: Tensor, x: Optional[Tensor] = None, track_gradients: bool = False
    ) -> Tensor:
        r"""
        Returns the log-probability of $p(x|\theta) \cdot p(\theta).$

        This corresponds to an **unnormalized** posterior log-probability.

        Args:
            theta: Parameters $\theta$.
            x: Conditioning context for posterior $p(\theta|x)$. If not provided,
                fall back onto `x` passed to `set_default_x()`.
            track_gradients: Whether the returned tensor supports tracking gradients.
                This can be helpful for e.g. sensitivity analysis, but increases memory
                consumption.

        Returns:
            `(len(θ),)`-shaped log-probability $\log(p(x|\theta) \cdot p(\theta))$.

        """

        # TODO Train exited here, entered after sampling?
        self.net.eval()

        theta, x = self._prepare_theta_and_x_for_log_prob_(theta, x)

        if self.sample_with == "mcmc" or self.sample_with == "rejection":
            # Calculate likelihood over trials and in one batch.
            warn(
                "The log probability from SNL is only correct up to a normalizing constant."
            )
            log_likelihood_trial_sum = self._log_likelihoods_over_trials(
                x=x.to(self._device),
                theta=theta.to(self._device),
                net=self.net,
                track_gradients=track_gradients,
            )

            # Move to cpu for comparison with prior.
            return log_likelihood_trial_sum.cpu() + self._prior.log_prob(theta)
        elif self.sample_with == "vi":
            with torch.set_grad_enabled(track_gradients):
                return self._q.log_prob(theta.to(self._device))
        else:
            raise NotImplementedError(
                "We only implement methods mcmc, rejection and vi"
            )

    def sample(
        self,
        sample_shape: Shape = torch.Size(),
        x: Optional[Tensor] = None,
        show_progress_bars: bool = True,
        sample_with: Optional[str] = None,
        mcmc_method: Optional[str] = None,
        mcmc_parameters: Optional[Dict[str, Any]] = None,
        rejection_sampling_parameters: Optional[Dict[str, Any]] = None,
        vi_parameters: Optional[Dict[str, Any]] = None,
    ) -> Tensor:
        r"""
        Return samples from posterior distribution $p(\theta|x)$ with MCMC.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior. If
                sample_shape is multidimensional we simply draw `sample_shape.numel()`
                samples and then reshape into the desired shape.
            x: Conditioning context for posterior $p(\theta|x)$. If not provided,
                fall back onto `x` passed to `set_default_x()`.
            show_progress_bars: Whether to show sampling progress monitor.
            sample_with: Method to use for sampling from the posterior. Must be one of
                [`mcmc` | `rejection`].
            mcmc_method: Optional parameter to override `self.mcmc_method`.
            mcmc_parameters: Dictionary overriding the default parameters for MCMC.
                The following parameters are supported: `thin` to set the thinning
                factor for the chain, `warmup_steps` to set the initial number of
                samples to discard, `num_chains` for the number of chains,
                `init_strategy` for the initialisation strategy for chains; `prior`
                will draw init locations from prior, whereas `sir` will use Sequential-
                Importance-Resampling using `init_strategy_num_candidates` to find init
                locations.
            rejection_sampling_parameters: Dictionary overriding the default parameters
                for rejection sampling. The following parameters are supported:
                `proposal` as the proposal distribtution (default is the prior).
                `max_sampling_batch_size` as the batchsize of samples being drawn from
                the proposal at every iteration. `num_samples_to_find_max` as the
                number of samples that are used to find the maximum of the
                `potential_fn / proposal` ratio. `num_iter_to_find_max` as the number
                of gradient ascent iterations to find the maximum of that ratio. `m` as
                multiplier to that ratio.
            vi_sampling_parameters: Dict for sampling parameters in vi e.g. one may use
            a sample correction.

        Returns:
            Samples from posterior.
        """

        self.net.eval()

        sample_with = sample_with if sample_with is not None else self._sample_with
        x, num_samples = self._prepare_for_sample(x, sample_shape)

        potential_fn_provider = PotentialFunctionProvider()
        if sample_with == "mcmc":

            mcmc_method, mcmc_parameters = self._potentially_replace_mcmc_parameters(
                mcmc_method, mcmc_parameters
            )

            samples = self._sample_posterior_mcmc(
                num_samples=num_samples,
                potential_fn=potential_fn_provider(
                    self._prior, self.net, x, mcmc_method
                ),
                init_fn=self._build_mcmc_init_fn(
                    self._prior,
                    potential_fn_provider(self._prior, self.net, x, "slice_np"),
                    **mcmc_parameters,
                ),
                mcmc_method=mcmc_method,
                show_progress_bars=show_progress_bars,
                **mcmc_parameters,
            )
        elif sample_with == "rejection":
            rejection_sampling_parameters = self._potentially_replace_rejection_parameters(
                rejection_sampling_parameters
            )
            if "proposal" not in rejection_sampling_parameters:
                rejection_sampling_parameters["proposal"] = self._prior

            samples, _ = rejection_sample(
                potential_fn=potential_fn_provider(
                    self._prior, self.net, x, "rejection"
                ),
                num_samples=num_samples,
                **rejection_sampling_parameters,
            )
        elif sample_with == "vi":
            # TODO Check if train was called. And warn if not
            vi_parameters = self._potentially_replace_vi_parameters(vi_parameters)
            method = vi_parameters.get("sampling_method", "naive")
            method_params = vi_parameters.get("sampling_method_params", {})
            track_gradients = vi_parameters.get("track_gradients", False)
            sample_shape = torch.Size(sample_shape)
            if method.lower() == "naive":
                if track_gradients and self._q.has_rsample:
                    samples = self._q.rsample(sample_shape)
                else:
                    samples = self._q.sample(sample_shape)
            elif method.lower() == "ir":
                potential_fn = potential_fn_provider(self._prior, self.net, x, "vi")
                samples = importance_resampling(
                    sample_shape.numel(), potential_fn=potential_fn, proposal=self._q, **method_params
                )
            elif method.lower() == "imh":
                 potential_fn = potential_fn_provider(self._prior, self.net, x, "vi")
                 samples = independent_mh(sample_shape.numel(), potential_fn,self._q, **method_params)
            elif method.lower() == "rejection":
                rejection_sampling_parameters = self._potentially_replace_rejection_parameters(
                    rejection_sampling_parameters
                )
                rejection_sampling_parameters["proposal"] = self._q
                samples, _ = rejection_sample(
                    potential_fn=potential_fn_provider(
                        self._prior, self.net, x, "rejection"
                    ),
                    num_samples=num_samples,
                    **rejection_sampling_parameters,
                )
            elif method.lower() == "slice":
                potential_fn = potential_fn_provider(self._prior, self.net, x, "vi")
                samples = random_direction_slice_sampler(
                    sample_shape.numel(), potential_fn, self._q, **method_params
                )
            else:
                raise NotImplementedError("The sampling methods from the vi posterior are currently restricted to naive, ir, imh, rejection and slice")
        else:
            raise NameError(
                "The only implemented sampling methods are `mcmc`, `rejection` and `vi`."
            )

        self.net.train(True)

        return samples.reshape((*sample_shape, -1))

    def sample_conditional(
        self,
        sample_shape: Shape,
        condition: Tensor,
        dims_to_sample: List[int],
        x: Optional[Tensor] = None,
        sample_with: str = "mcmc",
        show_progress_bars: bool = True,
        mcmc_method: Optional[str] = None,
        mcmc_parameters: Optional[Dict[str, Any]] = None,
        rejection_sampling_parameters: Optional[Dict[str, Any]] = None,
    ) -> Tensor:
        r"""
        Return samples from conditional posterior $p(\theta_i|\theta_j, x)$.

        In this function, we do not sample from the full posterior, but instead only
        from a few parameter dimensions while the other parameter dimensions are kept
        fixed at values specified in `condition`.

        Samples are obtained with MCMC.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior. If
                sample_shape is multidimensional we simply draw `sample_shape.numel()`
                samples and then reshape into the desired shape.
            condition: Parameter set that all dimensions not specified in
                `dims_to_sample` will be fixed to. Should contain dim_theta elements,
                i.e. it could e.g. be a sample from the posterior distribution.
                The entries at all `dims_to_sample` will be ignored.
            dims_to_sample: Which dimensions to sample from. The dimensions not
                specified in `dims_to_sample` will be fixed to values given in
                `condition`.
            x: Conditioning context for posterior $p(\theta|x)$. If not provided,
                fall back onto `x` passed to `set_default_x()`.
            sample_with: Method to use for sampling from the posterior. Must be one of
                [`mcmc` | `rejection`]. In this method, the value of
                `self._sample_with` will be ignored.
            show_progress_bars: Whether to show sampling progress monitor.
            mcmc_method: Optional parameter to override `self.mcmc_method`.
            mcmc_parameters: Dictionary overriding the default parameters for MCMC.
                The following parameters are supported: `thin` to set the thinning
                factor for the chain, `warmup_steps` to set the initial number of
                samples to discard, `num_chains` for the number of chains,
                `init_strategy` for the initialisation strategy for chains; `prior`
                will draw init locations from prior, whereas `sir` will use Sequential-
                Importance-Resampling using `init_strategy_num_candidates` to find init
                locations.
            rejection_sampling_parameters: Dictionary overriding the default parameters
                for rejection sampling. The following parameters are supported:
                `proposal` as the proposal distribtution (default is the prior).
                `max_sampling_batch_size` as the batchsize of samples being drawn from
                the proposal at every iteration. `num_samples_to_find_max` as the
                number of samples that are used to find the maximum of the
                `potential_fn / proposal` ratio. `num_iter_to_find_max` as the number
                of gradient ascent iterations to find the maximum of that ratio. `m` as
                multiplier to that ratio.

        Returns:
            Samples from conditional posterior.
        """

        return super().sample_conditional(
            PotentialFunctionProvider(),
            sample_shape,
            condition,
            dims_to_sample,
            x,
            sample_with,
            show_progress_bars,
            mcmc_method,
            mcmc_parameters,
            rejection_sampling_parameters,
        )

    def map(
        self,
        x: Optional[Tensor] = None,
        num_iter: int = 1000,
        learning_rate: float = 1e-2,
        init_method: Union[str, Tensor] = "posterior",
        num_init_samples: int = 500,
        num_to_optimize: int = 100,
        save_best_every: int = 10,
        show_progress_bars: bool = True,
    ) -> Tensor:
        """
        Returns the maximum-a-posteriori estimate (MAP).

        The method can be interrupted (Ctrl-C) when the user sees that the
        log-probability converges. The best estimate will be saved in `self.map_`.

        The MAP is obtained by running gradient ascent from a given number of starting
        positions (samples from the posterior with the highest log-probability). After
        the optimization is done, we select the parameter set that has the highest
        log-probability after the optimization.

        Warning: The default values used by this function are not well-tested. They
        might require hand-tuning for the problem at hand.

        Args:
            x: Conditioning context for posterior $p(\theta|x)$. If not provided,
                fall back onto `x` passed to `set_default_x()`.
            num_iter: Number of optimization steps that the algorithm takes
                to find the MAP.
            learning_rate: Learning rate of the optimizer.
            init_method: How to select the starting parameters for the optimization. If
                it is a string, it can be either [`posterior`, `prior`], which samples
                the respective distribution `num_init_samples` times. If it is a,
                the tensor will be used as init locations.
            num_init_samples: Draw this number of samples from the posterior and
                evaluate the log-probability of all of them.
            num_to_optimize: From the drawn `num_init_samples`, use the
                `num_to_optimize` with highest log-probability as the initial points
                for the optimization.
            save_best_every: The best log-probability is computed, saved in the
                `map`-attribute, and printed every `print_best_every`-th iteration.
                Computing the best log-probability creates a significant overhead
                (thus, the default is `10`.)
            show_progress_bars: Whether or not to show a progressbar for sampling from
                the posterior.

        Returns:
            The MAP estimate.
        """
        return super().map(
            x=x,
            num_iter=num_iter,
            learning_rate=learning_rate,
            init_method=init_method,
            num_init_samples=num_init_samples,
            num_to_optimize=num_to_optimize,
            save_best_every=save_best_every,
            show_progress_bars=show_progress_bars,
        )

    @staticmethod
    def _log_likelihoods_over_trials(
        x: Tensor, theta: Tensor, net: nn.Module, track_gradients: bool = False,
    ) -> Tensor:
        r"""Return log likelihoods summed over iid trials of `x`.

        Note: `x` can be a batch with batch size larger 1. Batches in `x` are assumed
        to be iid trials, i.e., data generated based on the same paramters /
        experimental conditions.

        Repeats `x` and $\theta$ to cover all their combinations of batch entries.

        Args:
            x: batch of iid data.
            theta: batch of parameters
            net: neural net with .log_prob()
            track_gradients: Whether to track gradients.

        Returns:
            log_likelihood_trial_sum: log likelihood for each parameter, summed over all
                batch entries (iid trials) in `x`.
        """

        # Repeat `x` in case of evaluation on multiple `theta`. This is needed below in
        # when calling nflows in order to have matching shapes of theta and context x
        # at neural network evaluation time.
        theta_repeated, x_repeated = NeuralPosterior._match_theta_and_x_batch_shapes(
            theta=theta, x=atleast_2d(x)
        )
        assert (
            x_repeated.shape[0] == theta_repeated.shape[0]
        ), "x and theta must match in batch shape."
        assert (
            next(net.parameters()).device == x.device and x.device == theta.device
        ), f"device mismatch: net, x, theta: {next(net.parameters()).device}, {x.decive}, {theta.device}."

        # Calculate likelihood in one batch.
        with torch.set_grad_enabled(track_gradients):
            log_likelihood_trial_batch = net.log_prob(x_repeated, theta_repeated)
            # Reshape to (x-trials x parameters), sum over trial-log likelihoods.
            log_likelihood_trial_sum = log_likelihood_trial_batch.reshape(
                x.shape[0], -1
            ).sum(0)

        return log_likelihood_trial_sum


class PotentialFunctionProvider:
    """
    This class is initialized without arguments during the initialization of the
     Posterior class. When called, it specializes to the potential function appropriate
     to the requested mcmc_method.

    NOTE: Why use a class?
    ----------------------
    During inference, we use deepcopy to save untrained posteriors in memory. deepcopy
    uses pickle which can't serialize nested functions
    (https://stackoverflow.com/a/12022055).

    It is important to NOT initialize attributes upon instantiation, because we need the
    most current trained posterior neural net.

    Returns:
        Potential function for use by either numpy or pyro sampler.
    """

    def __call__(
        self, prior, likelihood_nn: nn.Module, x: Tensor, method: str,
    ) -> Callable:
        r"""Return potential function for posterior $p(\theta|x)$.

        Switch on numpy or pyro potential function based on `method`.

        Args:
            prior: Prior distribution that can be evaluated.
            likelihood_nn: Neural likelihood estimator that can be evaluated.
            x: Conditioning variable for posterior $p(\theta|x)$. Can be a batch of iid
                x.
            method: One of `slice_np`, `slice`, `hmc` or `nuts`, `rejection`.

        Returns:
            Potential function for sampler.
        """
        self.likelihood_nn = likelihood_nn
        self.prior = prior
        self.device = next(likelihood_nn.parameters()).device
        self.x = atleast_2d(x).to(self.device)

        if method == "slice":
            return partial(self.pyro_potential, track_gradients=False)
        elif method in ("hmc", "nuts"):
            return partial(self.pyro_potential, track_gradients=True)
        elif "slice_np" in method:
            return partial(self.posterior_potential, track_gradients=False)
        elif method == "rejection":
            return partial(self.posterior_potential, track_gradients=True)
        elif method == "vi":
            return partial(self.posterior_potential, track_gradients=False)
        else:
            NotImplementedError

    def log_likelihood(self, theta: Tensor, track_gradients: bool = False) -> Tensor:
        """Return log likelihood of fixed data given a batch of parameters."""

        log_likelihoods = LikelihoodBasedPosterior._log_likelihoods_over_trials(
            x=self.x,
            theta=ensure_theta_batched(theta).to(self.device),
            net=self.likelihood_nn,
            track_gradients=track_gradients,
        )

        return log_likelihoods

    def posterior_potential(
        self, theta: np.array, track_gradients: bool = False
    ) -> ScalarFloat:
        r"""Return posterior log prob. of theta $p(\theta|x)$"

        Args:
            theta: Parameters $\theta$, batch dimension 1.

        Returns:
            Posterior log probability of the theta, $-\infty$ if impossible under prior.
        """
        theta = torch.as_tensor(theta, dtype=torch.float32)

        # Notice opposite sign to pyro potential.
        return self.log_likelihood(
            theta, track_gradients=track_gradients
        ).cpu() + self.prior.log_prob(theta)

    def pyro_potential(
        self, theta: Dict[str, Tensor], track_gradients: bool = False
    ) -> Tensor:
        r"""Return posterior log probability of parameters $p(\theta|x)$.

         Args:
            theta: Parameters $\theta$. The tensor's shape will be
                (1, shape_of_single_theta) if running a single chain or just
                (shape_of_single_theta) for multiple chains.

        Returns:
            The potential $-[\log r(x_o, \theta) + \log p(\theta)]$.
        """

        theta = next(iter(theta.values()))

        return -(
            self.log_likelihood(theta, track_gradients=track_gradients).cpu()
            + self.prior.log_prob(theta)
        )


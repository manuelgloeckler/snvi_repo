import logging
import math
from typing import Any, Dict, Optional, Tuple

import torch
from sbi import inference as inference
from sbi.utils.get_nn_models import likelihood_nn

from sbibm.algorithms.sbi.utils import (
    wrap_posterior,
    wrap_prior_dist,
    wrap_simulator_fn,
)


from sbivibm.utils import automatic_transform

import logging
import math
from typing import Any, Dict, Optional, Tuple
import numpy as np

from sbibm.tasks.task import Task


def run(
    task: Task,
    num_samples: int,
    num_simulations: int,
    num_observation: Optional[int] = None,
    observation: Optional[torch.Tensor] = None,
    num_rounds: int = 10,
    neural_net: str = "maf",
    hidden_features: int = 50,
    simulation_batch_size: int = 10000,
    training_batch_size: int = 1000,
    automatic_transforms_enabled: bool = True,
    mcmc_method: str = "slice_np_vectorized",
    mcmc_parameters: Dict[str, Any] = {
        "num_chains": 100,
        "thin": 10,
        "warmup_steps": 100,
        "init_strategy": "sir",
        "sir_batch_size": 1000,
        "sir_num_batches": 100,
    },
    show_progress_bars: bool = True,
    z_score_x: bool = True,
    z_score_theta: bool = True,
    **kwargs,
) -> Tuple[list,torch.Tensor, int]:
    """Runs (S)NLE from `sbi`
    Args:
        task: Task instance
        num_observation: Observation number to load, alternative to `observation`
        observation: Observation, alternative to `num_observation`
        num_samples: Number of samples to generate from posterior
        num_simulations: Simulation budget
        num_rounds: Number of rounds
        neural_net: Neural network to use, one of maf / mdn / made / nsf
        hidden_features: Number of hidden features in network
        simulation_batch_size: Batch size for simulator
        training_batch_size: Batch size for training network
        automatic_transforms_enabled: Whether to enable automatic transforms
        mcmc_method: MCMC method
        mcmc_parameters: MCMC parameters
        z_score_x: Whether to z-score x
        z_score_theta: Whether to z-score theta
    Returns:
        Samples from posterior, number of simulator calls, log probability of true params if computable
    """
    assert not (num_observation is None and observation is None)
    assert not (num_observation is not None and observation is not None)

    log = logging.getLogger(__name__)

    if num_rounds == 1:
        log.info(f"Running NLE")
        num_simulations_per_round = num_simulations
    else:
        log.info(f"Running SNLE")
        num_simulations_per_round = math.floor(num_simulations / num_rounds)

    if simulation_batch_size > num_simulations_per_round:
        simulation_batch_size = num_simulations_per_round
        log.warn("Reduced simulation_batch_size to num_simulation_per_round")

    if training_batch_size > num_simulations_per_round:
        training_batch_size = num_simulations_per_round
        log.warn("Reduced training_batch_size to num_simulation_per_round")

    prior = task.get_prior_dist()
    if observation is None:
        observation = task.get_observation(num_observation)

    simulator = task.get_simulator(max_calls=num_simulations)

    
    # PyTorch 1.8/1.9 compatibility
    prior.set_default_validate_args(False)

    #transforms = task._get_transforms(automatic_transforms_enabled)["parameters"] Incompatible with Torch 1.8/9 ??? -> Return alsways identity...
    transforms = automatic_transform(task)
    if automatic_transforms_enabled:
        prior = wrap_prior_dist(prior, transforms)
        simulator = wrap_simulator_fn(simulator, transforms)

    density_estimator_fun = likelihood_nn(
        model=neural_net.lower(),
        hidden_features=hidden_features,
        z_score_x=z_score_x,
        z_score_theta=z_score_theta,
    )
    inference_method = inference.SNLE_A(
        density_estimator=density_estimator_fun,
        prior=prior,
    )

    posteriors = []
    proposal = prior
    mcmc_parameters["warmup_steps"] = 25

    for r in range(num_rounds):
        theta, x = inference.simulate_for_sbi(
            simulator,
            proposal,
            num_simulations=num_simulations_per_round,
            simulation_batch_size=simulation_batch_size,
        )

        density_estimator = inference_method.append_simulations(
            theta, x, from_round=r
        ).train(
            training_batch_size=training_batch_size,
            retrain_from_scratch_each_round=False,
            discard_prior_samples=False,
            show_train_summary=show_progress_bars,
        )
        if r > 1:
            mcmc_parameters["init_strategy"] = "latest_sample"
        posterior = inference_method.build_posterior(
            mcmc_method=mcmc_method, mcmc_parameters=mcmc_parameters
        )
        # Copy hyperparameters, e.g., mcmc_init_samples for "latest_sample" strategy.
        if r > 0:
            posterior.copy_hyperparameters_from(posteriors[-1])
        proposal = posterior.set_default_x(observation)
        posteriors.append(posterior)

    if automatic_transforms_enabled:
        for i in range(len(posteriors)):
            posteriors[i] = wrap_posterior(posteriors[i], transforms)


    assert simulator.num_simulations == num_simulations

    samples = posteriors[-1].sample((num_samples,)).detach()

    return posteriors, samples, simulator.num_simulations








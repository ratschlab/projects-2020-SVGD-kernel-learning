import jax.numpy as np
from jax import jit, vmap, random, value_and_grad
from jax.experimental import optimizers
from jax.ops import index_update, index
import haiku as hk
import jax
import numpy as onp

import traceback
import time
from tqdm import tqdm
from functools import partial
import json_tricks as json

import utils
import metrics
import stein
import kernels
import nets
import distributions

from models import Optimizer, KernelLearner

import os
on_cluster = not os.getenv("HOME") == "/home/lauro"
disable_tqdm = on_cluster

#class Logger():
#    """
#    Utility to track and store info logged during run (loss, KSD, parameters, etc).
#    """
#    def __init__(self):
#        pass


class SVGD():
    def __init__(self,
                 key,
                 target,
                 proposal,
                 n_particles: int = 400,
                 get_kernel=None,
                 learning_rate=0.1,
                 u_est_update=False,
                 debugging_config=None):
        """
        Arguments
        ----------
        target, proposal: instances of class distributions.Distribution
        kernel: callable
        optimizer_svgd needs to have pure methods
           optimizer_svgd.init(params) -> state
           optimizer_svgd.update(key, gradient, state) -> state
           optimizer_svgd.get_params(state) -> params
        """
        self.target = target
        self.proposal = proposal
        self.n_particles = n_particles
        self.u_est_update=u_est_update
        self.debugging_config = debugging_config

        # optimizer for particle updates
        self.opt = Optimizer(*optimizers.sgd(learning_rate))
        self.threadkey, subkey = random.split(key)
        self.initialize_optimizer(subkey)
        self.step_counter = 0
        self.rundata = {}
        self.initialize_groups()
        self.loglikelihood = vmap(proposal.logpdf)(self.get_params())

        if get_kernel is None:
            self.get_kernel = lambda kernel_params: kernels.get_rbf_kernel(1)
        else:
            self.get_kernel = get_kernel

    def initialize_optimizer(self, key):
        particles = self.init_particles(key)
        self.optimizer_state = self.opt.init(particles)
        return None

    def init_particles(self, key):
        particle_shape = (self.n_particles, self.target.d)
        particles = self.proposal.sample(self.n_particles, key=key)
        assert particles.shape == particle_shape
        return particles

    def initialize_groups(self, key=None):
        if key is None:
            self.threadkey, key = random.split(self.threadkey)
        key, subkey = random.split(key)
        self.group_names = ("leader", "follower") # TODO make this a dict or namedtuple
        idx = random.permutation(subkey, np.arange(self.n_particles))
        #self.group_idx = idx[:-1], idx[-1:]
        self.group_idx = idx.split(2)
        return None

    @partial(jit, static_argnums=0)
    def _step(self, optimizer_state, kernel_params, group_idx, step_counter):
        """
        Updates particles in direction of the SVGD gradient.

        Returns
        * updated optimizer_state
        * dKL: np array of same shape as followers (n, d)
        * auxdata consisting of [mean_drift, mean_repulsion] of shape (n, 2, d)
        """
        leader_idx, follower_idx = group_idx
        particles = self.opt.get_params(optimizer_state)
        leaders, followers = particles[leader_idx], particles[follower_idx]
        kernel = self.get_kernel(kernel_params)
        if self.u_est_update:
            negdKL, auxdata = stein.phistar_u( # negdKL = [leader_dKL, follower_dKL]
                followers, leaders, self.target.logpdf, kernel)
            dKL = -negdKL # Stein gradient
            dKL = index_update(dKL, np.concatenate(group_idx), dKL) # reshuffle idx
        else:
            negdKL, auxdata = stein.phistar(
                particles, leaders, self.target.logpdf, kernel)
            dKL = -negdKL
        optimizer_state = self.opt.update(step_counter, dKL, optimizer_state)
        return optimizer_state, dKL, auxdata

    def step(self, kernel_params):
        """Log rundata, take step, update loglikelihood. Mutates state"""
        updated_optimizer_state, dKL, auxdata = self._step(
            self.optimizer_state, kernel_params, self.group_idx, self.step_counter)
        self.log(dKL, auxdata, kernel_params, temp=None)
        self.update_logq(kernel_params)
        self.optimizer_state = updated_optimizer_state # take step
        self.step_counter += 1
        return None

    def get_params(self):
        return self.opt.get_params(self.optimizer_state)

    def log(self, dKL, auxdata, kernel_params, temp=None):
        particles = self.opt.get_params(self.optimizer_state)
        mean_auxdata = np.mean(auxdata, axis=0)
        metrics.append_to_log(self.rundata, {
            "mean_drift": mean_auxdata[0],
            "mean_repulsion": mean_auxdata[1],
            "step": self.step_counter,
            "stein_gradient_norm": np.linalg.norm(dKL),
            "loglikelihood": np.sum(self.loglikelihood),
        })
        # mean and var of groups
        for k, idx in zip(self.group_names, self.group_idx): # TODO: iterate thru particle groups directly instead
            metrics.append_to_log(self.rundata, {
                f"{k}_mean": np.mean(particles[idx], axis=0),
                f"{k}_std":  np.std(particles[idx], axis=0),
                f"{k}_kl":   np.mean(self.loglikelihood[idx]) \
                           - np.mean(vmap(self.target.logpdf)(particles[idx])),
                f"{k}_ksd": self.ksd_squared(particles[idx], kernel_params),
            })

    @partial(jit, static_argnums=0)
    def ksd_squared(self, particles, kernel_params):
        return stein.ksd_squared_u(
            particles, self.target.logpdf, self.get_kernel(kernel_params))

    @partial(jit, static_argnums=0)
    def _update_logq(self, loglikelihood, optimizer_state, kernel_params, group_idx, step_counter):
        """
        Update log pdf of current particles

        Arguments:
            loglikelihood : np.array of shape (n, d)
        """
        leader_idx, follower_idx = group_idx
        particles = self.opt.get_params(optimizer_state)
        def transformation(x):
            # inject x
            particles_with_inject = index_update(particles, follower_idx[0], x)
            optimizer_state_with_inject = self.opt.init(particles_with_inject)

            # step
            updated_optimizer_state, *_ = self._step(
                optimizer_state_with_inject, kernel_params, group_idx, step_counter)

            # extract z = T(x)
            updated_particles = self.opt.get_params(updated_optimizer_state)
            z = updated_particles[follower_idx[0]]
            return z

        jacdets = metrics.compute_jacdet(transformation, particles)
        return loglikelihood - np.log(jacdets), jacdets

    def update_logq(self, kernel_params):
        self.loglikelihood, jacdets = self._update_logq(
            self.loglikelihood, self.optimizer_state, kernel_params,
            self.group_idx, self.step_counter)

    def flow(self, key=None, n_iter=400, kernel_params=None):
        """
        Makes n_iter number of SVGD steps. Fixed kernel.
        """
        if key is None:
            self.threadkey, key = random.split(self.threadkey)
        try:
            for i in tqdm(range(n_iter), disable=disable_tqdm):
                self.step(kernel_params)
            key, subkey = random.split(key)
            self.rundata["Interrupted because of NaN"] = False
            self.rundata["particles"] = self.opt.get_params(self.optimizer_state)
        except FloatingPointError as e:
            print("printing traceback:")
            traceback.print_exc()
            self.rundata["Interrupted because of NaN"] = True
            self.rundata = utils.dict_dejaxify(self.rundata, target="numpy")
        return None


class AdversarialSVGD():
    """Jointly optimize particles and kernel parameters."""
    def __init__(self,
                 key,
                 target,
                 proposal,
                 sizes,
                 n_particles=400,
                 svgd_lr=0.1,
                 kernel_lr=0.1,
                 lambda_reg=0,
                 svgd_key=None):
        """
        Initialize containers for kernel learning and particle updates.
        """
        self.threadkey, keya, keyb = random.split(key, 3)
        self.kernel = KernelLearner(keya,
                                    target,
                                    sizes,
                                    kernels.get_rbf_kernel(1),
                                    kernel_lr,
                                    lambda_reg=lambda_reg,
                                    scaling_parameter=bool(lambda_reg),
                                    std_normalize=False)
        if svgd_key is not None:
            keyb = svgd_key
        self.svgd = SVGD(key=keyb,
                         target=target,
                         proposal=proposal,
                         n_particles=n_particles,
                         get_kernel=self.kernel.get_kernel,
                         learning_rate=svgd_lr)

    def step(self, n_iter_kernel=50):
        """
        1) Get particles
        2) Optimize kernel for KSD
        3) Move particles along SVGD gradient
        """
        leader_idx = self.svgd.group_idx[0]
        particles    = self.svgd.get_params()[leader_idx]
        for _ in range(n_iter_kernel):
            self.kernel.step(particles)
        kernel_params = self.kernel.get_params()
        self.svgd.step(kernel_params)
        return None

    def flow_and_train(self, n_iter=100, n_iter_kernel=50):
        for i in tqdm(range(n_iter), disable=disable_tqdm):
            self.step(n_iter_kernel)
            #self.kernel.initialize_optimizer(keep_params=True) # reinitialize optimizer
        return None

    def log(self):
        pass


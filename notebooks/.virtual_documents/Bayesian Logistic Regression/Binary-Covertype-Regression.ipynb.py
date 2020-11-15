get_ipython().run_line_magic("load_ext", " autoreload")
import sys
import os
sys.path.append("/home/lauro/code/msc-thesis/svgd/kernel_learning/")
from jax import config
config.update("jax_debug_nans", False)
from tqdm import tqdm
from jax import config


import jax.numpy as jnp
import jax.numpy as np
from jax import grad, jit, vmap, random, lax, jacfwd, value_and_grad
from jax.ops import index_update, index
import matplotlib.pyplot as plt
import matplotlib
import numpy as onp
import jax
import pandas as pd
import scipy
import haiku as hk
    
import utils
import plot
import distributions
import stein
import models
import flows
from itertools import cycle, islice

key = random.PRNGKey(0)

from sklearn.model_selection import train_test_split
from sklearn.calibration import calibration_curve

from functools import partial
import kernels
import metrics

import matplotlib.pyplot as plt
import seaborn as sns
from sklearn import datasets
sns.set(style='white')

from tensorflow_probability.substrates import jax as tfp
tfd = tfp.distributions
tfb = tfp.bijectors
tfpk = tfp.math.psd_kernels

import optax


# set up exporting
import matplotlib
matplotlib.use("pgf")
matplotlib.rcParams.update({
    "pgf.texsystem": "pdflatex",
    'font.family': 'serif',
    'text.usetex': True,
    'pgf.rcfonts': False,
})

# save figures by using plt.savefig('title of figure')


get_ipython().run_line_magic("matplotlib", " inline")


data = scipy.io.loadmat('/home/lauro/code/msc-thesis/wang_svgd/data/covertype.mat')
features = data['covtype'][:, 1:]
features = onp.hstack([features, onp.ones([features.shape[0], 1])]) # add intercept term

labels = data['covtype'][:, 0]
labels[labels == 2] = 0

x_train, x_test, y_train, y_test = train_test_split(features, labels, test_size=0.2, random_state=42)

num_features = features.shape[-1]


batch_size = 128
num_datapoints = len(x_train)
num_batches = num_datapoints // batch_size


def get_batches(x, y, n_steps=num_batches*2, batch_size=batch_size):
    """Split x and y into batches"""
    assert len(x) == len(y)
    assert x.ndim > y.ndim
    n = len(x)
    idxs = onp.random.choice(n, size=(n_steps, batch_size))
    for idx in idxs:
        yield x[idx], y[idx]
#     batch_cycle = cycle(zip(*[onp.array_split(data, len(data)//batch_size) for data in (x, y)]))
#     return islice(batch_cycle, n_steps)


a0, b0 = 1, 0.01 # hyper-parameters


from jax.scipy import stats, special


# alternative model
def sample_from_prior(key, num=100):
    keya, keyb = random.split(key)
    alpha = random.gamma(keya, a0, shape=(num,)) / b0
    w = random.normal(keyb, shape=(num, num_features))
    return w, np.log(alpha)


def prior_logp(w, log_alpha):
    """
    Returns logp(w, log_alpha) = sum_i(logp(wi, alphai))

    w has shape (num_features,), or (n, num_features)
    similarly, log_alpha may have shape () or (n,)"""
    if log_alpha.ndim == 0:
        assert w.ndim == 1
    elif log_alpha.ndim == 1:
        assert log_alpha.shape[0] == w.shape[0]

    alpha = np.exp(log_alpha)
    logp_alpha = np.sum(stats.gamma.logpdf(alpha, a0, scale=1/b0))
    if w.ndim == 2:
        logp_w = np.sum(vmap(lambda wi, alphai: stats.norm.logpdf(wi, scale=1/np.sqrt(alphai)))(w, alpha))
    elif w.ndim == 1:
        logp_w = np.sum(stats.norm.logpdf(w, scale=1/np.sqrt(alpha)))
    else:
        raise
    return logp_alpha + logp_w


def loglikelihood(y, x, w):
    """
    compute log p(y | x, w) for a single parameter w of
    shape (num_features,) and a batch of data (y, x) of
    shape (m,) and (m, num_features)

    log p(y | x, w) = sum_i(logp(yi| xi, w))
    """
    y = ((y - 1/2)*2).astype(np.int32)
    logits = x @ w
    prob_y = special.expit(logits*y)
    return np.sum(np.log(prob_y))


def log_posterior_unnormalized(y, x, w, log_alpha):
    """All is batched"""
    log_prior = prior_logp(w, log_alpha)
    log_likelihood = np.sum(vmap(lambda wi: loglikelihood(y, x, wi))(w))
    return log_prior + log_likelihood


def log_posterior_unnormalized_single_param(y, x, w, log_alpha):
    """y, x are batched, w, log_alpha not. In case I need
    an unbatched eval of the target logp."""
    log_prior = prior_logp(w, log_alpha)
    log_likelihood = loglikelihood(y, x, w)
    return log_prior + log_likelihood


def compute_probs(y, x, w):
    """y and x are data batches. w is a single parameter
    array of shape (num_features,)"""
    y = ((y - 1/2)*2).astype(np.int32)
    logits = x @ w
    prob_y = special.expit(logits*y)
    return prob_y


@jit
def compute_test_accuracy(w):
    probs = vmap(lambda wi: compute_probs(y_test, x_test, wi))(w)
    probs_y = np.mean(probs, axis=0)
    return np.mean(probs_y > 0.5)


@jit
def compute_train_accuracy(w):
    probs = vmap(lambda wi: compute_probs(y_train, x_train, wi))(w)
    probs_y = np.mean(probs, axis=0)
    return np.mean(probs_y > 0.5)


def ravel(w, log_alpha):
    return np.hstack([w, np.expand_dims(log_alpha, -1)])


def unravel(params):
    if params.ndim == 1:
        return params[:-1], params[-1]
    elif params.ndim == 2:
        return params[:, :-1], np.squeeze(params[:, -1])


def get_minibatch_logp(x, y):
    """
    Returns callable logp that computes the unnormalized target
    log pdf of raveled (flat) params with shape (num_features+1,)
    or shape (n, num_features+1).

    y, x are minibatches of data."""
    assert len(x) == len(y)
    assert x.ndim > y.ndim

    def logp(params): # TODO: if this doesn't work, then modify to just take a single param vector
        """params = ravel(w, log_alpha)"""
        w, log_alpha = unravel(params)
        log_prior = prior_logp(w, log_alpha)
        if w.ndim == 1:
            mean_loglikelihood = loglikelihood(y, x, w)
        elif w.ndim == 2:
            mean_loglikelihood = np.mean(vmap(lambda wi: loglikelihood(y, x, wi))(w))
        else:
            raise
        return log_prior + num_datapoints * mean_loglikelihood # = grad(log p)(theta) + N/n sum_i grad(log p)(theta | x)
    return logp


key, subkey = random.split(key)
w, log_alpha = sample_from_prior(subkey, 100)


xs = x_train[:100]
ys = y_train[:100]


log_posterior_unnormalized(ys, xs, w, log_alpha)


compute_test_accuracy(w)


lp = get_minibatch_logp(xs, ys)
params = ravel(w, log_alpha)
lp(params)


NUM_EPOCHS = 3
NUM_VALS = 5*NUM_EPOCHS # number of test accuracy evaluations per run
NUM_STEPS = num_batches*NUM_EPOCHS


NUM_STEPS


def sample_tv(key):
    return ravel(*sample_from_prior(key, num=batch_size)).split(2)


def run_svgd(key, lr):
    key, subkey = random.split(key)
    init_particles = ravel(*sample_from_prior(subkey, 100))
#     svgd_opt = optax.chain(optax.scale_by_schedule(utils.polynomial_schedule),
#                            optax.scale_by_rms(),
#                            optax.scale(-lr))
    svgd_opt = optax.sgd(lr)

    svgd_grad = models.KernelGradient(get_target_logp=lambda batch: get_minibatch_logp(*batch), scaled=False)
    particles = models.Particles(key, svgd_grad.gradient, init_particles, custom_optimizer=svgd_opt)

    test_batches = get_batches(x_test, y_test, 2*NUM_VALS, batch_size=batch_size)
    train_batches = get_batches(x_train, y_train, NUM_STEPS+1)
    for i, batch in tqdm(enumerate(train_batches), total=NUM_STEPS):
        particles.step(batch)
        if i % (NUM_STEPS//NUM_VALS) == 0:
            test_logp = get_minibatch_logp(*next(test_batches))
            stepdata = {
                "accuracy": compute_test_accuracy(unravel(particles.particles.training)[0]),
                "test_logp": test_logp(particles.particles.training),
            }
            metrics.append_to_log(particles.rundata, stepdata)

    particles.done()
    return particles


def run_neural_svgd(key, lr):
    """init_batch is a batch of initial samples / particles.
    Note: there's two types of things I call 'batch': a batch from the dataset
    and a batch of particles. don't confuse them"""
    key, subkey = random.split(key)
    init_particles = ravel(*sample_from_prior(subkey, batch_size))
    nsvgd_opt = optax.sgd(lr)

    key1, key2 = random.split(key)
    neural_grad = models.SDLearner(target_dim=init_particles.shape[1],
                                   get_target_logp=lambda batch: get_minibatch_logp(*batch),
                                   learning_rate=5e-3,
                                   key=key1,
                                   aux=False)
    particles = models.Particles(key2, neural_grad.gradient, init_particles, custom_optimizer=nsvgd_opt)

    # Warmup on first batch
    neural_grad.train(next_batch=sample_tv,
                      n_steps=100,
                      early_stopping=False,
                      data=next(get_batches(x_train, y_train, 2)))

    next_particles = partial(particles.next_batch)
    test_batches = get_batches(x_test, y_test, 2*NUM_VALS, batch_size=batch_size)
    train_batches = get_batches(x_train, y_train, NUM_STEPS+1)
    for i, data_batch in tqdm(enumerate(train_batches), total=NUM_STEPS):
        neural_grad.train(next_batch=next_particles, n_steps=10, data=data_batch)
        particles.step(neural_grad.get_params())
        if i % (NUM_STEPS//NUM_VALS)==0:
            test_logp = get_minibatch_logp(*next(test_batches))
            train_logp = get_minibatch_logp(*data_batch)
            stepdata = {
                "accuracy": compute_test_accuracy(unravel(particles.particles.training)[0]),
                "test_logp": test_logp(particles.particles.training),
                "training_logp": train_logp(particles.particles.training),
            }
            metrics.append_to_log(particles.rundata, stepdata)
    neural_grad.done()
    particles.done()
    return particles, neural_grad


schedule = utils.polynomial_schedule

def run_sgld(key, lr):
    key, subkey = random.split(key)
    init_particles = ravel(*sample_from_prior(subkey, 100))
    """init_batch = (w, log_alpha) is a batch of initial samples / particles."""
    key, subkey = random.split(key)
#     sgld_opt = utils.scaled_sgld(subkey, lr, schedule)
    sgld_opt = utils.sgld(lr, 0)

    def energy_gradient(data, particles, aux=True):
        """data = [batch_x, batch_y]"""
        xx, yy = data
        logp = get_minibatch_logp(xx, yy)
        logprob, grads = value_and_grad(logp)(particles)
        if aux:
            return -grads, {"logp": logprob}
        else:
            return -grads

    particles = models.Particles(key, energy_gradient, init_particles, custom_optimizer=sgld_opt)
    test_batches = get_batches(x_test, y_test)
    train_batches = get_batches(x_train, y_train, NUM_STEPS+1)
    for i, batch_xy in tqdm(enumerate(train_batches), total=NUM_STEPS):
        particles.step(batch_xy)
        if i % (NUM_STEPS//NUM_VALS)==0:
            test_logp = get_minibatch_logp(*next(test_batches))
            stepdata = {
                "accuracy": compute_test_accuracy(unravel(particles.particles.training)[0]),
                "train_accuracy": compute_train_accuracy(unravel(particles.particles.training)[0]),
                "test_logp": np.mean(test_logp(particles.particles.training))
            }
            metrics.append_to_log(particles.rundata, stepdata)
    particles.done()
    return particles


# Run samplers
key, subkey = random.split(key)

sgld_p = run_sgld(subkey, 1e-6)
# svgd_p = run_svgd(subkey, 5e-2)
# neural_p, neural_grad = run_neural_svgd(subkey, 1e-6)


sgld_aux = sgld_p.rundata
svgd_aux = svgd_p.rundata
neural_aux = neural_p.rundata


sgld_accs, svgd_accs, neural_accs = [aux["accuracy"] for aux in (sgld_aux, svgd_aux, neural_aux)]


plt.subplots(figsize=[15, 8])
names = ["SGLD", "SVGD", "Neural"]
accs = [sgld_accs, svgd_accs, neural_accs]
for name, acc in zip(names, accs):
    plt.plot(acc, "--.", label=name)
plt.legend()


spaced_idx = np.arange(0, NUM_STEPS, NUM_STEPS // NUM_VALS)
plt.plot(sgld_aux["training_logp"])
plt.plot(spaced_idx, sgld_aux["test_logp"])


spaced_idx = np.arange(0, NUM_STEPS, NUM_STEPS // NUM_VALS)
plt.plot(neural_aux["training_logp"])
plt.plot(spaced_idx, neural_aux["test_logp"])


key, subkey = random.split(key)

key, subkey = random.split(key)
def sgld_acc(lr):
    particles = run_sgld(subkey, lr)
    acc = particles.rundata["accuracy"]
    return np.mean(np.array(acc[-10:]))

def svgd_acc(lr):
    particles = run_svgd(subkey, lr)
    acc = particles.rundata["accuracy"]
    return np.mean(np.array(acc[-10:]))

def nsvgd_acc(lr):
    particles, _ = run_neural_svgd(subkey, lr)
    acc = particles.rundata["accuracy"]
    return np.mean(np.array(acc[-10:]))

def print_accs(lrs, accs):
    accs = np.asarray(accs)
    print(accs)
    print(np.argmax(accs))
    plt.plot(lrs, accs, "--.")
    plt.xscale("log")


accs = []
lrs = np.logspace(-9, -4, 15)
for lr in lrs:
    accs.append(sgld_acc(lr))
accs = np.array(accs)
print_accs(lrs, accs)


accs = []
lrs = np.logspace(-5, -1, 15)
for lr in lrs:
    accs.append(svgd_acc(lr))
accs = np.array(accs)
print_accs(lrs, accs)


accs = []
lrs = np.logspace(-5, -1, 15)
for lr in lrs:
    accs.append(nsvgd_acc(lr))
    print(accs[-1])
accs = np.array(accs)
print_accs(lrs, accs)


plt.subplots(figsize=[15, 8])
plt.plot(sgld_aux["training_mean"], "--o");


plt.plot(neural_grad.rundata["train_steps"])


# get_ipython().run_line_magic("matplotlib", " widget")
get_ipython().run_line_magic("matplotlib", " inline")
plt.subplots(figsize=[15, 8])
plt.plot(neural_grad.rundata["training_loss"])
plt.plot(neural_grad.rundata["validation_loss"])


plt.subplots(figsize=[15, 8])
plt.plot(neural_aux["training_mean"]);


plt.subplots(figsize=[15, 8])
plt.plot(svgd_aux["training_mean"]);


@jit
def batch_probs(params):
    """Returns test probabilities P(y=1) for
    all y in the test set, for w a parameter array
    of shape (n, num_features)"""
    w, _ = unravel(params)
    probs = vmap(lambda wi: compute_probs(y_test, x_test, wi))(w)
    return np.mean(probs, axis=0)


probabilities = [batch_probs(p.particles.training) for p in (sgld_p, svgd_p, neural_p)]


fig, axs = plt.subplots(1, 3, figsize=[17, 5])

for ax, probs, name in zip(axs, probabilities, names):
    true_freqs, bins = calibration_curve(y_test, probs, n_bins=10)
    ax.plot(true_freqs, bins, "--o")
#     print(bins)
    ax.plot(bins, bins)
    ax.set_ylabel("True frequency")
    ax.set_xlabel("Predicted probability")
    ax.set_title(name)


sdlfk


certainty = np.max([1 - probs, probs])


import jax.numpy as np
from jax import vmap
import jax
import haiku as hk

import warnings
import utils

"""A collection of positive definite kernel functions written using Jax.

Kernels are implemented as Haiku modules.
Every kernel takes as input two jax scalars or arrays x, y of shape (d,), where d is the particle dimension,
and outputs a scalar.
"""

class ARD(hk.Module):
    def __init__(self, name=None):
        super(ARD, self).__init__(name=name)

    def __call__(self, x, y):
        logh_init = np.zeros
        logh = hk.get_parameter("logh", shape=x.shape, dtype=x.dtype, init=logh_init)
        return _ard(x, y, logh)

def vanilla_ard(x, y):
    ard = ARD()
    return(ard(x, y))

def build_mlp(sizes, name=None, skip_connection=False):
    """
    * sizes is a list of integers representing layer dimension

    Network uses He initalization; see https://github.com/deepmind/dm-haiku/issues/6
    and https://sonnet.readthedocs.io/en/latest/api.html#variancescaling.
    """
    def mlp(x):
        lin = hk.nets.MLP(output_sizes=sizes,
                          w_init=hk.initializers.VarianceScaling(scale=2.0),
                          activation=jax.nn.relu,
                          activate_final=False,
                          name=name)
        if skip_connection is False:
            return lin(x)
        else:
            return lin(x) + x # make sure sizes fit (ie sizes[-1] == input dimension)
    return hk.transform(mlp)


## utils

def median_heuristic(x):
    """
    Heuristic for choosing ARD bandwidth.

    IN: np array of shape (n,) or (n,d): set of particles
    OUT: scalar: bandwidth parameter for RBF kernel, based on the heuristic from the SVGD paper.
    Note: assumes k(x, y) = exp(- (x - y)^2 / h / 2)
    """
    if x.ndim == 2:
        return vmap(median_heuristic, 1)(x)
    elif x.ndim == 1:
        n = x.shape[0]
        medsq = np.median(utils.squared_distance_matrix(x))
        h = medsq / np.log(n) / 2
        return h
    else:
        raise ValueError("Shape of x has to be either (n,) or (n, d)")

def _ard(x, y, logh):
    """
    IN:
    * x, y: np arrays of shape (d,)
    * logh: np array of shape (d,), or scalar. represents log of bandwidth parameter (so can be negative or zero).

    OUT:
    Scalar value of the ARD kernel evaluated at (x, y, h).
    """
    x, y = np.array(x), np.array(y)
    if x.shape != y.shape:
        raise ValueError(f"Shapes of particles x and y need to match. Recieved shapes x: {x.shape}, y: {y.shape}")
    if x.ndim > 1:
        raise ValueError(f"Input particles x and y can't have more than one dimension. Instead they have rank {x.ndim}")

    logh = np.array(logh)
    logh = np.squeeze(logh)
    if logh.ndim > 1:
        raise ValueError(f"Bandwidth needs to be a scalar or a d-dim vector. Instead it has shape {logh.shape}")
    elif logh.ndim == 1:
        assert x.shape == logh.shape

    h = np.exp(logh)
    if h.ndim == 0:
        return np.exp(- np.sum((x - y)**2 / h) / 2)
    else:
        return np.exp(- np.sum((x - y)**2 / h) / 2)

def get_ard_fn(logh):
    return lambda x, y: _ard(x, y, logh)

def _ard_m(x, y, sigma):
    """
    Arguments:
    * x, y : array-like. Shape (d,)
    * sigma: array-like. Shape (d, d). Must be positive definite.

    Returns:
    Scalar given by
    \[ e^{- 1/2 (x - y)^T \Sigma^{-1} (x - y)} \]
    """
    x, y = np.asarray(x), np.asarray(y)
    if x.shape != y.shape:
        raise ValueError(f"Shapes of particles x and y need to match. Recieved shapes x: {x.shape}, y: {y.shape}")
    elif x.ndim > 1 or x.ndim == 0:
        raise ValueError(f"Input particles x and y need to have shape (d,). Instead received shape {x.shape}")
    sigma = np.asarray(sigma)
    d = x.shape[0]
    if sigma.ndim != 2 and d != 1:
        raise ValueError(f"Sigma needs to be a square matrix. Instead, received shape {sigma.shape}.")

    inv = np.linalg.inv(sigma)
    return np.exp(- np.matmul(np.matmul(x - y, inv), x - y) / 2) # TODO add normalization factor

def ard_m(sigma):
    return lambda x, y: _ard_m(x, y, sigma)

def enc_funnel(z):
    """encode single sample z, shaped (d,)"""
    *x, y = z
    x, y = np.asarray(x), np.asarray(y)
    x_enc = x * np.exp(-y/2)
    return np.append(x_enc, y)

def dec_funnel(z):
    """decode single sample z, shaped (d,)"""
    *x, y = z
    x, y = np.asarray(x), np.asarray(y)
    x_dec = x * np.exp(y/2)
    return np.append(x_dec, y)

def funnel_optimal_kernel(x, y):
    x, y = np.asarray(x), np.asarray(y)
    if x.shape != y.shape:
        raise ValueError(f"Shapes of particles x and y need to match. Recieved shapes x: {x.shape}, y: {y.shape}")
    elif x.ndim > 1 or x.ndim == 0:
        raise ValueError(f"Input particles x and y need to have shape (d,). Instead received shape {x.shape}")
    return get_kernel_fn(logh=0)(enc_funnel(x), enc_funnel(y))


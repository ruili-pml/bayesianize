import operator
from functools import reduce
import math
from typing import Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as dist


from .base import VariationalMixin


EPS = 1e-6


__all__ = ["FFGMixin"]


def _prod(iterable):
    return reduce(operator.mul, iterable, 1)


def _normal_sample(mean, sd):
    return mean + torch.randn_like(mean) * sd


class FFGMixin(VariationalMixin):
    """Variational module that places a fully factorized Gaussian over .weight and .bias attributes.
    In the forward pass it marginalizes over the weights and directly samples the outputs from a Gaussian
    when in training mode, in testing mode it simply samples the weights and performs the forward pass
    as usual (in either case the outputs come from the same distribution, but the gradients have lower
    variance for the training mode; see https://arxiv.org/abs/1506.02557). This Mixin class can be combined
    with linear and convolutions layers, probably also with deconvolutional ones (need to check the math first).
    """

    def __init__(self, *args, prior_mean: float = 0., prior_weight_sd: Union[float, str] = 1.,
                 prior_bias_sd: float = 1., init_sd: float = 1e-4, max_sd: Optional[float] = None,
                 local_reparameterization: bool = True, nonlinearity_scale: float = 1., 
                 sqrt_width_scaling: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_bias = self.bias is not None
        self.local_reparameterization = local_reparameterization
        self.max_sd = max_sd

        # I use a softplus to ensure that the sd is positive, so need to map it through
        # the inverse for initialization of the parameter
        _init_sd = math.log(math.expm1(init_sd))
        self.weight_mean = nn.Parameter(self.weight.data.detach().clone())
        self._weight_sd = nn.Parameter(torch.full_like(self.weight.data, _init_sd))
        if self.has_bias:
            self.bias_mean = nn.Parameter(self.bias.data.detach().clone())
            self._bias_sd = nn.Parameter(torch.full_like(self.bias.data, _init_sd))
        else:
            self.register_parameter("bias_mean", None)
            self.register_parameter("_bias_sd", None)

        del self._parameters["weight"]
        if self.has_bias:
            del self._parameters["bias"]

        self.weight = self.weight_mean.data
        self.bias = self.bias_mean.data if self.has_bias else None

        if sqrt_width_scaling:
            input_dim = _prod(self.weight_mean.shape[1:]) + int(self.has_bias)
            prior_weight_sd /= input_dim ** 0.5
            prior_bias_sd /= input_dim ** 0.5

        prior_weight_sd *= nonlinearity_scale
        self.register_buffer("prior_weight_mean", torch.full_like(self.weight_mean, prior_mean))
        self.register_buffer("prior_weight_sd", torch.full_like(self.weight_sd, prior_weight_sd))

        prior_bias_mean = torch.full_like(self.bias_mean, prior_mean) if self.has_bias else None
        prior_bias_sd = torch.full_like(self.bias_sd, prior_bias_sd) if self.has_bias else None
        self.register_buffer("prior_bias_mean", prior_bias_mean)
        self.register_buffer("prior_bias_sd", prior_bias_sd)

    def extra_repr(self):
        s = super().extra_repr()
        m = self.prior_weight_mean.data.flatten()[0]
        if torch.allclose(m, self.prior_weight_mean) and (not self.has_bias or
                                                          torch.allclose(m, self.prior_bias_mean)):
            s += f", prior mean={m.item():.2f}"
        sd = self.prior_weight_sd.flatten()[0]
        if torch.allclose(sd, self.prior_weight_sd) and (not self.has_bias or torch.allclose(sd, self.prior_bias_sd)):
            s += f", prior sd={sd.item():.2f}"
        return s

    def init_from_deterministic_params(self, param_dict):
        weight = param_dict["weight"]
        bias = param_dict.get("bias")
        with torch.no_grad():
            self.weight_mean.data.copy_(weight.detach())
            if bias is not None:
                self.bias_mean.data.copy_(bias.detach())

    @property
    def weight_sd(self):
        weight_sd = F.softplus(self._weight_sd)
        return weight_sd.clamp(1e-5, self.max_sd) 

    @property
    def bias_sd(self):
        if self.has_bias:
            bias_sd = F.softplus(self._bias_sd)
            return bias_sd.clamp(1e-5, self.max_sd) 
        return None

    @property
    def weight_dist(self):
        return dist.Normal(self.weight_mean, self.weight_sd)

    @property
    def bias_dist(self):
        if self.has_bias:
            return dist.Normal(self.bias_mean, self.bias_sd)
        return None

    @property
    def prior_weight_dist(self):
        return dist.Normal(self.prior_weight_mean, self.prior_weight_sd)

    @property
    def prior_bias_dist(self):
        if self.has_bias:
            return dist.Normal(self.prior_bias_mean, self.prior_bias_sd)
        return None

    def kl_divergence(self):
        kl = dist.kl_divergence(self.weight_dist, self.prior_weight_dist).sum()
        if self.has_bias:
            kl += dist.kl_divergence(self.bias_dist, self.prior_bias_dist).sum()
        return kl

    def forward(self, x: torch.Tensor):
        if self.local_reparameterization:
            # use local reparameterization during training, i.e. sample the linear outputs
            # a ~ N(x^T \mu_w + \mu_b, (x^2)^T \sigma_W^2 + \sigma_b^2)
            self.weight = self.weight_mean.add(0)
            self.bias = self.bias_mean.add(0) if self.has_bias else None
            output_mean = super().forward(x)
            self.weight = self.weight_sd.pow(2)
            self.bias = self.bias_sd.pow(2) if self.has_bias else None
            output_var = super().forward(x.pow(2))
            output_var += output_var.abs().mul(output_var.lt(0.).float()).detach() + EPS
            return _normal_sample(output_mean, output_var.sqrt())
        else:
            # sample the weights during testing, i.e. W ~ N(\mu_W, \sigma_W^2), b ~ N(\mu_b, \sigma_b^2)
            # and calculate x^T W + b
            self.weight = _normal_sample(self.weight_mean, self.weight_sd)
            self.bias = _normal_sample(self.bias_mean, self.bias_sd) if self.has_bias else None
            return super().forward(x)

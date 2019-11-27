import tensorflow as tf
import numpy as np

from realsafe.attacks.base import BatchAttack
from realsafe.attacks.utils import get_xs_ph, get_ys_ph, maybe_to_array, get_unit


class MIM(BatchAttack):
    """
    Momentum Iterative Method (MIM)
    A white-box iterative constraint-based method. Require a differentiable loss function.

    Supported distance metric: `l_2`, `l_inf`
    Supported goal: `t`, `tm`, `ut`
    Supported config parameters:
    - `magnitude`: max distortion, could be either a float number or a numpy float number array with shape of
        (batch_size,).
    - `alpha`: step size for each iteration, could be either a float number or a numpy float number array with shape of
        (batch_size,).
    - `decay_factor`: an float number, the decay factor for momentum.
    - `iteration`: an integer, the iteration count.

    References:
    [1] https://arxiv.org/abs/1710.06081
    """

    def __init__(self, model, batch_size, loss, goal, distance_metric, session):
        self.model, self.batch_size, self._session = model, batch_size, session
        self.loss, self.goal, self.distance_metric = loss, goal, distance_metric
        # placeholder for batch_attack's input
        self.xs_ph = get_xs_ph(model, batch_size)
        self.ys_ph = get_ys_ph(model, batch_size)
        # flatten shape of xs_ph
        xs_flatten_shape = (batch_size, np.prod(self.model.x_shape))
        # store xs and ys in variables to reduce memory copy between tensorflow and python
        # variable for the original example with shape of (batch_size, D)
        self.xs_var = tf.Variable(tf.zeros(shape=xs_flatten_shape, dtype=self.model.x_dtype))
        # variable for labels
        self.ys_var = tf.Variable(tf.zeros(shape=(batch_size,), dtype=self.model.y_dtype))
        # variable for the (hopefully) adversarial example with shape of (batch_size, D)
        self.xs_adv_var = tf.Variable(tf.zeros(shape=xs_flatten_shape, dtype=self.model.x_dtype))
        # variable for the momentum term
        self.g_var = tf.Variable(tf.zeros(shape=xs_flatten_shape, dtype=self.model.x_dtype))
        # decay factor
        self.decay_factor_ph = tf.placeholder(self.model.x_dtype, ())
        self.decay_factor_var = tf.Variable(tf.zeros(shape=(), dtype=self.model.x_dtype))
        # magnitude
        self.eps_ph = tf.placeholder(self.model.x_dtype, (self.batch_size,))
        self.eps_var = tf.Variable(tf.zeros((self.batch_size,), dtype=self.model.x_dtype))
        # step size
        self.alpha_ph = tf.placeholder(self.model.x_dtype, (self.batch_size,))
        self.alpha_var = tf.Variable(tf.zeros((self.batch_size,), dtype=self.model.x_dtype))
        # expand dim for easier broadcast operations
        eps = tf.expand_dims(self.eps_var, 1)
        alpha = tf.expand_dims(self.alpha_var, 1)
        # calculate loss' gradient with relate to the adversarial example
        # grad.shape == (batch_size, D)
        self.xs_adv_model = tf.reshape(self.xs_adv_var, (batch_size, *self.model.x_shape))
        self.loss = loss(self.xs_adv_model, self.ys_var)
        grad = tf.gradients(self.loss, self.xs_adv_var)[0]
        if goal == 't' or goal == 'tm':
            grad = -grad
        elif goal != 'ut':
            raise NotImplementedError
        # 1-norm of gradient
        grad_l1 = tf.reduce_sum(tf.abs(grad), axis=1)
        # update the momentum term
        g_next = self.decay_factor_var * self.g_var + grad / tf.expand_dims(grad_l1, 1)
        self.update_g_step = self.g_var.assign(g_next)
        # update the adversarial example
        if distance_metric == 'l_2':
            g_unit = get_unit(self.g_var)
            xs_adv_delta = self.xs_adv_var - self.xs_var + alpha * g_unit
            # clip by max l_2 magnitude of adversarial noise
            xs_adv_next = self.xs_var + tf.clip_by_norm(xs_adv_delta, eps, axes=[1])
        elif distance_metric == 'l_inf':
            xs_lo, xs_hi = self.xs_var - eps, self.xs_var + eps
            g_sign = tf.sign(self.g_var)
            xs_adv_delta = self.xs_adv_var - self.xs_var + alpha * g_sign
            # clip by max l_inf magnitude of adversarial noise
            xs_adv_next = self.xs_var + tf.clip_by_value(xs_adv_delta, xs_lo, xs_hi)
        else:
            raise NotImplementedError
        # clip by (x_min, x_max)
        xs_adv_next = tf.clip_by_value(xs_adv_next, self.model.x_min, self.model.x_max)
        self.update_xs_adv_step = self.xs_adv_var.assign(xs_adv_next)

        self.config_eps_step = self.eps_var.assign(self.eps_ph)
        self.config_alpha_step = self.alpha_var.assign(self.alpha_ph)
        self.config_decay_factor_step = self.decay_factor_var.assign(self.decay_factor_ph)
        self.setup_xs = [self.xs_var.assign(tf.reshape(self.xs_ph, xs_flatten_shape)),
                         self.xs_adv_var.assign(tf.reshape(self.xs_ph, xs_flatten_shape))]
        self.setup_ys = self.ys_var.assign(self.ys_ph)
        self.setup_g = tf.variables_initializer([self.g_var])
        self.iteration = None

    def config(self, **kwargs):
        if 'magnitude' in kwargs:
            eps = maybe_to_array(kwargs['magnitude'], self.batch_size)
            self._session.run(self.config_eps_step, feed_dict={self.eps_ph: eps})
        if 'alpha' in kwargs:
            alpha = maybe_to_array(kwargs['alpha'], self.batch_size)
            self._session.run(self.config_alpha_step, feed_dict={self.alpha_ph: alpha})
        if 'decay_factor' in kwargs:
            decay_factor = kwargs['decay_factor']
            self._session.run(self.config_decay_factor_step, feed_dict={self.decay_factor_ph: decay_factor})
        if 'iteration' in kwargs:
            self.iteration = kwargs['iteration']

    def batch_attack(self, xs, ys=None, ys_target=None):
        lbs = ys if self.goal == 'ut' else ys_target
        self._session.run(self.setup_xs, feed_dict={self.xs_ph: xs})
        self._session.run(self.setup_ys, feed_dict={self.ys_ph: lbs})
        self._session.run(self.setup_g)
        for _ in range(self.iteration):
            self._session.run(self.update_g_step)
            self._session.run(self.update_xs_adv_step)
        return self._session.run(self.xs_adv_model)

from abc import abstractmethod
from typing import Dict, Optional, Tuple

import flax
import jax
import jax.numpy as jnp
from flax.training.train_state import TrainState

from tango.common.registrable import Registrable

from .model import Model
from .optim import LRScheduler, Optimizer
from .util import get_PRNGkey


class FlaxTrainWrapper(Registrable):
    @abstractmethod
    def compute_metrics(self, logits, labels) -> Dict:
        pass

    @abstractmethod
    def loss_fn(self, params, batch, logits, labels, dropout_rng):
        pass

    @abstractmethod
    def eval_fn(self, batch, state, model):
        pass


class FlaxTrainState:
    def __init__(
        self,
        model: Model,
        optimizer: Optimizer,
        train_wrapper: FlaxTrainWrapper,
        do_distributed: bool,
        lr_scheduler: Optional[LRScheduler] = None,
        shape: Optional[list] = None,
    ):
        self.model = model
        self.optimizer = optimizer
        self.lr_scheduler = (lr_scheduler,)
        self.train_wrapper = train_wrapper
        self.do_distributed = do_distributed
        self.state = self.create_train_state(shape)

    def create_train_state(self, shape):
        print(self.model)
        if hasattr(self.model, "params"):
            params = self.model.params
        else:
            x = jnp.ones(shape)
            params = self.model.init(get_PRNGkey(), x)["params"]
        self.state = TrainState.create(
            apply_fn=self.model.__call__, params=params, tx=self.optimizer
        )
        return self.state

    def train_state(self, batch, dropout_rng):
        # if transformer model
        labels = batch.pop("labels")
        logits = self.state.apply_fn(**batch, params=self.state.params, dropout_rng=dropout_rng, train=True)[0]
        grad_fn = jax.value_and_grad(self.train_wrapper.loss_fn)
        loss, grad = grad_fn(self.state.params, batch, logits, labels, dropout_rng)
        if self.do_distributed:
            grad = jax.lax.pmean(grad, "batch")
        self.state = self.state.apply_gradients(grads=grad)
        other_metrics = self.train_wrapper.compute_metrics(logits=logits, labels=labels)
        metrics = {"loss": loss}
        metrics.update(other_metrics)
        if self.do_distributed:
            metrics = jax.lax.pmean(metrics, axis_name="batch")
        return metrics

    def val_state(self, batch) -> Dict:
        logits = self.train_wrapper.eval_fn(batch, self.state, self.model)
        metrics = self.train_wrapper.compute_metrics(logits=logits, labels=batch["labels"])
        if self.do_distributed:
            metrics = jax.lax.pmean(metrics, axis_name="batch")
        return metrics

    def replicate_state(self):
        self.state = flax.jax_utils.replicate(self.state)
        return self.state

    def unreplicate_state(self):
        self.state = flax.jax_utils.unreplicate(self.state)
        return self.state

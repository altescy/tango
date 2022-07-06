from abc import abstractmethod
from collections import defaultdict
from itertools import islice
from typing import Dict, List, Optional, Sequence

import jax
from flax.training.train_state import TrainState

from tango.common.dataset_dict import DatasetDictBase
from tango.common.exceptions import ConfigurationError
from tango.common.lazy import Lazy
from tango.common.registrable import Registrable
from tango.common.tqdm import Tqdm
from tango.format import Format, JsonFormat
from tango.step import Step

from .data import FlaxDataLoader
from .eval_callback import EvalCallback
from .model import Model
from .util import get_PRNGkey


class FlaxEvalWrapper(Registrable):
    @abstractmethod
    def eval_fn(self, state, batch, model):
        """
        returns logits and metrics.
        """
        pass


@Step.register("flax::eval")
class FlaxEvalStep(Step):
    DETERMINISTIC = True
    CACHEABLE = True
    FORMAT: Format = JsonFormat()
    SKIP_ID_ARGUMENTS = {"log_every"}

    def run(  # type: ignore[override]
        self,
        model: Model,
        state: TrainState,
        dataset: DatasetDictBase,
        dataloader: Lazy[FlaxDataLoader],
        eval_wrapper: FlaxEvalWrapper,
        test_split: str = "test",
        seed: int = 42,
        log_every: int = 1,
        do_distributed: bool = False,
        eval_steps: Optional[int] = None,
        metric_names: Sequence[str] = ("loss",),
        auto_aggregate_metrics: bool = True,
        callbacks: Optional[List[Lazy[EvalCallback]]] = None,
    ) -> Dict[str, float]:

        # construct dataloader
        dataloader: FlaxDataLoader = dataloader.construct(dataset=dataset[test_split])
        # init model params
        model.params = state.params

        steps: int
        try:
            dataloader_len = dataloader.dataset_size
            steps = dataloader_len if eval_steps is None else min(dataloader_len, eval_steps)
        except TypeError:
            if eval_steps is None:
                raise ConfigurationError(
                    "You must set 'eval_steps' for streaming/iterable datasets"
                )
            else:
                steps = eval_steps

        if do_distributed:
            devices = jax.devices()
            if len(devices) <= 1:
                raise ConfigurationError(
                    "YOu have set distributed training=True but there is only one device."
                )

        # Initialize callbacks
        callbacks: List[EvalCallback] = [
            callback.construct(
                step_id=self.unique_id,
                work_dir=self.work_dir,
                model=model,
                dataset_dict=dataset,
                dataloader=dataloader,
            )
            for callback in (callbacks or [])
        ]

        for callback in callbacks:
            callback.pre_eval_loop()

        def eval_step(state, batch):
            logits, metrics = eval_wrapper.eval_fn(state, batch, model)
            if do_distributed:
                metrics = jax.lax.pmean(metrics, axis_name="batch")
            return logits, metrics

        rng = get_PRNGkey(seed)
        eval_batches = enumerate(islice(dataloader(rng, do_distributed), steps))

        running_metrics: Dict[str, float] = defaultdict(float)
        aggregated_metrics: Dict[str, float] = defaultdict(float)

        with Tqdm.tqdm(eval_batches, desc="Evaluating", total=steps) as batch_iter:
            for step, batch in batch_iter:
                should_log_this_step = step % log_every == 0 or step == steps - 1
                for callback in callbacks:
                    callback.pre_batch(step, batch)

                if do_distributed:
                    parallel_eval_step = jax.pmap(eval_step, axis_name="batch")
                    logits, metrics = parallel_eval_step(state, batch)
                    metrics = jax.lax.pmean(metrics, axis_name="batch")
                else:
                    logits, metrics = eval_step(state, batch)

                metrics = jax.device_get(metrics)

                for callback in callbacks:
                    callback.post_batch(step, logits)

                if auto_aggregate_metrics:
                    for key, val in metrics.items():
                        if key in metric_names:
                            running_metrics[key] += metrics[key].item()
                            aggregated_metrics[key] = running_metrics[key] / (step + 1)
                else:
                    aggregated_metrics.update(metrics)

                if should_log_this_step:
                    batch_iter.set_postfix(**aggregated_metrics)
                del batch

        for callback in callbacks:
            callback.post_eval_loop(aggregated_metrics)

        return aggregated_metrics
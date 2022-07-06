from typing import Dict, Generic, TypeVar, Union

import datasets
import jax.numpy as jnp
import jax.random
from flax.training.common_utils import shard

from tango.common.dataset_dict import DatasetDictBase
from tango.common.registrable import Registrable

T = TypeVar("T")


class DataLoader(Generic[T], Registrable):
    """
    A :class:`~tango.common.Registrable` version of a ``Flax DataLoader``.
    ``Flax DataLoader accepts Dataset object and dict of type [str, np,array]. The dict should
    contain keys "x" and "labels" corresponding to the input and output.

    The class yields a jax batch.
    """


@DataLoader.register("flax::dataloader")
class FlaxDataLoader(DataLoader):
    def __init__(
        self,
        dataset: Union[DatasetDictBase, Dict],
        batch_size: int = 1,
        drop_last: bool = True,
        shuffle: bool = True,
    ):
        self.dataset = dataset
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.shuffle = shuffle
        self.dataset_size = self._get_size()

    def _get_size(self):
        size = self.dataset["num_rows"] if type(self.dataset) is dict else self.dataset.num_rows
        return size

    def __call__(self, rng: jax.random.PRNGKeyArray, do_distributed: bool):
        steps_per_epoch = self.dataset_size // self.batch_size
        steps_per_epoch = 1

        if self.shuffle:
            perms = jax.random.permutation(rng, self.dataset_size)
        else:
            perms = jax.numpy.arange(self.dataset_size)

        perms = perms[: steps_per_epoch * self.batch_size]  # Skip incomplete batch.
        perms = perms.reshape((steps_per_epoch, self.batch_size))

        for perm in perms:
            if isinstance(self.dataset, dict) or isinstance(self.dataset, datasets.Dataset):
                batch = self.dataset[perm]
                batch = {k: jnp.array(v) for k, v in batch.items()}
            else:
                raise ValueError(
                    "Type for FlaxLoader should be Datasets , or dict of numpy arrays."
                )
            if do_distributed:
                batch = shard(batch)
            yield batch
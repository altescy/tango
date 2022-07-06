import jax
import jax.numpy as jnp
import numpy as np
import optax
import tensorflow_datasets as tfds
from flax import linen as nn
from flax.training.common_utils import onehot
from transformers import AutoConfig, AutoTokenizer, FlaxAutoModelForSeq2SeqLM

from tango.integrations.flax import FlaxEvalWrapper, FlaxTrainWrapper, Model
from tango.step import Step


@Step.register("load_mnist_data")
class LoadMNISTData(Step):
    DETERMINISTIC = True
    CACHEABLE = True

    def run(self):
        ds_builder = tfds.builder("mnist")
        ds_builder.download_and_prepare()
        train_ds = tfds.as_numpy(ds_builder.as_dataset(split="train", batch_size=-1))
        train_ds["x"] = train_ds["image"] / 255.0
        train_ds["labels"] = train_ds["label"]
        train_ds["num_rows"] = len(train_ds["x"])
        test_ds = tfds.as_numpy(ds_builder.as_dataset(split="test", batch_size=-1))
        test_ds["x"] = test_ds["image"] / 255.0
        test_ds["labels"] = test_ds["label"]
        test_ds["num_rows"] = len(test_ds["x"])
        dataset = {"train": train_ds, "test": test_ds}
        return dataset


@Model.register("mnist")
class MNIST(Model):
    """
    A simple CNN model
    """

    @nn.compact
    def __call__(self, x):
        x = nn.Conv(features=32, kernel_size=(3, 3))(x)
        x = nn.relu(x)
        x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
        x = nn.Conv(features=64, kernel_size=(3, 3))(x)
        x = nn.relu(x)
        x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
        x = x.reshape((x.shape[0], -1))  # flatten
        x = nn.Dense(features=256)(x)
        x = nn.relu(x)
        x = nn.Dense(features=10)(x)
        return x


@FlaxTrainWrapper.register("mnist_train_wrapper")
class TrainWrapper(FlaxTrainWrapper):
    def __init__(self):
        self.model = MNIST()

    def compute_metrics(self, logits, labels):
        def cross_entropy_loss(logits, labels):
            labels_onehot = jax.nn.one_hot(labels, num_classes=10)
            return optax.softmax_cross_entropy(logits=logits, labels=labels_onehot).mean()

        loss = cross_entropy_loss(logits=logits, labels=labels)
        accuracy = jnp.mean(jnp.argmax(logits, -1) == labels)
        metrics = {
            "loss": loss,
            "accuracy": accuracy,
        }
        return metrics

    def loss_fn(self, params, batch, state, dropout_rng):
        """
        Compute loss and metrics during train.
        """

        def compute_loss(logits, labels):
            labels_onehot = jax.nn.one_hot(labels, num_classes=10)
            loss = optax.softmax_cross_entropy(logits=logits, labels=labels_onehot).mean()
            return loss

        labels = batch["labels"]
        logits = self.model.apply({"params": params}, batch["x"])
        loss = compute_loss(logits, labels)
        return loss, logits

    def eval_fn(self, batch, state, model):
        """
        Compute loss and metrics during eval.
        """
        logits = self.model.apply({"params": state.params}, batch["x"])
        return logits


@FlaxEvalWrapper.register("mnist_eval_wrapper")
class EvalWrapper(FlaxEvalWrapper):
    def __init__(self):
        self.model = MNIST()

    def compute_metrics(self, logits, labels):
        def cross_entropy_loss(logits, labels):
            labels_onehot = jax.nn.one_hot(labels, num_classes=10)
            return optax.softmax_cross_entropy(logits=logits, labels=labels_onehot).mean()

        loss = cross_entropy_loss(logits=logits, labels=labels)
        accuracy = jnp.mean(jnp.argmax(logits, -1) == labels)
        metrics = {
            "loss": loss,
            "accuracy": accuracy,
        }
        return metrics

    def eval_step(self, state, batch):
        logits = self.model.apply({"params": state.params}, batch["x"])
        metrics = self.compute_metrics(logits=logits, labels=batch["label"])
        return logits, metrics


"""
Transformer model
"""


@Step.register("tokenize_data")
class PreProcessing(Step):
    def run(self, dataset):
        tokenizer = AutoTokenizer.from_pretrained("facebook/bart-base")
        model = FlaxAutoModelForSeq2SeqLM.from_pretrained("facebook/bart-base")
        model_module = __import__(model.__module__, fromlist=["shift_tokens_tight"])
        shift_tokens_right_fn = getattr(model_module, "shift_tokens_right")
        config = AutoConfig.from_pretrained("facebook/bart-base")

        MAX_SOURCE_LENGTH = 512
        MAX_TGT_LENGTH = 64

        def preprocess_function(examples):
            inputs = examples["document"]
            targets = examples["summary"]
            inputs = [inp for inp in inputs]
            model_inputs = tokenizer(
                inputs,
                max_length=MAX_SOURCE_LENGTH,
                padding="max_length",
                truncation=True,
                return_tensors="np",
            )

            # Setup the tokenizer for targets
            with tokenizer.as_target_tokenizer():
                labels = tokenizer(
                    targets,
                    max_length=MAX_TGT_LENGTH,
                    padding="max_length",
                    truncation=True,
                    return_tensors="np",
                )

            model_inputs["labels"] = labels["input_ids"]
            decoder_input_ids = shift_tokens_right_fn(
                labels["input_ids"], config.pad_token_id, config.decoder_start_token_id
            )
            model_inputs["decoder_input_ids"] = np.asarray(decoder_input_ids)

            # We need decoder_attention_mask so we can ignore pad tokens from loss
            model_inputs["decoder_attention_mask"] = labels["attention_mask"]

            return model_inputs

        column_names = dataset["train"].column_names

        dataset = dataset.map(
            preprocess_function,
            batched=True,
            remove_columns=column_names,
            desc="Running tokenizer on dataset",
        )
        return dataset


@FlaxTrainWrapper.register("xsum_train_wrapper")
class TransformerTrainWrapper(FlaxTrainWrapper):
    def compute_metrics(self, logits, labels):
        # return empty dict if no other metrics to compute
        return {}

    def loss_helper(self, logits, labels, batch):
        label_smoothing_factor = 0
        padding_mask = batch["decoder_attention_mask"]
        vocab_size = logits.shape[-1]
        confidence = 1.0 - label_smoothing_factor
        low_confidence = (1.0 - confidence) / (vocab_size - 1)
        normalizing_constant = -(
            confidence * jnp.log(confidence)
            + (vocab_size - 1) * low_confidence * jnp.log(low_confidence + 1e-20)
        )
        soft_labels = onehot(labels, vocab_size, on_value=confidence, off_value=low_confidence)

        loss = optax.softmax_cross_entropy(logits, soft_labels)
        loss = loss - normalizing_constant

        # ignore padded tokens from loss
        loss = loss * padding_mask
        loss = loss.sum() / padding_mask.sum()
        return loss

    def loss_fn(self, params, batch, logits, labels):
        loss = self.loss_helper(logits, labels, batch)
        return loss

    def eval_fn(self, batch, state, model):
        labels = batch.pop("labels")
        logits = model(**batch, params=model.params, train=False)[0]
        loss = self.loss_helper(logits, labels, batch)
        # summarize metrics
        metrics = {"loss": loss}
        return metrics


@FlaxEvalWrapper.register("xsum_eval_wrapper")
class TransformerEvalWrapper(FlaxEvalWrapper):
    def loss_helper(self, logits, labels, batch):
        label_smoothing_factor = 0
        padding_mask = batch["decoder_attention_mask"]
        vocab_size = logits.shape[-1]
        confidence = 1.0 - label_smoothing_factor
        low_confidence = (1.0 - confidence) / (vocab_size - 1)
        normalizing_constant = -(
            confidence * jnp.log(confidence)
            + (vocab_size - 1) * low_confidence * jnp.log(low_confidence + 1e-20)
        )
        soft_labels = onehot(labels, vocab_size, on_value=confidence, off_value=low_confidence)

        loss = optax.softmax_cross_entropy(logits, soft_labels)
        loss = loss - normalizing_constant

        # ignore padded tokens from loss
        loss = loss * padding_mask
        loss = loss.sum() / padding_mask.sum()
        return loss

    def eval_fn(self, state, batch, model):
        labels = batch.pop("labels")
        logits = model(**batch, params=state.params, train=False)[0]
        loss = self.loss_helper(logits, labels, batch)
        metrics = {"loss": loss}

        return logits, metrics
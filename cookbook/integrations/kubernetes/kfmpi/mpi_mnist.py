import os
import pathlib

import flytekit
import tensorflow as tf
import horovod.tensorflow as hvd
from flytekit import task, workflow, Resources
from flytekit.types.directory import FlyteDirectory
from flytekit.core.base_task import IgnoreOutputs
from flytekitplugins.kfmpi import MPIJob

@tf.function
def training_step(images, labels, first_batch, mnist_model, loss, opt):
    with tf.GradientTape() as tape:
        probs = mnist_model(images, training=True)
        loss_value = loss(labels, probs)

    # Horovod: add Horovod Distributed GradientTape.
    tape = hvd.DistributedGradientTape(tape)

    grads = tape.gradient(loss_value, mnist_model.trainable_variables)
    opt.apply_gradients(zip(grads, mnist_model.trainable_variables))

    # Horovod: broadcast initial variable states from rank 0 to all other processes.
    # This is necessary to ensure consistent initialization of all workers when
    # training is started with random weights or restored from a checkpoint.
    #
    # Note: broadcast should be done after the first gradient step to ensure optimizer
    # initialization.
    if first_batch:
        hvd.broadcast_variables(mnist_model.variables, root_rank=0)
        hvd.broadcast_variables(opt.variables(), root_rank=0)

    return loss_value


@task(
    task_config=MPIJob(
        num_workers=2,
        num_launcher_replicas=1,
        slots=1,
        per_replica_requests=Resources(mem="2000Mi", cpu="1"),
        per_replica_limits=Resources(mem="2000Mi", cpu="1"),
    ),
    retries=5,
    cache=True,
    cache_version="0.1",
)
def horovod_train_task(batch_size: int, buffer_size: int, dataset_size: int) -> FlyteDirectory:
    """
    :param batch_size: Represents the number of consecutive elements of this dataset to combine in a single batch.
    :param buffer_size: Defines the size of the buffer used to hold elements of the dataset used for training.
    :param dataset_size: The number of elements of this dataset that should be taken to form the new dataset when
        running batched training.
    """
    hvd.init()

    (mnist_images, mnist_labels), _ = \
        tf.keras.datasets.mnist.load_data(path='mnist-%d.npz' % hvd.rank())

    dataset = tf.data.Dataset.from_tensor_slices(
        (tf.cast(mnist_images[..., tf.newaxis] / 255.0, tf.float32),
         tf.cast(mnist_labels, tf.int64))
    )
    dataset = dataset.repeat().shuffle(buffer_size).batch(batch_size)

    mnist_model = tf.keras.Sequential([
        tf.keras.layers.Conv2D(32, [3, 3], activation='relu'),
        tf.keras.layers.Conv2D(64, [3, 3], activation='relu'),
        tf.keras.layers.MaxPooling2D(pool_size=(2, 2)),
        tf.keras.layers.Dropout(0.25),
        tf.keras.layers.Flatten(),
        tf.keras.layers.Dense(128, activation='relu'),
        tf.keras.layers.Dropout(0.5),
        tf.keras.layers.Dense(10, activation='softmax')
    ])
    loss = tf.losses.SparseCategoricalCrossentropy()

    # Horovod: adjust learning rate based on number of GPUs.
    opt = tf.optimizers.Adam(0.001 * hvd.size())

    # working_dir = flytekit.current_context().working_directory
    # checkpoint_dir = pathlib.Path(os.path.join(working_dir, "checkpoint"))
    # checkpoint_dir.mkdir(exist_ok=True)
    checkpoint_dir = "checkpoint"

    checkpoint = tf.train.Checkpoint(model=mnist_model, optimizer=opt)

    # Horovod: adjust number of steps based on number of GPUs.
    for batch, (images, labels) in enumerate(dataset.take(dataset_size // hvd.size())):
        loss_value = training_step(images, labels, batch == 0, mnist_model,loss, opt)

        if batch % 10 == 0 and hvd.local_rank() == 0:
            print('Step #%d\tLoss: %.6f' % (batch, loss_value))

    if hvd.rank() != 0:
        raise IgnoreOutputs("I am not rank 0")
    else:
        checkpoint.save(str(checkpoint_dir))
        return FlyteDirectory(path=str(checkpoint_dir))


@workflow
def horovod_training_wf(batch_size: int = 128, buffer_size: int = 10000, dataset_size: int = 10000) -> FlyteDirectory:
    """
    :param batch_size: Represents the number of consecutive elements of this dataset to combine in a single batch.
    :param buffer_size: Defines the size of the buffer used to hold elements of the dataset used for training.
    :param dataset_size: The number of elements of this dataset that should be taken to form the new dataset when
        running batched training.
    """
    return horovod_train_task(batch_size=batch_size, buffer_size=buffer_size, dataset_size=dataset_size)

if __name__ == "__main__":
    model, plot, logs = horovod_training_wf()
    print(f"Model: {model}, plot PNG: {plot}, Tensorboard Log Dir: {logs}")
# Copyright 2017-2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse

import tensorflow as tf
import matplotlib.pyplot as plt
import numpy as np

import transformer_model
import text_processor

flags = tf.compat.v1.flags

# Configuration
flags.DEFINE_string("data_dir", default="data/",
      help="data directory")
flags.DEFINE_string("model_dir", default="model/",
      help="directory of model")
flags.DEFINE_integer("train_steps", default=10000,
      help="number of training steps")
flags.DEFINE_integer("vocab_level", default=13,
      help="base 2 exponential of the expected vocab size")
flags.DEFINE_float("dropout", default=0.3,
      help="dropout rate")
flags.DEFINE_integer("heads", default=4,
      help="number of heads")
flags.DEFINE_integer("seq_len", default=48,
      help="length of the each fact")
flags.DEFINE_integer("sparse_len", default=2,
      help="the length of the sparse representation")
flags.DEFINE_integer("batch_size", default=128,
      help="batch size")
flags.DEFINE_integer("layers", default=2,
      help="number of layers")
flags.DEFINE_integer("depth", default=128,
      help="the size of the attention layer")
flags.DEFINE_integer("feedforward", default=128,
      help="the size of feedforward layer")

flags.DEFINE_bool("train", default=True,
      help="whether to train")
flags.DEFINE_bool("evaluate", default=True,
      help="whether to evaluate")
flags.DEFINE_bool("predict", default=True,
      help="whether to predict")
flags.DEFINE_integer("predict_samples", default=10,
      help="the number of samples to predict")

FLAGS = flags.FLAGS

INPUT_TENSOR_NAME = "inputs"
SIGNATURE_NAME = "serving_default"
training_file = "training.tfrecords"

HEIGHT = 32
WIDTH = 32
DEPTH = 3
NUM_CLASSES = 10
NUM_DATA_BATCHES = 5
NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 10000 * NUM_DATA_BATCHES
NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 10000
RESNET_SIZE = 32
BATCH_SIZE = 1

# Scale the learning rate linearly with the batch size. When the batch size is
# 128, the learning rate should be 0.05.
_INITIAL_LEARNING_RATE = 0.05 * BATCH_SIZE / 128
_MOMENTUM = 0.9

# We use a weight decay of 0.0002, which performs better than the 0.0001 that
# was originally suggested.
_WEIGHT_DECAY = 2e-4

_BATCHES_PER_EPOCH = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / BATCH_SIZE


def model_fn(features, labels, mode, params):
    facts = features["input_ids"]
    vocab_size = params['vocab_size'] + 2

    network = transformer_model.TED_generator(vocab_size, FLAGS)

    logits, attention_weights = network(facts, mode == tf.estimator.ModeKeys.TRAIN)

    def loss_function(real, pred):
        mask = tf.math.logical_not(tf.math.equal(real, 0))  # Every element that is NOT padded
        # They will have to deal with run on sentences with this kind of setup
        loss_ = tf.keras.losses.sparse_categorical_crossentropy(real, pred, from_logits=True)

        mask = tf.cast(mask, dtype=loss_.dtype)
        loss_ *= mask

        return tf.reduce_mean(loss_)

    # Add weight decay to the loss.
    loss = loss_function(tf.slice(facts, [0, 1], [-1, -1]), logits)

    # Create a tensor named cross_entropy for logging purposes.
    tf.identity(loss, name='loss')
    tf.summary.scalar('loss', loss)

    predictions = {
        'original': features["input_ids"],
        'prediction': tf.argmax(logits, 2),
        'sparse_attention': attention_weights
    }

    if mode == tf.estimator.ModeKeys.PREDICT:
        export_outputs = {
            SIGNATURE_NAME: tf.estimator.export.PredictOutput(predictions)
        }
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions, export_outputs=export_outputs)

    if mode == tf.estimator.ModeKeys.TRAIN:
        global_step = tf.compat.v1.train.get_or_create_global_step()

        optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=1e-5, beta2=0.98, epsilon=1e-9)

        # Batch norm requires update ops to be added as a dependency to the train_op
        update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss, global_step)
    else:
        train_op = None

    return tf.estimator.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        loss=loss,
        train_op=train_op)

def file_based_input_fn_builder(input_file, sequence_length, batch_size, is_training, drop_remainder):

    name_to_features = {
      "input_ids": tf.io.FixedLenFeature([sequence_length], tf.int64),
    }

    def _decode_record(record, name_to_features):
        """Decodes a record to a TensorFlow example."""
        example = tf.io.parse_single_example(record, name_to_features)

        # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
        # So cast all int64 to int32.
        for name in list(example.keys()):
            t = example[name]
        if t.dtype == tf.int64:
            t = tf.cast(t, tf.int32)
            example[name] = t

        return example

    def input_fn(params):
        """The actual input function."""

        # For training, we want a lot of parallel reading and shuffling.
        # For eval, we want no shuffling and parallel reading doesn't matter.
        d = tf.data.TFRecordDataset("encoded_data/" + input_file + ".tfrecords")
        if is_training:
            d = d.shuffle(buffer_size=1024)
            d = d.repeat()

        d = d.map(lambda record: _decode_record(record, name_to_features)).batch(batch_size=batch_size,
                                                                                 drop_remainder=drop_remainder)

        return d

    return input_fn


def main(argv=None):
    mirrored_strategy = tf.distribute.MirroredStrategy()
    config = tf.estimator.RunConfig(
        train_distribute=mirrored_strategy, eval_distribute=mirrored_strategy)

    vocab_size, tokenizer = text_processor.text_processor(FLAGS.data_dir, FLAGS.seq_len, FLAGS.vocab_level, "encoded_data")

    estimator = tf.estimator.Estimator(model_fn=model_fn, model_dir=FLAGS.model_dir, params={'vocab_size': vocab_size},
                                       config=config)

    if FLAGS.train:
        print("***************************************")
        print("Training")
        print("***************************************")

        train_input_fn = file_based_input_fn_builder(
            input_file="training",
            sequence_length=FLAGS.seq_len,
            batch_size=FLAGS.batch_size,
            is_training=True,
            drop_remainder=True)

        estimator.train(
            input_fn=train_input_fn,
            steps=FLAGS.train_steps)

    if FLAGS.evaluate:
        print("***************************************")
        print("Evaluating")
        print("***************************************")

        eval_input_fn = file_based_input_fn_builder(
            input_file="testing",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        print("Evaluation loss: " + str(estimator.evaluate(input_fn=eval_input_fn)))

    if FLAGS.predict:
        print("***************************************")
        print("Predicting")
        print("***************************************")

        pred_input_fn = file_based_input_fn_builder(
            input_file="predict",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        print("Started predicting")

        results = estimator.predict(input_fn=pred_input_fn, predict_keys=['prediction', 'original', 'sparse_attention'])

        print("Ended predicting")

        for i, result in enumerate(results):
            print("------------------------------------")
            output_sentence = result['prediction']
            input_sentence = result['original']
            attention = result['sparse_attention']
            print("result: " + str(output_sentence))
            print("decoded: " + str(tokenizer.decode([i for i in output_sentence if i < tokenizer.vocab_size])))
            print("original: " + str(tokenizer.decode([i for i in input_sentence if i < tokenizer.vocab_size])))

            if i + 1 == FLAGS.predict_samples:
                plot_attention_weights(attention, input_sentence, tokenizer)
                break

        print("Ended showing result")


def plot_attention_weights(attention, encoded_sentence, tokenizer):
    fig = plt.figure(figsize=(16, 8))
    result = list(range(FLAGS.sparse_len))

    sentence = encoded_sentence

    for head in range(attention.shape[0]):
        ax = fig.add_subplot(2, 4, head + 1)

        # plot the attention weights
        ax.matshow(attention[head][:, :], cmap='viridis')

        fontdict = {'fontsize': 10}

        ax.set_xticks(range(len(sentence) + 2))
        ax.set_yticks(range(len(result)))

        #ax.set_ylim(len(result) - 1.5, -0.5)

        ax.set_xticklabels(
            ['<start>'] + [tokenizer.decode([i]) for i in sentence if i < tokenizer.vocab_size] + ['<end>'],
            fontdict=fontdict, rotation=90)

        ax.set_yticklabels(result, fontdict=fontdict)

        ax.set_xlabel('Head {}'.format(head + 1))

    plt.tight_layout()
    plt.show()

if __name__ == '__main__':
    tf.compat.v1.app.run()

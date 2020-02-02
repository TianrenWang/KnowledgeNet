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


import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt

import transformer_model
import text_processor

flags = tf.compat.v1.flags

# Configuration
flags.DEFINE_string("data_dir", default="data/",
      help="data directory")
flags.DEFINE_string("model_dir", default="model/",
      help="directory of model")
flags.DEFINE_integer("train_steps", default=100000,
      help="number of training steps")
flags.DEFINE_integer("embed_steps", default=50000,
      help="number of embedding steps")
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
flags.DEFINE_integer("sparse_lim", default=6,
      help="maximum number of keys each query can attend to")
flags.DEFINE_bool("use_sparse", default=False,
      help="whether to use sparse attention")
flags.DEFINE_float("sparse_thresh", default=0.0,
      help="the threshold to keep the attention weight")
flags.DEFINE_float("conc", default=1.4,
      help="concentration factor multiplier")
flags.DEFINE_float("sparse_loss", default=0,
      help="sparse loss multiplier")
flags.DEFINE_float("alpha", default=0.98,
      help="exponentially smoothed average constant")
flags.DEFINE_integer("graph_size", default=512,
      help="the number of nodes in the graph")
flags.DEFINE_integer("batch_size", default=128,
      help="batch size for training")
flags.DEFINE_integer("embed_batch_size", default=128,
      help="the batch size for the embedding steps")
flags.DEFINE_integer("layers", default=2,
      help="number of layers")
flags.DEFINE_integer("depth", default=128,
      help="the size of the attention layer")
flags.DEFINE_integer("feedforward", default=128,
      help="the size of feedforward layer")

flags.DEFINE_bool("train", default=True,
      help="whether to train")
flags.DEFINE_bool("embed", default=True,
      help="whether to embed the graph")
flags.DEFINE_bool("predict", default=True,
      help="whether to predict")
flags.DEFINE_integer("predict_samples", default=10,
      help="the number of samples to predict")

FLAGS = flags.FLAGS
flags = tf.compat.v1.flags.FLAGS.flag_values_dict()
for i, key in enumerate(flags.keys()):
    if i > 18:
        print(key + ": " + str(flags[key]))

SIGNATURE_NAME = "serving_default"
encoderLayerNames = ['encoder_layer{}'.format(i + 1) for i in range(FLAGS.layers)]


def model_fn(features, labels, mode, params):
    sentences = features["input_ids"]
    facts = tf.cast(features["input_fact"], tf.int32)
    vocab_size = params['vocab_size'] + 2
    is_embedding = params['embedding']

    network = transformer_model.TED_generator(vocab_size, FLAGS)

    logits, encoder_attention_weights, compress_attention, pickOut_attention, projection_attention = network(sentences, facts, mode == tf.estimator.ModeKeys.TRAIN, is_embedding)

    def loss_function(real, pred):
        mask = tf.math.logical_not(tf.math.equal(real, 0))  # Every element that is NOT padded
        # They will have to deal with run on sentences with this kind of setup
        loss_ = tf.keras.losses.sparse_categorical_crossentropy(real, pred, from_logits=True)

        mask = tf.cast(mask, dtype=loss_.dtype)
        loss_ *= mask

        return tf.reduce_mean(loss_)

    # Calculate the loss
    loss = loss_function(tf.slice(sentences, [0, 1], [-1, -1]), logits)
    sparse_loss = tf.zeros(tf.shape(logits)[0])
    sparse_loss = tf.math.square(pickOut_attention * FLAGS.conc)
    sparse_loss = tf.reduce_sum(sparse_loss, axis=-1) / FLAGS.conc
    sparse_loss = tf.math.abs(tf.math.log(tf.math.sqrt(sparse_loss)))
    sparse_loss = tf.reduce_sum(sparse_loss, axis=-1) * tf.cast(facts, tf.float32)
    loss = tf.cond(tf.constant(is_embedding), lambda: loss + FLAGS.sparse_loss * tf.reduce_mean(sparse_loss), lambda: loss)

    # Create a tensor named cross_entropy for logging purposes.
    tf.identity(loss, name='loss')
    tf.summary.scalar('loss', loss)

    predictions = {
        'original': features["input_ids"],
        'prediction': tf.argmax(logits, 2),
        'sparse_attention': compress_attention,
        'pickout_attention': pickOut_attention,
        'projection_attention': tf.nn.softmax(2 - projection_attention),
        'sparse_loss': sparse_loss
    }

    for i, weight in enumerate(encoder_attention_weights):
        predictions["encoder_layer" + str(i + 1)] = weight

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
        "input_len": tf.io.FixedLenFeature([1], tf.int64),
        "input_fact": tf.io.FixedLenFeature([1], tf.int64)
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

    estimator = tf.estimator.Estimator(model_fn=model_fn, model_dir=FLAGS.model_dir,
                                       params={'vocab_size': vocab_size, 'embedding': False}, config=config)

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

        trainspec = tf.estimator.TrainSpec(
            input_fn=train_input_fn,
            max_steps=FLAGS.train_steps)

        eval_input_fn = file_based_input_fn_builder(
            input_file="testing",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        evalspec = tf.estimator.EvalSpec(
            input_fn=eval_input_fn)

        tf.estimator.train_and_evaluate(estimator, trainspec, evalspec)

        updates = estimator.get_variable_value("nodeUpdates").astype(int)
        values = estimator.get_variable_value("nodes")

        for i in range(len(updates)):
            print(str(i) + ": Updates: " + str(updates[i]) + " -- values: " + str(np.sum(np.abs(values[i]))))

        print("non-zeros: " + str(np.count_nonzero(estimator.get_variable_value("nodeUpdates").astype(int))))
        print("total: " + str(np.sum(updates)))


    if FLAGS.embed:
        print("***************************************")
        print("Embedding")
        print("***************************************")

        estimator = tf.estimator.Estimator(model_fn=model_fn, model_dir=FLAGS.model_dir,
                                           params={'vocab_size': vocab_size, 'embedding': True},
                                           config=config)

        train_input_fn = file_based_input_fn_builder(
            input_file="facts_only_training",
            sequence_length=FLAGS.seq_len,
            batch_size=FLAGS.embed_batch_size,
            is_training=True,
            drop_remainder=True)

        trainspec = tf.estimator.TrainSpec(
            input_fn=train_input_fn,
            max_steps=FLAGS.embed_steps)

        eval_input_fn = file_based_input_fn_builder(
            input_file="facts_only_testing",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        evalspec = tf.estimator.EvalSpec(
            input_fn=eval_input_fn)

        tf.estimator.train_and_evaluate(estimator, trainspec, evalspec)

        updates = estimator.get_variable_value("nodeUpdates").astype(int)
        values = estimator.get_variable_value("nodes")

        for i in range(len(updates)):
            print(str(i) + ": Updates: " + str(updates[i]) + " -- values: " + str(np.sum(np.abs(values[i]))))

        print("non-zeros: " + str(np.count_nonzero(estimator.get_variable_value("nodeUpdates").astype(int))))
        print("total: " + str(np.sum(updates)))

    if FLAGS.predict:
        print("***************************************")
        print("Predicting")
        print("***************************************")

        pred_input_fn = file_based_input_fn_builder(
            input_file="facts_only_testing",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        results = estimator.predict(input_fn=pred_input_fn, predict_keys=['prediction', 'original', 'sparse_attention',
                                                                          'pickout_attention', 'projection_attention',
                                                                          'sparse_loss'] + encoderLayerNames)

        for i, result in enumerate(results):
            print("------------------------------------")
            output_sentence = result['prediction']
            input_sentence = result['original']
            sparse_attention = result['sparse_attention']
            sparse_loss = result['sparse_loss']
            print("result: " + str(output_sentence))
            print("sparse loss: " + str(sparse_loss))
            print("decoded: " + str(tokenizer.decode([i for i in output_sentence if i < tokenizer.vocab_size])))
            print("original: " + str(tokenizer.decode([i for i in input_sentence if i < tokenizer.vocab_size])))
            print("pickout attention: " + str(np.sort(result['pickout_attention'])[:, :, -3:]))
            print("pickout indices: " + str(np.argsort(result['pickout_attention'])[:, :, -3:]))
            print("projection_attention: " + str(np.sort(result['projection_attention'])[:, -3:]))
            print("projection indices: " + str(np.argsort(result['projection_attention'])[:, -3:]))
            # plot_attention_weights(sparse_attention, input_sentence, tokenizer, True)

            if i + 1 == FLAGS.predict_samples:
                # for layerName in encoderLayerNames:
                #     plot_attention_weights(result[layerName], input_sentence, tokenizer, False)
                break

        print("***************************************")
        print("Verifying Connections")
        print("***************************************")

        connection_input_fn = file_based_input_fn_builder(
            input_file="connections",
            sequence_length=FLAGS.seq_len,
            batch_size=1,
            is_training=False,
            drop_remainder=True)

        results = estimator.predict(input_fn=connection_input_fn, predict_keys=['prediction', 'original', 'sparse_attention',
                                                                          'pickout_attention', 'projection_attention',
                                                                          'sparse_loss'] + encoderLayerNames)

        for i, result in enumerate(results):
            print("------------------------------------")
            output_sentence = result['prediction']
            input_sentence = result['original']
            sparse_attention = result['sparse_attention']
            sparse_loss = result['sparse_loss']
            print("result: " + str(output_sentence))
            print("sparse loss: " + str(sparse_loss))
            print("decoded: " + str(tokenizer.decode([i for i in output_sentence if i < tokenizer.vocab_size])))
            print("original: " + str(tokenizer.decode([i for i in input_sentence if i < tokenizer.vocab_size])))
            print("pickout attention: " + str(np.sort(result['pickout_attention'])[:, :, -3:]))
            print("pickout indices: " + str(np.argsort(result['pickout_attention'])[:, :, -3:]))
            print("projection_attention: " + str(np.sort(result['projection_attention'])[:, -3:]))
            print("projection indices: " + str(np.argsort(result['projection_attention'])[:, -3:]))
            # plot_attention_weights(sparse_attention, input_sentence, tokenizer, True)
            #
            # if i + 1 == FLAGS.predict_samples:
            #     # for layerName in encoderLayerNames:
            #     #     plot_attention_weights(result[layerName], input_sentence, tokenizer, False)
            #     break

        print("Ended showing result")


def plot_attention_weights(attention, encoded_sentence, tokenizer, compressed):
    fig = plt.figure(figsize=(16, 8))
    result = list(range(attention.shape[1]))

    sentence = encoded_sentence
    fontdict = {'fontsize': 10}

    for head in range(attention.shape[0]):
        ax = fig.add_subplot(2, 4, head + 1)

        input_sentence = ['<start>'] + [tokenizer.decode([i]) for i in sentence if i < tokenizer.vocab_size and i != 0] + ['<end>']
        output_sentence = input_sentence

        ax.set_xticklabels(input_sentence, fontdict=fontdict, rotation=90)

        if compressed: # check if this is the compressed layer
            output_sentence = list(range(FLAGS.sparse_len))

        ax.set_yticklabels(output_sentence, fontdict=fontdict)

        # plot the attention weights
        ax.matshow(attention[head][:len(output_sentence), :len(input_sentence)], cmap='viridis')

        ax.set_xticks(range(len(sentence) + 2))
        ax.set_yticks(range(len(result)))

        ax.set_ylim(len(output_sentence) - 1, 0)
        ax.set_xlim(0, len(input_sentence) - 1)

        ax.set_xlabel('Head {}'.format(head + 1))

    plt.tight_layout()
    plt.show()

if __name__ == '__main__':
    tf.compat.v1.app.run()

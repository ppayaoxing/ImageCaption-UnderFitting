# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Input ops."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


import tensorflow as tf
FLAGS = tf.flags.FLAGS

from .image_processing import simple_process_image


def parse_sequence_example(serialized, image_feature, caption_feature, flip_caption_feature=None):
  """Parses a tensorflow.SequenceExample into an image and caption.

  Args:
    serialized: A scalar string Tensor; a single serialized SequenceExample.
    image_feature: Name of SequenceExample context feature containing image
      data.
    caption_feature: Name of SequenceExample feature list containing integer
      captions.

  Returns:
    encoded_image: A scalar string Tensor containing a JPEG encoded image.
    caption: A 1-D uint64 Tensor with dynamically specified length.
  """
  if not flip_caption_feature:
    flip_caption = None
    context, sequence = tf.parse_single_sequence_example(
        serialized,
        context_features={
            image_feature: tf.FixedLenFeature([], dtype=tf.string)
        },
        sequence_features={
            caption_feature: tf.FixedLenSequenceFeature([], dtype=tf.int64),
        })

    encoded_image = context[image_feature]
    caption = sequence[caption_feature]
  else:
    context, sequence = tf.parse_single_sequence_example(
        serialized,
        context_features={
            image_feature: tf.FixedLenFeature([], dtype=tf.string)
        },
        sequence_features={
            caption_feature: tf.FixedLenSequenceFeature([], dtype=tf.int64),
            flip_caption_feature: tf.FixedLenSequenceFeature([], dtype=tf.int64)
        })
    encoded_image = context[image_feature]
    caption = sequence[caption_feature]
    flip_caption = sequence[flip_caption_feature]

  return encoded_image, caption, flip_caption


def prefetch_input_data(reader,
                        file_pattern,
                        is_training,
                        batch_size,
                        values_per_shard,
                        input_queue_capacity_factor=16,
                        num_reader_threads=1,
                        shard_queue_name="filename_queue",
                        value_queue_name="input_queue"):
  """Prefetches string values from disk into an input queue.

  In training the capacity of the queue is important because a larger queue
  means better mixing of training examples between shards. The minimum number of
  values kept in the queue is values_per_shard * input_queue_capacity_factor,
  where input_queue_memory factor should be chosen to trade-off better mixing
  with memory usage.

  Args:
    reader: Instance of tf.ReaderBase.
    file_pattern: Comma-separated list of file patterns (e.g.
        /tmp/train_data-?????-of-00100).
    is_training: Boolean; whether prefetching for training or eval.
    batch_size: Model batch size used to determine queue capacity.
    values_per_shard: Approximate number of values per shard.
    input_queue_capacity_factor: Minimum number of values to keep in the queue
      in multiples of values_per_shard. See comments above.
    num_reader_threads: Number of reader threads to fill the queue.
    shard_queue_name: Name for the shards filename queue.
    value_queue_name: Name for the values input queue.

  Returns:
    A Queue containing prefetched string values.
  """
  data_files = []
  for pattern in file_pattern.split(","):
    data_files.extend(tf.gfile.Glob(pattern))
  if not data_files:
    tf.logging.fatal("Found no input files matching %s", file_pattern)
  else:
    tf.logging.info("Prefetching values from %d files matching %s",
                    len(data_files), file_pattern)

  if is_training:
    filename_queue = tf.train.string_input_producer(
        data_files, shuffle=True, capacity=16, name=shard_queue_name)
    min_queue_examples = values_per_shard * input_queue_capacity_factor
    capacity = min_queue_examples + 100 * batch_size
    values_queue = tf.RandomShuffleQueue(
        capacity=capacity,
        min_after_dequeue=min_queue_examples,
        dtypes=[tf.string],
        name="random_" + value_queue_name)
  else:
    filename_queue = tf.train.string_input_producer(
        data_files, shuffle=False, capacity=1, name=shard_queue_name)
    capacity = values_per_shard + 3 * batch_size
    values_queue = tf.FIFOQueue(
        capacity=capacity, dtypes=[tf.string], name="fifo_" + value_queue_name)

  enqueue_ops = []
  for _ in range(num_reader_threads):
    _, value = reader.read(filename_queue)
    enqueue_ops.append(values_queue.enqueue([value]))
  tf.train.queue_runner.add_queue_runner(tf.train.queue_runner.QueueRunner(
      values_queue, enqueue_ops))
  tf.summary.scalar(
      "queue/%s/fraction_of_%d_full" % (values_queue.name, capacity),
      tf.cast(values_queue.size(), tf.float32) * (1. / capacity))

  return values_queue


def batch_with_dynamic_pad(images_and_captions,
                           batch_size,
                           queue_capacity,
                           add_summaries=True):
  """Batches input images and captions.

  This function splits the caption into an input sequence and a target sequence,
  where the target sequence is the input sequence right-shifted by 1. Input and
  target sequences are batched and padded up to the maximum length of sequences
  in the batch. A mask is created to distinguish real words from padding words.

  Example:
    Actual captions in the batch ('-' denotes padded character):
      [
        [ 1 2 3 4 5 ],
        [ 1 2 3 4 - ],
        [ 1 2 3 - - ],
      ]

    input_seqs:
      [
        [ 1 2 3 4 ],
        [ 1 2 3 - ],
        [ 1 2 - - ],
      ]

    target_seqs:
      [
        [ 2 3 4 5 ],
        [ 2 3 4 - ],
        [ 2 3 - - ],
      ]

    mask:
      [
        [ 1 1 1 1 ],
        [ 1 1 1 0 ],
        [ 1 1 0 0 ],
      ]

  Args:
    images_and_captions: A list of pairs [image, caption], where image is a
      Tensor of shape [height, width, channels] and caption is a 1-D Tensor of
      any length. Each pair will be processed and added to the queue in a
      separate thread.
    batch_size: Batch size.
    queue_capacity: Queue capacity.
    add_summaries: If true, add caption length summaries.

  Returns:
    images: A Tensor of shape [batch_size, height, width, channels].
    input_seqs: An int32 Tensor of shape [batch_size, padded_length].
    target_seqs: An int32 Tensor of shape [batch_size, padded_length].
    mask: An int32 0/1 Tensor of shape [batch_size, padded_length].
  """
  enqueue_list = []
  for image, caption in images_and_captions:
    caption_length = tf.shape(caption)[0]
    input_length = tf.expand_dims(tf.subtract(caption_length, 1), 0)

    input_seq = tf.slice(caption, [0], input_length)
    target_seq = tf.slice(caption, [1], input_length)
    indicator = tf.ones(input_length, dtype=tf.int32)
    enqueue_list.append([image, input_seq, target_seq, indicator])

  images, input_seqs, target_seqs, mask = tf.train.batch_join(
      enqueue_list,
      batch_size=batch_size,
      capacity=queue_capacity,
      dynamic_pad=True,
      name="batch_and_pad")

  if add_summaries:
    lengths = tf.add(tf.reduce_sum(mask, 1), 1)
    tf.summary.scalar("caption_length/batch_min", tf.reduce_min(lengths))
    tf.summary.scalar("caption_length/batch_max", tf.reduce_max(lengths))
    tf.summary.scalar("caption_length/batch_mean", tf.reduce_mean(lengths))
  
  return images, input_seqs, target_seqs, mask

def caption_to_attributes_target(caption, mask):
  unique_ids, _ = tf.unique(caption)
  attributes_target = tf.reduce_sum(tf.one_hot(unique_ids, tf.shape(mask)[0]), axis=0) * mask
  return attributes_target

def get_attributes_target(target_seq, mask):
  return tf.map_fn(lambda x: caption_to_attributes_target(x, mask), target_seq, dtype=tf.float32)

def caption_to_multi_labels(captions):
  print("captions", captions)
  def c2ml(caption):
    unique_ids, _ = tf.unique(caption)
    label = tf.reduce_sum(tf.one_hot(unique_ids, FLAGS.vocab_size), axis=0)
    return label
  labels = tf.map_fn(lambda x: c2ml(x), captions, dtype=tf.float32)
  print("labels", labels)
  return labels

def get_images_and_captions(is_training):
  # Prefetch serialized SequenceExample protos.
  input_queue = prefetch_input_data(
      tf.TFRecordReader(),
      FLAGS.input_file_pattern,
      is_training=is_training,
      batch_size=FLAGS.batch_size,
      values_per_shard=FLAGS.values_per_input_shard,
      input_queue_capacity_factor=FLAGS.input_queue_capacity_factor,
      num_reader_threads=FLAGS.num_input_reader_threads)

  # Image processing and random distortion. Split across multiple threads
  # with each thread applying a slightly different distortion.
  assert FLAGS.num_preprocess_threads % 2 == 0
  images_and_captions = []
  for thread_id in range(FLAGS.num_preprocess_threads):
    serialized_sequence_example = input_queue.dequeue()
    if FLAGS.support_flip:
      encoded_image, caption, flip_caption = parse_sequence_example(
          serialized_sequence_example,
          image_feature=FLAGS.image_feature_name,
          caption_feature=FLAGS.caption_feature_name,
          flip_caption_feature=FLAGS.flip_caption_feature_name)
      # random decides flip or not
      flip_image = simple_process_image(encoded_image, thread_id=thread_id, flip=True, is_training=is_training)
      image = simple_process_image(encoded_image, thread_id=thread_id, flip=False, is_training=is_training)
      maybe_flip_image, maybe_flip_caption = tf.cond(
                          tf.less(tf.random_uniform([],0,1.0), 0.5), 
                          lambda: [flip_image, flip_caption], 
                          lambda: [image, caption])
      images_and_captions.append([maybe_flip_image, maybe_flip_caption])
    else:
      encoded_image, caption, _ = parse_sequence_example(
          serialized_sequence_example,
          image_feature=FLAGS.image_feature_name,
          caption_feature=FLAGS.caption_feature_name)
      image = simple_process_image(encoded_image, thread_id=thread_id, flip=False, is_training=is_training)
      images_and_captions.append([image, caption])

  # Batch inputs.
  queue_capacity = (2 * FLAGS.num_preprocess_threads *
                    FLAGS.batch_size)
  images, input_seqs, target_seqs, input_mask = (
      batch_with_dynamic_pad(images_and_captions,
                                       batch_size=FLAGS.batch_size,
                                       queue_capacity=queue_capacity))
  return images, input_seqs, target_seqs, input_mask

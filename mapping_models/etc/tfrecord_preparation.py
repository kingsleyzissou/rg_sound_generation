import tensorflow as tf
import numpy as np
import pydub
import os
import json
import gin
import time
from mapping_models.data_providers import PartialTFRecordProvider
from ddsp import spectral_ops
import ddsp.training.models as models
import ddsp.training.trainers as trainers
import ddsp.training.train_util as train_util


# ------------------------------------------------------------------------------

def _load_audio(audio_path, sample_rate):
    with tf.io.gfile.GFile(audio_path, 'rb') as f:
        # Load audio at original SR
        audio_segment = (pydub.AudioSegment.from_file(f).set_channels(1))
        # Compute expected length at given `sample_rate`
        expected_len = int(audio_segment.duration_seconds * sample_rate)
        # Resample to `sample_rate`
        audio_segment = audio_segment.set_frame_rate(sample_rate)
        sample_arr = audio_segment.get_array_of_samples()
        audio = np.array(sample_arr).astype(np.float32)
        # Zero pad missing samples, if any
        audio = spectral_ops.pad_or_trim_to_expected_length(audio, expected_len)
    # Convert from int to float representation.
    audio /= np.iinfo(sample_arr.typecode).max
    return audio


def _byte_feature(value):
    return tf.train.Feature(
        bytes_list=tf.train.BytesList(value=value))


def _float_feature(value):
    return tf.train.Feature(
        float_list=tf.train.FloatList(value=value))


def _int64_feature(value):
    return tf.train.Feature(
        int64_list=tf.train.Int64List(value=value))


# ------------------------------------------------------------------------------

def prepare_partial_tfrecord(
        dataset_dir='nsynth_guitar',
        split='train',
        sample_rate=16000,
        frame_rate=250
):
    split_dir = os.path.join(dataset_dir, split)
    audio_dir = os.path.join(split_dir, 'audio')
    nsynth_dataset_file = os.path.join(split_dir, 'examples.json')
    partial_tfrecord_file = os.path.join(split_dir, 'partial.tfrecord')

    with open(nsynth_dataset_file, 'r') as file:
        nsynth_dataset_dict = json.load(file)

    steps = len(nsynth_dataset_dict)

    with tf.io.TFRecordWriter(partial_tfrecord_file) as writer:
        for step, (k, v) in enumerate(nsynth_dataset_dict.items()):
            start_time = time.perf_counter()

            file_name = '{}.wav'.format(k)
            target_path = os.path.join(audio_dir, file_name)

            audio = _load_audio(target_path, sample_rate)
            f0_hz, f0_confidence = spectral_ops.compute_f0(
                audio, sample_rate, frame_rate)
            mean_loudness_db = spectral_ops.compute_loudness(
                audio, sample_rate, frame_rate, 2048)

            audio = audio.astype(np.float32)
            f0_hz = f0_hz.astype(np.float32)
            f0_confidence = f0_confidence.astype(np.float32)
            mean_loudness_db = mean_loudness_db.astype(np.float32)

            # pitch_hz = core.midi_to_hz(v['pitch'])

            partial_dataset_dict = {
                'sample_name': _byte_feature([str.encode(k)]),
                'note_number': _int64_feature([v['pitch']]),
                'velocity': _int64_feature([v['velocity']]),
                'instrument_source': _int64_feature([v['instrument_source']]),
                'qualities': _int64_feature(v['qualities']),
                'audio': _float_feature(audio),
                'f0_hz': _float_feature(f0_hz),
                'f0_confidence': _float_feature(f0_confidence),
                'loudness_db': _float_feature(mean_loudness_db),
            }

            tf_example = tf.train.Example(
                features=tf.train.Features(feature=partial_dataset_dict))

            writer.write(tf_example.SerializeToString())

            stop_time = time.perf_counter()
            elapsed_time = stop_time - start_time
            print('{}/{} - sample_name: {} - elapsed_time: {:.3f}'.format(
                step+1, steps, k, elapsed_time))


# ------------------------------------------------------------------------------

def prepare_complete_tfrecord(dataset_dir='nsynth_guitar',
                              split='train',
                              sample_rate=16000,
                              frame_rate=250):
    split_dir = os.path.join(dataset_dir, split)
    operative_config_file = train_util.get_latest_operative_config(split_dir)
    partial_tfrecord_file = os.path.join(split_dir, 'partial.tfrecord')
    complete_tfrecord_file = os.path.join(split_dir, 'complete.tfrecord')

    with gin.unlock_config():
        if tf.io.gfile.exists(operative_config_file):
            gin.parse_config_file(operative_config_file, skip_unknown=True)

    data_provider = PartialTFRecordProvider(
        file_pattern=partial_tfrecord_file + '*',
        example_secs=4,
        sample_rate=sample_rate,
        frame_rate=frame_rate)

    dataset = data_provider.get_batch(1, shuffle=False, repeats=1)

    strategy = train_util.get_strategy()
    with strategy.scope():
        model = models.get_model()
        trainer = trainers.Trainer(model, strategy)
        trainer.restore(split_dir)

        # steps = dataset.cardinality()

        with tf.io.TFRecordWriter(complete_tfrecord_file) as writer:
            for step, e in enumerate(dataset):
                start_time = time.perf_counter()

                sample_name = e['sample_name'][0].numpy()
                note_number = e['note_number'][0].numpy()
                velocity = e['velocity'][0].numpy()
                instrument_source = e['instrument_source'][0].numpy()
                qualities = e['qualities'][0].numpy()
                audio = e['audio'][0].numpy()
                f0_hz = e['f0_hz'][0].numpy()
                f0_confidence = e['f0_confidence'][0].numpy()
                loudness_db = e['loudness_db'][0].numpy()

                complete_dataset_dict = {
                    'sample_name': _byte_feature(sample_name),
                    'note_number': _int64_feature(note_number),
                    'velocity': _int64_feature(velocity),
                    'instrument_source': _int64_feature(instrument_source),
                    'qualities': _int64_feature(qualities),
                    'audio': _float_feature(audio),
                    'f0_hz': _float_feature(f0_hz),
                    'f0_confidence': _float_feature(f0_confidence),
                    'loudness_db': _float_feature(loudness_db),
                }

                e = model.encode(e, training=False)

                f0_scaled = tf.squeeze(e['f0_scaled']).numpy()
                ld_scaled = tf.squeeze(e['ld_scaled']).numpy()
                z = tf.reshape(e['z'], shape=(-1)).numpy()

                complete_dataset_dict.update({
                    'f0_scaled': _float_feature(f0_scaled),
                    'ld_scaled': _float_feature(ld_scaled),
                    'z': _float_feature(z),
                })

                tf_example = tf.train.Example(
                    features=tf.train.Features(feature=complete_dataset_dict))

                writer.write(tf_example.SerializeToString())

                stop_time = time.perf_counter()
                elapsed_time = stop_time - start_time
                print('{} - sample_name: {} - elapsed_time: {:.3f}'.format(
                    step+1, e['sample_name'], elapsed_time))


# ------------------------------------------------------------------------------

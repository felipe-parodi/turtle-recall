#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

print('Import')

import os
import sys
sys.path.append('../../lib')
import glob
import math
from datetime import datetime as dt
import warnings
warnings.simplefilter('ignore', UserWarning)
import numpy as np
np.set_printoptions(suppress=True)
import pandas as pd
import tensorflow as tf
print('tf:', tf.__version__)
import tensorflow_addons as tfa
print('tfa:', tfa.__version__)
from sklearn.metrics import accuracy_score
from keras_cv_attention_models import beit

from vecxoz_utils import init_tpu
from vecxoz_utils import init_tfdata
from vecxoz_utils import KeepLastCKPT
from vecxoz_utils import compute_cv_scores
from vecxoz_utils import create_submission
from vecxoz_utils import ArgumentParserExtended

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

parser = ArgumentParserExtended()

parser.add_str('--data_tfrec_dir',    default='../../data/tfrec', help='Data directory containig TFRecord files')
parser.add_str('--data_dir',          default='../../data',  help='Data directory containig CSV files')
parser.add_str('--data_preds_dir',    default='preds',       help='Directory where predictions will be saved')
parser.add_str('--tpu_ip_or_name',    default=None,          help='TPU name or GRPC address e.g. node-1 or grpc://10.70.50.202:8470 or None')
parser.add_str('--mixed_precision',   default=None,          help='Mixed precision. E.g.: mixed_float16, mixed_bfloat16, or None')
parser.add_str('--job',               default='train_val_test_score_subm', help='Job to perform. A combination of words: train, val, test, score, subm. E.g. train_val_test. Use skip instead train to skip training in the first fold')
parser.add_str('--metric_name',       default='acc',         help='Metric name')
parser.add_str('--monitor',           default='val_loss',    help='Value to monitor during training to apply checkpoint callback, etc.')
parser.add_int('--n_folds',           default=5,             help='Number of folds')
parser.add_int('--initial_fold',      default=0,             help='Initial fold (from 0)')
parser.add_int('--final_fold',        default=None,          help='Final fold (from 1). None means all folds i.e. equal to n_folds')
parser.add_int('--n_channels',        default=3,             help='Number of image channels')
parser.add_int('--auto',              default=-1,            help='Constant value of tf.data.experimental.AUTOTUNE. It is used to manage number of parallel calls, etc.')
parser.add_int('--dim',               default=512,           help='Image size in pixels. This final size which will be passed to a model')
parser.add_int('--dim_pre',           default=512,           help='Image size in pixels before augmentation. This may differ form final size passed to a model')
parser.add_bool('--image_inverted',   default=False,         help='Whether to invert the image i.e. 255 - image')
parser.add_int('--n_examples_total',  default=12803,         help='Number of training examples. This value is used to define an epoch')
parser.add_int('--n_epochs',          default=200,           help='Number of epochs to train')
parser.add_int('--batch_size',        default=32,            help='Batch size')
parser.add_bool('--drop_remainder',   default=False,         help='Whether to drop remainder')
parser.add_float('--lr',              default=5e-5,          help='Learning rate')
parser.add_str('--weights',           default='imagenet21k-ft1k', help='Name of pretrained weights. E.g.: imagenet, noisy-student, imagenet21k+imagenet2012, imagenet21k, or None')
parser.add_float('--aug_percentage',  default=0.5,           help='Probablity of outputting augmented image regardless of total number of augmentations')
parser.add_int('--aug_number',        default=9,             help='Number of train-time augmentations. 0 means no augmentation')
parser.add_int('--tta_number',        default=0,             help='Number of test-time augmentations. 0 means no augmentation. In result there will be (tta_number + 1) predictions')
parser.add_int('--n_classes',         default=2265,          help='Number of classes for classification task. For regression task must be 1')
parser.add_int('--buffer_size',       default=256,           help='Shuffle buffer size for tf.data. For small RAM or large data use small buffer e.g. 128 or None to disable')

args = parser.parse_args()

# Date-time
args.date_time = dt.now().strftime('%Y%m%d-%H%M%S-%f')
# Interim value for random number generator used with aug
args.aug_maxval = round(args.aug_number / args.aug_percentage)
# Wildcard for test TFRecord file
args.data_tfrec_test = os.path.join(args.data_tfrec_dir, 'test.tfrecord*')
# Model function
# args.model_func = efn.EfficientNetB7
# Number of sub-train examples i.e. all folds except one (e.g. 4/5 of full train)
args.n_examples_train = args.n_examples_total - (args.n_examples_total // args.n_folds)
# Folds
if args.final_fold is None:
    args.final_fold = args.n_folds
assert args.initial_fold <= args.n_folds - 1, 'Incorrect initial_fold'
assert args.final_fold <= args.n_folds and args.final_fold > args.initial_fold, 'Incorrect final_fold'
# Create dirs
os.makedirs(args.data_preds_dir, exist_ok=True)

print('Settings:')
print(parser.args_repr(args, False))

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------
#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

feature_description = {
    'image':    tf.io.FixedLenFeature([], tf.string),
    'label':    tf.io.FixedLenFeature([], tf.int64),
}


def parse_example(example_proto):
    """
    Returns image in uint8 because some processing/augmentation functions
    require this dtype (e.g. sobel).
    """
    #
    d = tf.io.parse_single_example(example_proto, feature_description)
    image = tf.image.decode_jpeg(d['image'], channels=args.n_channels, dct_method='INTEGER_ACCURATE')
    #
    image = tf.image.resize(image, [args.dim_pre, args.dim_pre])
    image = tf.reshape(image, [args.dim_pre, args.dim_pre, args.n_channels])
    #
    image = tf.cast(image, tf.uint8)
    label = tf.cast(d['label'], tf.int32)
    #
    # (512, 512, 3), uint8, [0, 255]
    return image, label


def aug_0(image, label):
    return image, label
def aug_1(image, label):
    return tf.image.flip_left_right(image), label
def aug_2(image, label):
    return tf.image.flip_up_down(image), label
def aug_3(image, label):
    return tfa.image.rotate(image, math.pi/4*2), label #  90
def aug_4(image, label):
    return tfa.image.rotate(image, math.pi/4*4), label # 180
def aug_5(image, label):
    return tfa.image.rotate(image, math.pi/4*6), label # 270
def aug_6(image, label):
    return tf.image.central_crop(tfa.image.rotate(image, math.pi/4*1), 0.7), label #  45
def aug_7(image, label):
    return tf.image.central_crop(tfa.image.rotate(image, math.pi/4*3), 0.7), label # 135
def aug_8(image, label):
    return tf.image.central_crop(tfa.image.rotate(image, math.pi/4*5), 0.7), label # 225
def aug_9(image, label):
    return tf.image.central_crop(tfa.image.rotate(image, math.pi/4*7), 0.7), label # 315


# args.tta_number = 0 means NO tta i.e. tta_func_list = [aug_0]
tta_func_list = [aug_0, aug_1, aug_2, aug_3, aug_4, aug_5, aug_6, aug_7, aug_8, aug_9][:args.tta_number + 1]


def aug(image, label):
    """
    Runs single transformation per call with `aug_percentage` probability
    Specific transformation probability is `aug_percentage` / `aug_number`
    """
    #
    if args.aug_number != 0:
        aug_id = tf.random.uniform([], minval=0, maxval=args.aug_maxval, dtype=tf.int32)
    #
    if args.aug_number == 0:
        pass
    elif aug_id == 0:
        image, label = aug_1(image, label)
    elif aug_id == 1:
        image, label = aug_2(image, label)
    elif aug_id == 2:
        image, label = aug_3(image, label)
    elif aug_id == 3:
        image, label = aug_4(image, label)
    elif aug_id == 4:
        image, label = aug_5(image, label)
    elif aug_id == 5:
        image, label = aug_6(image, label)
    elif aug_id == 6:
        image, label = aug_7(image, label)
    elif aug_id == 7:
        image, label = aug_8(image, label)
    elif aug_id == 8:
        image, label = aug_9(image, label)
    #
    # (512, 512, 3), uint8, [0, 255]
    return image, label


def norm(image, label):
    #
    image = tf.image.resize(image, [args.dim, args.dim])
    image = tf.reshape(image, [args.dim, args.dim, args.n_channels])
    #
    image = tf.cast(image, tf.float32)
    label = tf.cast(label, tf.int32)
    #
    if args.image_inverted:
        image = 255.0 - image
    #
    image = image / 255.0
    #
    # (512, 512, 3), float32, about [-1, +1] or [0, 1]
    return image, label

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

def init_model(print_summary=True):
    model = beit.BeitLargePatch16(input_shape=(args.dim, args.dim, args.n_channels), pretrained=args.weights, num_classes=args.n_classes)
    model.compile(optimizer=tf.keras.optimizers.Adam(args.lr), 
                  loss='sparse_categorical_crossentropy',
                  metrics=['acc'])
    if print_summary:
        model.summary()
    return model

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------

if args.job != 'score' and args.job != 'subm' and args.job != 'score_subm':
    for fold_id in range(args.initial_fold, args.final_fold):
        print('\n*****')
        print('Fold:', fold_id)
        print('*****\n')
        #--------------------------------------------------------------------------
        print('Clear session...')
        tf.keras.backend.clear_session()
        #--------------------------------------------------------------------------
        if args.mixed_precision is not None:
            print('Init Mixed Precision:', args.mixed_precision)
            policy = tf.keras.mixed_precision.experimental.Policy(args.mixed_precision)
            tf.keras.mixed_precision.experimental.set_policy(policy)
        else:
            print('Using default PRECISION:', tf.keras.backend.floatx())
        #--------------------------------------------------------------------------
        print('FULL BATCH SHAPE: %d x %d x %d x %d' % (args.batch_size,
                                                       args.dim,
                                                       args.dim,
                                                       args.n_channels))
        #--------------------------------------------------------------------------
        # Init TPU
        print('Init TPU')
        tpu, topology, strategy = init_tpu(args.tpu_ip_or_name)
        #--------------------------------------------------------------------------
        # Globs
        all_fold_ids = np.array(range(args.n_folds))
        train_fold_ids = all_fold_ids[all_fold_ids != fold_id]
        train_glob = os.path.join(args.data_tfrec_dir, ('fold.[' + '%d'*(args.n_folds-1) + '].tfrecord*') % tuple(train_fold_ids))
        val_glob   = os.path.join(args.data_tfrec_dir, 'fold.[%d].tfrecord*' % fold_id)
        print('TRAIN GLOB:', train_glob)
        print('VAL   GLOB:', val_glob)
        print('TEST  GLOB:', args.data_tfrec_test)
        #--------------------------------------------------------------------------
        print('Init datasets')
        train_ds = init_tfdata(train_glob, 
                               deterministic=False,  
                               batch_size=args.batch_size, 
                               auto=args.auto,
                               parse_example=parse_example, 
                               aug=aug, 
                               norm=norm,
                               repeat=True,
                               buffer_size=args.buffer_size, 
                               drop_remainder=args.drop_remainder,
                               cache=False)
        val_ds = init_tfdata(val_glob, 
                             deterministic=True,  
                             batch_size=args.batch_size, 
                             auto=args.auto,
                             parse_example=parse_example,
                             norm=norm,
                             repeat=False,  
                             buffer_size=None,
                             drop_remainder=args.drop_remainder,
                             cache=True)
        #--------------------------------------------------------------------------
        print('Init model')
        with strategy.scope():
            model = init_model(print_summary=True)
        #--------------------------------------------------------------------------
        print('Init callbacks')
        call_ckpt = tf.keras.callbacks.ModelCheckpoint('model-f%d-e{epoch:03d}-{val_loss:.4f}-{val_%s:.4f}.h5' % (fold_id, args.metric_name),
                                                       monitor=args.monitor, # do not use if no val set
                                                       save_best_only=True, # set False if no val set
                                                       save_weights_only=True,
                                                       mode='auto',
                                                       verbose=1)
        call_reduce_lr = tf.keras.callbacks.ReduceLROnPlateau(monitor=args.monitor, 
                                                              factor=0.5, 
                                                              patience=4, 
                                                              min_delta=1e-4,
                                                              min_lr=1e-8,
                                                              verbose=1,
                                                              mode='auto')
        call_early_stop = tf.keras.callbacks.EarlyStopping(monitor=args.monitor,
                                                           patience=8,
                                                           min_delta=1e-4,
                                                           mode='auto',
                                                           verbose=1)
        call_keep_last = KeepLastCKPT(wildcard='model-f%d-e*.h5' % fold_id)
        #-------------------------------------------------------------------------- 
        if 'train' in args.job:
            print('Fit (fold %d)' % fold_id)
            h = model.fit(
                train_ds,
                # batch_size=args.batch_size, # just not to forget
                steps_per_epoch=args.n_examples_train // args.batch_size,
                epochs=args.n_epochs,
                validation_data=val_ds,
                callbacks=[call_ckpt,
                           call_reduce_lr,
                           call_early_stop,
                           call_keep_last]
            )
        #--------------------------------------------------------------------------
        # 'skip' in args.job means to skip training for the first fold only so we will train in all next folds
        args.job = args.job.replace('skip', 'train') 
        #--------------------------------------------------------------------------
        # Load best model for fold
        m = sorted(glob.glob('model-f%d*.h5' % fold_id))[-1]
        print('Load model (fold %d): %s' % (fold_id, m))
        model.load_weights(m)
        #--------------------------------------------------------------------------
        # TTA
        #--------------------------------------------------------------------------
        for tta_id in range(len(tta_func_list)):
            # Create VAL and TEST datasets with TTA transforms corresponding to AUG transforms seen during training
            print('Init datasets for prediction (fold %d, tta %d)' % (fold_id, tta_id))
            val_ds = init_tfdata(val_glob, 
                                 deterministic=True,  
                                 batch_size=args.batch_size, 
                                 auto=args.auto,
                                 parse_example=parse_example,
                                 tta=tta_func_list[tta_id],
                                 norm=norm,
                                 repeat=False,  
                                 buffer_size=None,
                                 drop_remainder=args.drop_remainder,
                                 cache=True)

            test_ds = init_tfdata(args.data_tfrec_test, 
                                  deterministic=True,  
                                  batch_size=args.batch_size, 
                                  auto=args.auto,
                                  parse_example=parse_example,
                                  tta=tta_func_list[tta_id],
                                  norm=norm,
                                  repeat=False,  
                                  buffer_size=None,
                                  drop_remainder=args.drop_remainder,
                                  cache=True)
            #--------------------------------------------------------------------------
            # Predict val
            if 'val' in args.job:
                print('Predict VAL (fold %d, tta %d)' % (fold_id, tta_id))
                y_pred_val = model.predict(val_ds, verbose=1)
                np.save(os.path.join(args.data_preds_dir, 'y_pred_val_fold_%d_tta_%d.npy' % (fold_id, tta_id)), y_pred_val)
            #--------------------------------------------------------------------------
            # Predict test
            if 'test' in args.job:
                print('Predict TEST (fold %d, tta %d)' % (fold_id, tta_id))
                y_pred_test = model.predict(test_ds, verbose=1)
                np.save(os.path.join(args.data_preds_dir, 'y_pred_test_fold_%d_tta_%d.npy' % (fold_id, tta_id)), y_pred_test)
            #--------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Compute val scores
#------------------------------------------------------------------------------

if 'val' in args.job or 'score' in args.job:
    print('VAL scores')
    _ = compute_cv_scores(args.data_dir, args.data_preds_dir, args.n_folds, args.tta_number, print_scores=True)

#------------------------------------------------------------------------------
# Create submission
#------------------------------------------------------------------------------

if 'test' in args.job or 'subm' in args.job:
    print('Create submission CSV')
    written_file_name = create_submission(args.data_dir, args.data_preds_dir, args.n_folds, args.tta_number)
    print('Submission was saved to:', written_file_name)

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------
#------------------------------------------------------------------------------
#------------------------------------------------------------------------------




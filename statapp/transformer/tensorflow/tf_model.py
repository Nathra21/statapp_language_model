import sys
import os
import json
import datetime
from pprint import pprint
import pdb

import numpy as np
import pandas as pd
from tqdm import tqdm
from sklearn.model_selection import train_test_split
import tensorflow as tf
import tensorflow_datasets as tfds
from tensorflow.keras.layers import Dense, Embedding, LayerNormalization, TimeDistributed
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint

# from tensorflow.keras.mixed_precision import experimental as mixed_precision
# policy = mixed_precision.Policy('mixed_float32')
# mixed_precision.set_policy(policy)


# this_file_dir = os.path.dirname(__file__)
# sys.path.append(os.path.dirname(this_file_dir))
import statapp
from statapp.transformer.common import get_positional_encodings
from statapp.transformer.tensorflow.sampling import generate_sample_with_transformer, predict_probas_with_transformer, get_max_model_outputs
from statapp.common.preprocessing import load_data, encode_data, split_into_X_y
from statapp.common.utils import NumpyEncoder, add_to_log, pad_or_cut

data_path = "data/fr.train.top1M.txt"

hparams = {
    "max_seq_length": 1024,
    "num_blocks": 3,
    "d_model": 512,
    "ff_hidden_size": 512,
    "num_heads": 16,
    "target_vocab_size": 2000,
    "epochs": 500,
    "batch_size": 128,
    "learning_rate": 1e-3,
}

def scaled_dot_product_attention(q, k, v, mask=False):
    """Perform scaled dot-product attention on input tensors.
    Only operates on the last two dimensions of input tensors.
    
    Args:
        q (tf.Tensor): Query tensor of shape (..., seq_length, d_q)
        k (tf.Tensor): Key tensor of shape (..., seq_length, d_q)
        v (tf.Tensor): Value tensor of shape (..., seq_length, d_v)
    
    Returns:
        tf.Tensor of shape (..., seq_length, d_v)
    """
    assert q.shape[-1] == k.shape[-1]
    assert q.shape[-2] == k.shape[-2]
    assert k.shape[-2] == v.shape[-2]
    
    if mask:
        mask_matrix = generate_mask_matrix(tf.shape(q)[-2])
    else:
        mask_matrix = tf.zeros((tf.shape(q)[-2], tf.shape(q)[-2]))
    
    mask_matrix = tf.cast(mask_matrix, tf.float32)
    
    dimension = tf.cast(tf.shape(q)[-1], dtype=tf.float32)
    
    scores = tf.matmul(q, k, transpose_b=True) # (..., seq_length, seq_length)
    scores = scores / tf.math.sqrt(dimension) # (..., seq_length, seq_length)
    scores = scores + np.finfo(np.float32).min * mask_matrix
    scores = tf.nn.softmax(scores, axis=-1) # (..., seq_length, seq_length)

    out = tf.matmul(scores, v) # (..., seq_length, d_v)
    
    return out


def test_sdpa():
    """Test the scaled dot-product attention function.
    
    Returns:
        tf.Tensor of value:
            [[  1. ,   0. ],
             [  5.5,   0. ],
             [100. ,   5. ],
             [550. ,   5.5]]
    """
    np.set_printoptions(suppress=True)

    k = tf.constant(
        [[[
            [10, 0, 0],
            [0, 10, 0],
            [0, 0, 10],
            [0, 0, 10],
        ]]],
        dtype=tf.float32
    )

    v = tf.constant(
        [[[
            [1, 0],
            [10, 0],
            [100, 5],
            [1000, 6],
        ]]],
        dtype=tf.float32
    )

    q = tf.constant(
        [[[
            [0, 0, 10],
            [0, 0, 10],
            [0, 0, 10],
            [0, 0, 10],
        ]]],
        dtype=tf.float32
    )
    
    att = scaled_dot_product_attention(q, k, v, mask=True)
    
    return att


def generate_mask_matrix(seq_length):
    """Create a mask matrix to keep the model from attending to a token it must predict or to those that follow it.
    Simply an upper-triangular matrix filled with ones (with zeros on the diagonal).
    """
    shape = (seq_length, seq_length)
    mask = 1 - tf.linalg.band_part(
        tf.ones(shape),
        -1,
        0
    )
    return mask


class MultiHeadAttention(tf.keras.Model):
    def __init__(self, dim, num_heads):
        super(MultiHeadAttention, self).__init__()
        
        assert dim % num_heads == 0, "Dim is not divisible by num_heads"
        self.dim = dim
        self.num_heads = num_heads
        self.depth = self.dim // self.num_heads
        
        self.dense_Q = Dense(self.dim, name="Q", dtype=tf.float32)
        self.dense_K = Dense(self.dim, name="K", dtype=tf.float32)
        self.dense_V = Dense(self.dim, name="V", dtype=tf.float32)
    
    def reshape_dense_output(self, x):
        """Reshape output from dense_{Q,K,V} by splitting the last dimension
        into heads, and reordering dimensions so that seq_length is
        second_to_last (for sending into SDPA).
        
        Args:
            x: tf.Tensor of shape (batch_size, seq_length, self.dim)
        
        Returns:
            tf.Tensor of shape (batch_size, self.num_heads, seq_length, self.depth)
        """
        x = tf.reshape(x, (tf.shape(x)[0], tf.shape(x)[1], self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, x):
        assert len(x.shape) == 3, "Input should be of shape (batch_size, seq_length, d), not {}".format(x.shape)
        
        q = self.reshape_dense_output(self.dense_Q(x)) # (batch_size, num_heads, seq_length, depth)
        k = self.reshape_dense_output(self.dense_K(x)) # (batch_size, num_heads, seq_length, depth)
        v = self.reshape_dense_output(self.dense_V(x)) # (batch_size, num_heads, seq_length, depth)
        
        att = scaled_dot_product_attention(q, k, v, True) # (batch_size, num_heads, seq_length, depth)
        att = tf.transpose(att, perm=[0, 2, 1, 3]) # (batch_size, seq_length, num_heads, depth)
        att = tf.reshape(att, (tf.shape(att)[0], tf.shape(att)[1], -1)) # (batch_size, seq_length, dim)
        
        return att


class EncoderBlock(tf.keras.Model):
    def __init__(self, dim, ff_hidden_size, num_heads):
        super(EncoderBlock, self).__init__()
        self.dim = dim
        self.mha = MultiHeadAttention(dim, num_heads)
        self.ff1 = Dense(ff_hidden_size, activation="relu")
        self.ff2 = Dense(dim, activation="relu")
        
        self.norm_after_mha = LayerNormalization(dtype=tf.float32)
        self.norm_after_ff = LayerNormalization(dtype=tf.float32)
    
    def call(self, x):
        mha_output = self.mha(x)
        mha_output = self.norm_after_mha(x + mha_output)
        
        ff_output = self.ff2(self.ff1(mha_output))
        ff_output = self.norm_after_ff(mha_output + ff_output)
        
        return ff_output


class Transformer(tf.keras.Model):
    def __init__(self, d_model, ff_hidden_size, num_blocks, num_heads, vocab_size, max_seq_length, **_):
        super(Transformer, self).__init__(dynamic=True)
        self.d_model = d_model
        self.ff_hidden_size = ff_hidden_size
        self.num_blocks = num_blocks
        self.num_heads = num_heads
        self.vocab_size = vocab_size
        self.max_seq_length = max_seq_length

        self.embedding = Embedding(self.vocab_size, self.d_model, dtype=tf.float32)
        self.pos_encoding = tf.convert_to_tensor(
            get_positional_encodings(self.max_seq_length, self.d_model),
            dtype=tf.float32,
        )[tf.newaxis, ...]

        self.blocks = [
            EncoderBlock(dim=self.d_model, ff_hidden_size=self.ff_hidden_size, num_heads=self.num_heads)
            for _ in range(self.num_blocks)
        ]

    def call(self, x):
        x = self.embedding(x)
        
        # print("You are calling the TRANSFORMER service!", x)
        
        x = tf.math.add(
            x,
            self.pos_encoding[:, :tf.shape(x)[1], :],
            name="positional_encoding"
        )
        
        for block in self.blocks:
            x = block(x)
        
        x = (x @ tf.transpose(self.embedding.variables[0])) # (batch_size, seq_length, vocab_size)
        
        return x


def multi_sparse_cross_entropy(y_true, y_pred):
    loss = tf.keras.losses.SparseCategoricalCrossentropy(
        from_logits=True,
        reduction='none'
    )(y_true , y_pred)

    mask = tf.math.logical_not(tf.math.equal(y_true, 0))
    
    mask = tf.cast(mask, dtype=loss.dtype)
    loss *= mask
    
    return tf.reduce_mean(loss, axis=-1)


def calculate_perplexity(model, X_test, y_test, epsilon=0.0001):
    probas = []
    for X, y in tqdm(zip(X_test, y_test), total=len(X_test)):
        prompt = np.array(X).reshape(1, -1)
        y_pred = tf.nn.softmax(model.predict(prompt)[0], axis=-1)
        for i in range(len(y)):
            if y[i] != 0:
                probas.append(float(y_pred[i, y[i]]))
    
    probas = np.array(probas) + epsilon
    entropy = np.mean(-np.log2(probas))
    perplexity = 2**entropy
    
    return perplexity


def get_tf_dataset(path=data_path, encoder=None, target_vocab_size=hparams["target_vocab_size"], lines=None):
    ds = tf.data.TextLineDataset([path])
    
    if lines is not None:
        ds = ds.take(lines)

    if encoder is None:
        if lines is None:
            print("Computing dataset size...")
            lines = len(list(tqdm(ds)))
        
        print("Fitting encoder...")
        estimated_time = 0.055 * lines**0.7348
        eta = datetime.datetime.today() + datetime.timedelta(seconds=int(estimated_time))
        print("    Estimated time (minutes):", (estimated_time/60))
        print("    Come back at", eta.strftime("%H:%M"))
            
        encoder = tfds.features.text.SubwordTextEncoder.build_from_corpus((x.numpy() for x in ds), target_vocab_size)
    
    return ds, encoder


def load_saved_model(path, params):
    model = Transformer(**params)
    model.compile(loss=multi_sparse_cross_entropy)
    X = [[8, 854, 3, 532, 1, 797, 483, 509, 2, 8]]
    model.fit(X, X, epochs=1, verbose=0)
    model.load_weights(path)
    
    return model


def load_train_test_val_encoder(path=data_path, sample=2E-5, target_vocab_size=hparams["target_vocab_size"]):
    """Load and encode the data, then return train, test and validation data sets

    Args:
        path (str): path to data
        sample (float): size of sample (full dataset percent)

    Returns:
        train, test and validation data sets
    """
    text = load_data(path, sample=sample, split_on="\n")
    X, encoder = encode_data(text, tokens="subwords", target_vocab_size=target_vocab_size)
    train, test = train_test_split(X, test_size=0.1, shuffle=False)
    train, val = train_test_split(train, test_size=0.05, shuffle=False)
    # see : tensorflow_datasets/core/features/text/subword_text_encoder.py
    # encoder.vocab_size returns 1 + len(self._subwords) + text_encoder.NUM_BYTES
    return train, test, val, encoder


def main(train, test, val, encoder, log_training=True, comment=""):
    vocab_size = encoder.vocab_size - 1
    
    X_train, y_train = split_into_X_y(train, 128)
    X_test, y_test = split_into_X_y(test, 128)
    X_val, y_val = split_into_X_y(val, 128)
   
    # Form model
    model = Transformer(
       vocab_size=vocab_size,
       **hparams
    )
    
    model.compile(
        loss=multi_sparse_cross_entropy,
        optimizer=tf.keras.optimizers.Adam(hparams["learning_rate"]),
        # metrics=[tf.keras.metrics.CategoricalAccuracy()],
    )
    
    # Test
    _ = model(np.array(X_train[0]).reshape(1, -1))
    
    print("Non mais allô quoi... Ouvalument!")
    
    history = model.fit(
        X_train,
        y_train,
        # steps_per_epoch=np.ceil(len(X_train)/batch_size),
        batch_size=hparams["batch_size"],
        epochs=hparams["epochs"],
        validation_data=(X_val, y_val),
        callbacks = [
            EarlyStopping(monitor="val_loss", min_delta=0.01, patience=15, verbose=1),
            ModelCheckpoint("logs/tensorflow_transformer/saved_models/checkpoint.h5", monitor="val_loss", save_best_only=True, save_weights_only=False)
        ]
        # use_multiprocessing=False,
    )
    
    # perp = calculate_perplexity(model, X_test, y_test)
    
    prompt = "A l'age de 5 ans , elle invente "
    generated_text = generate_sample_with_transformer(model, prompt, encoder, 500)
    print(prompt + generated_text)
    
    log = {
        "hyperparameters": hparams,
        "history": history.history,
        "sample": {
            "prompt": prompt,
            "output": generated_text,
        },
        #"metrics": {
        #    "perplexity": perp,
        #},
        "data_size": {
            "train": len(X_train),
            "val": len(X_val),
            "test": len(X_test),
        },
        "comment": comment,
    }
    
    if log_training:
        add_to_log(log)
    
    # pprint(log, compact=True)
    
    return model, encoder, log, X_train, y_train

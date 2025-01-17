"""
Model dispatcher
"""
import os
# Supressing tf warnings
import warnings
warnings.filterwarnings('ignore', category=FutureWarning)
try:
    import cPickle as pickle
except ImportError:
    import pickle
from typing import List, Dict
import keras.backend.tensorflow_backend as K
from keras.preprocessing import sequence
from keras.preprocessing.text import Tokenizer
from keras import regularizers
from keras import initializers
from keras.callbacks import EarlyStopping, ReduceLROnPlateau
from keras.models import Model, load_model
from keras.layers import (Dense, GRU, LSTM, Bidirectional, Dropout, Input,
                          SpatialDropout1D, GlobalAveragePooling1D,
                          GlobalMaxPooling1D, MaxPooling1D, concatenate, add)
from keras.layers.embeddings import Embedding
from keras.optimizers import Adam
from keras.utils import multi_gpu_model, plot_model
import numpy as np
import matplotlib.pyplot as plt
from utils import constants


class TextClassifier(object):

    def save(self, model_path: str = None):
        if self._history is not None:
            model_path = self._get_model_path(self._history, model_path)
            os.makedirs(model_path, exist_ok=True)
            print('Saving model statistics')
            self._save_training_stats(self._history, model_path)
        else:
            return
        if self._model is not None:
            print('Saving model')
            self._model.save(os.path.join(model_path, 'model.h5'))
        if self._tokenizer is not None:
            print('Saving tokenizer')
            with open(os.path.join(model_path, 'tokenizer.pickle'),
                      'wb') as handle:
                pickle.dump(self._tokenizer,
                            handle,
                            protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, model_path: str):
        self._model = load_model(f'{model_path}/model.h5')
        with open(f'{model_path}/tokenizer.pickle', 'rb') as model:
            self._tokenizer = pickle.load(model)

    def fit(self,
            X_train,
            y_train,
            glove_path,
            embedding_dim,
            num_words,
            sequence_length,
            validation_data=None,
            epochs=1,
            batch_size=None) -> None:
        self._tokenizer = Tokenizer(num_words)
        self._tokenizer.fit_on_texts(X_train)
        X_val, y_val = validation_data
        X_train = self._text2vec(X_train, num_words, sequence_length)
        X_val = self._text2vec(X_val, num_words, sequence_length)
        parallel_model, self._model = self._lstm(num_words, sequence_length,
                                                 glove_path, embedding_dim)
        print('Training model')
        print(self._model.summary())
        callbacks = [
            EarlyStopping(monitor='val_loss', min_delta=0, patience=5),
            ReduceLROnPlateau(monitor='val_loss',
                              factor=0.2,
                              patience=3,
                              min_lr=0.000001)
        ]
        self._history = parallel_model.fit(X_train,
                                           y_train,
                                           validation_data=(X_val, y_val),
                                           epochs=epochs,
                                           batch_size=batch_size,
                                           callbacks=callbacks)

    def predict_proba(self, x, num_words, sequence_length,
                      batch_size=None) -> np.ndarray:
        x = self._text2vec(x, num_words, sequence_length)
        return self._model.predict(x, batch_size=batch_size)

    @staticmethod
    def _get_model_path(history, model_path: str = None) -> str:
        if 'val_loss' in history.history:
            loss = history.history['val_loss'][-1]
        else:
            loss = history.history['loss'][-1]
        if 'val_binary_accuracy' in history.history:
            acc = history.history['val_binary_accuracy'][-1]
        else:
            acc = history.history['binary_accuracy'][-1]
        model_path = os.path.join(constants.MODELS_PATH,
                                  'lstm_{:.4f}_{:.4f}'.format(loss, acc))
        return model_path

    @staticmethod
    def _get_gpus(gpus: str) -> List[int]:
        """
        Returns a list of integers (numbers of gpus)
        """
        return list(map(int, gpus.split(',')))

    @staticmethod
    def _load_txt_model(model_path: str, vector_size: int) -> Dict:
        """
        Returns pretrained serialized model saved in text format
        where numbers are separated with spaces
        """
        pickled_model = os.path.join(
            constants.PICKLES_PATH,
            '{}.pickle'.format(os.path.basename(model_path)))
        try:
            # load ready text model
            with open(pickled_model, 'rb') as model:
                return pickle.load(model)
        except:
            os.makedirs(os.path.dirname(pickled_model), exist_ok=True)
            # form text model
            with open(model_path, 'r') as file:
                model = {}
                for line in file:
                    split_line = line.split()
                    word = " ".join(split_line[0:len(split_line) - vector_size])
                    embedding = np.array(
                        [float(val) for val in split_line[-vector_size:]])
                    model[word] = embedding
                with open(pickled_model, 'wb') as handle:
                    pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)
                return model

    @staticmethod
    def _plot_loss_acc(history, model_path):
        """
        Saves into files accuracy and loss plots
        """
        plt.gcf().clear()
        # summarize history for accuracy
        plt.plot(history.history['binary_accuracy'])
        plt.plot(history.history['val_binary_accuracy'])
        plt.title('model accuracy')
        plt.ylabel('accuracy')
        plt.xlabel('epoch')
        plt.legend(['train', 'test'], loc='upper left')
        plt.savefig(os.path.join(model_path, 'accuracy.png'))
        plt.gcf().clear()
        # summarize history for loss
        plt.plot(history.history['loss'])
        plt.plot(history.history['val_loss'])
        plt.title('model loss')
        plt.ylabel('loss')
        plt.xlabel('epoch')
        plt.legend(['train', 'test'], loc='upper left')
        plt.savefig(os.path.join(model_path, 'loss.png'))
        plt.gcf().clear()

    def _save_training_stats(self, history, model_path):
        plot_model(self._model,
                   os.path.join(model_path, 'model.png'),
                   show_shapes=True)
        self._plot_loss_acc(history, model_path)

    def _text2vec(self, strings, num_words, max_comment_length):
        strings_seq = sequence.pad_sequences(
            self._tokenizer.texts_to_sequences(strings),
            maxlen=max_comment_length)
        return strings_seq

    def _lstm(self, top_words: int, sequence_length: int, glove_path: str,
              embedding_dim: int):
        """
        Returns compiled keras lstm model ready for training
        Params:
        - top_words - load the dataset but only keep the top n words, zero the rest
        """
        units = 128
        inputs = Input(shape=(None,))
        x = self._get_pretrained_embedding(top_words, sequence_length,
                                           glove_path, embedding_dim)(inputs)
        x = SpatialDropout1D(0.2)(x)
        # For mor detais about kernel_constraint - see chapter 5.1
        # in http://www.cs.toronto.edu/~rsalakhu/papers/srivastava14a.pdf
        x = Bidirectional(GRU(units, return_sequences=True))(x)
        x = Bidirectional(LSTM(units, return_sequences=True))(x)
        hidden = concatenate([
            GlobalMaxPooling1D()(x),
            GlobalAveragePooling1D()(x),
        ])
        hidden = add([hidden, Dense(4 * units, activation='relu')(hidden)])
        hidden = add([hidden, Dense(4 * units, activation='relu')(hidden)])
        output = Dense(1, activation='sigmoid')(hidden)
        gpus = self._get_gpus(os.environ['CUDA_VISIBLE_DEVICES'])
        if len(gpus) == 1:
            with K.tf.device('/gpu:{}'.format(gpus[0])):
                model = Model(inputs, output)
                parallel_model = model
        else:
            with K.tf.device('/cpu:0'):
                # creates a model that includes
                model = Model(inputs, output)
            parallel_model = multi_gpu_model(model, gpus=gpus)
        parallel_model.compile(loss='binary_crossentropy',
                               optimizer=Adam(learning_rate=0.001),
                               metrics=['binary_accuracy'])
        return parallel_model, model

    def _get_pretrained_embedding(self, top_words: int, sequence_length: int,
                                  glove_path: str, embedding_dim: int):
        """
        Returns Embedding layer with pretrained word2vec weights
        """
        word_vectors = {}
        if glove_path is not None:
            word_vectors = self._load_txt_model(glove_path, embedding_dim)
        else:
            return Embedding(input_dim=top_words,
                             output_dim=embedding_dim,
                             input_length=sequence_length,
                             trainable=False,
                             mask_zero=False)

        embedding_matrix = np.zeros((top_words, embedding_dim))
        for word, i in self._tokenizer.word_index.items():
            if i >= top_words:
                continue
            try:
                embedding_vector = word_vectors[word]
                embedding_matrix[i] = embedding_vector
            except KeyError:
                embedding_matrix[i] = np.random.normal(0, np.sqrt(0.25),
                                                       embedding_dim)

        return Embedding(input_dim=top_words,
                         output_dim=embedding_dim,
                         input_length=sequence_length,
                         weights=[embedding_matrix],
                         trainable=False)

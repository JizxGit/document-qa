from typing import List, Optional, Dict

import numpy as np
import tensorflow as tf

from configurable import Configurable
from data_processing.paragraph_qa import ParagraphAndQuestionSpec, ParagraphAndQuestion
from data_processing.span_data import ParagraphSpans
from data_processing.text_features import QaTextFeautrizer
from nn.embedder import WordEmbedder, CharEmbedder
from nn.span_prediction import to_packed_coordinates_np
from trivia_qa.triviaqa_training_data import TriviaQaAnswer
from utils import max_or_none


"""
Stuff to map python objects we want to classify into numpy arrays we can
feed into Tensorflow
"""


class AnswerEncoder(Configurable):
    def init(self, batch_size, context_word_dim):
        raise NotImplementedError()

    def encode(self, batch_size, context_len, context_word_dim, batch) -> Dict:
        raise NotImplementedError()

    def get_placeholders(self) -> List:
        raise NotImplementedError()


class SingleSpanAnswerEncoder(AnswerEncoder):
    def __init__(self):
        self.answer_spans = None

    def get_placeholders(self) -> List:
        return [self.answer_spans]

    def init(self, batch_size, context_word_dim):
        self.answer_spans = tf.placeholder('int32', [batch_size, 2], name='answer_spans')

    def encode(self, batch_size, context_len, context_word_dim, batch) -> Dict:
        answer_spans = np.zeros([batch_size, 2], dtype='int32')

        for doc_ix, doc in enumerate(batch):
            answer = doc.answer

            if answer is None:
                raise ValueError()

            if isinstance(answer, ParagraphSpans):
                answer = doc.answer[np.random.randint(0, len(doc.answer))]
                word_start = answer.para_word_start
                word_end = answer.para_word_end
            elif isinstance(answer, TriviaQaAnswer):
                candidates = np.where(answer.answer_spans[:, 1] < context_len[doc_ix])[0]
                if len(candidates) == 0:
                    raise ValueError()
                ix = candidates[np.random.randint(0, len(candidates))]
                word_start, word_end = answer.answer_spans[ix]
            else:
                raise NotImplementedError()

            if word_start > word_end:
                raise ValueError()
            if word_end >= context_len[doc_ix]:
                raise ValueError(word_end)

            answer_spans[doc_ix, 0] = word_start
            answer_spans[doc_ix, 1] = word_end
        return {self.answer_spans: answer_spans}


class DenseMultiSpanAnswerEncoder(AnswerEncoder):
    def __init__(self):
        self.answer_starts = None
        self.answer_ends = None

    def get_placeholders(self) -> List:
        return [self.answer_starts, self.answer_ends]

    def init(self, batch_size, context_word_dim):
        self.answer_starts = tf.placeholder('bool', [batch_size, context_word_dim], name='answer_spans')
        self.answer_ends = tf.placeholder('bool', [batch_size, context_word_dim], name='answer_spans')

    def encode(self, batch_size, context_len, context_word_dim, batch) -> Dict:
        answer_starts = np.zeros((batch_size, context_word_dim), dtype=np.bool)
        answer_ends = np.zeros((batch_size, context_word_dim), dtype=np.bool)
        for doc_ix, doc in enumerate(batch):
            if doc.answer is None:
                continue
            answer_spans = doc.answer.answer_spans
            answer_spans = answer_spans[answer_spans[:, 1] < context_word_dim]
            answer_starts[doc_ix, answer_spans[:, 0]] = True
            answer_ends[doc_ix, answer_spans[:, 1]] = True
        return {self.answer_starts: answer_starts, self.answer_ends: answer_ends}


class PackedMultiSpanAnswerEncoder(AnswerEncoder):
    def __init__(self, bound):
        self.bound = bound
        self.correct_spans = None

    def get_placeholders(self) -> List:
        return [self.correct_spans]

    def init(self, batch_size, context_word_dim):
        self.correct_spans = tf.placeholder('bool', [batch_size, None], name='correct_span')

    def encode(self, batch_size, context_len, context_word_dim, batch) -> Dict:
        sz = to_packed_coordinates_np(np.array([[context_word_dim-self.bound, context_word_dim-1]]),
                                   context_word_dim, self.bound)[0] + 1
        output = np.zeros((len(batch), sz), dtype=np.bool)
        for doc_ix, doc in enumerate(batch):
            output[doc_ix, to_packed_coordinates_np(doc.answer.answer_spans, context_word_dim, self.bound)] = True
        return {self.correct_spans: output}


class DocumentAndQuestionEncoder(Configurable):
    """
    Uses am WordEmbedder/CharEmbedder to encode text into padded batches of arrays
    """

    def __init__(self,
                 answer_encoder: AnswerEncoder,
                 para_size_th: Optional[int]=None,
                 sent_size_th: Optional[int]=None,
                 word_featurizer: Optional[QaTextFeautrizer]=None):
        # Parameters
        self.answer_encoder = answer_encoder
        self.doc_size_th = para_size_th
        self.sent_size_th = sent_size_th

        self.word_featurizer = word_featurizer

        self._word_embedder = None
        self._char_emb = None

        # Internal stuff we need to set on `init`
        self.len_opt = None
        self.batch_size = None
        self.max_context_word_dim = None
        self.max_ques_word_dim = None
        self.max_char_dim = None

        self.context_features = None
        self.context_words = None
        self.context_chars = None
        self.context_len = None
        self.question_features = None
        self.question_words = None
        self.question_chars = None
        self.question_len = None

    @property
    def version(self):
        # version 1: added word_featurizer
        # version 2: answer encoder is now modular
        return 2

    def init(self, input_spec: ParagraphAndQuestionSpec, len_op: bool,
             word_emb: WordEmbedder, char_emb: CharEmbedder):

        self._word_embedder = word_emb
        self._char_emb = char_emb

        self.batch_size = input_spec.batch_size
        self.len_opt = len_op

        self.max_ques_word_dim = input_spec.max_num_quesiton_words
        if self._char_emb is not None:
            if input_spec.max_word_size is not None:
                self.max_char_dim = min(self._char_emb.get_word_size_th(), input_spec.max_word_size)
            else:
                self.max_char_dim = self._char_emb.get_word_size_th()
        else:
            self.max_char_dim = 1

        self.max_context_word_dim = max_or_none(input_spec.max_num_context_words, self.doc_size_th)

        if not self.len_opt:
            n_context_words = self.max_context_word_dim
            n_question_words = self.max_ques_word_dim
        else:
            n_context_words = None
            n_question_words = None

        batch_size = self.batch_size

        self.context_words = tf.placeholder('int32', [batch_size, n_context_words], name='context_words')
        self.context_len = tf.placeholder('int32', [batch_size], name='context_len')

        self.question_words = tf.placeholder('int32', [batch_size, n_question_words], name='question_words')
        self.question_len = tf.placeholder('int32', [batch_size], name='question_len')

        if self._char_emb:
            self.context_chars = tf.placeholder('int32', [batch_size, n_context_words, self.max_char_dim], name='context_chars')
            self.question_chars = tf.placeholder('int32', [batch_size, n_question_words, self.max_char_dim], name='question_chars')
        else:
            self.context_chars = None
            self.question_chars = None

        if self.word_featurizer is not None:
            self.question_features = tf.placeholder('float32',
                                                    [batch_size, n_question_words,
                                                     self.word_featurizer.n_question_features()],
                                                    name='question_features')
            self.context_features = tf.placeholder('float32', [batch_size, n_context_words,
                                                               self.word_featurizer.n_context_features()],
                                                   name='context_features')
        else:
            self.question_features = None
            self.context_features = None

        self.answer_encoder.init(batch_size, n_context_words)

    def get_placeholders(self):
        return [x for x in
                [self.question_len, self.question_words, self.question_chars, self.question_features,
                 self.context_len, self.context_words, self.context_chars, self.context_features]
                if x is not None] + self.answer_encoder.get_placeholders()

    def encode(self, batch: List[ParagraphAndQuestion], is_train: bool):
        batch_size = len(batch)
        if self.batch_size is not None:
            if self.batch_size < batch_size:
                raise ValueError("Batch sized we pre-specified as %d, but got a batch of %d" % (self.batch_size, batch_size))
            # We have a fixed batch size, so we will pad our inputs with zeros along the batch dimension
            batch_size = self.batch_size

        context_word_dim, ques_word_dim, max_char_dim = \
            self.max_context_word_dim, self.max_ques_word_dim, self.max_char_dim

        feed_dict = {}

        if is_train and context_word_dim is not None:
            # Context might be truncated
            context_len = np.array([min(sum(len(s) for s in doc.context), context_word_dim)
                                    for doc in batch], dtype='int32')
        else:
            context_len = np.array([sum(len(s) for s in doc.context) for doc in batch], dtype='int32')
            context_word_dim = context_len.max()

        question_len = np.array([len(x.question) for x in batch], dtype='int32')

        if ques_word_dim is not None and question_len.max() > ques_word_dim:
            raise ValueError("Have a question of len %d but max ques dim is %d" %
                             (question_len.max(), ques_word_dim))
        feed_dict[self.context_len] = context_len
        feed_dict[self.question_len] = question_len

        if ques_word_dim is None:
            ques_word_dim = question_len.max()
        if context_word_dim is None:
            context_word_dim = context_len.max()

        if self._word_embedder is not None:
            context_words = np.zeros([batch_size, context_word_dim], dtype='int32')
            question_words = np.zeros([batch_size, ques_word_dim], dtype='int32')
            feed_dict[self.context_words] = context_words
            feed_dict[self.question_words] = question_words
        else:
            question_words, context_words = None, None

        if self._char_emb is not None:
            context_chars = np.zeros([batch_size, context_word_dim, max_char_dim], dtype='int32')
            question_chars = np.zeros([batch_size, ques_word_dim, max_char_dim], dtype='int32')
            feed_dict[self.question_chars] = question_chars
            feed_dict[self.context_chars] = context_chars
        else:
            context_chars, question_chars = None, None

        for doc_ix, doc in enumerate(batch):
            placeholders = {}

            for word_ix, word in enumerate(doc.question):
                if self._word_embedder is not None:
                    ix = self._word_embedder.question_word_to_ix(word)
                    if ix < 0:
                        wl = word.lower()
                        if wl in placeholders:
                            ix = placeholders[wl]
                        else:
                            ix = self._word_embedder.get_placeholder(ix, is_train)
                            placeholders[wl] = ix

                    question_words[doc_ix, word_ix] = ix
                if self._char_emb is not None:
                    for char_ix, char in enumerate(word):
                        if char_ix == self.max_char_dim:
                            break
                        question_chars[doc_ix, word_ix, char_ix] = self._char_emb.char_to_ix(char)

            word_ix = 0
            for sent_ix, sent in enumerate(doc.context):
                if self.sent_size_th is not None and sent_ix == self.sent_size_th:
                    break
                for word in sent:
                    if word_ix == self.max_context_word_dim:
                        break
                    if self._word_embedder is not None:
                        ix = self._word_embedder.context_word_to_ix(word)
                        if ix < 0:
                            wl = word.lower()
                            if wl in placeholders:
                                ix = placeholders[wl]
                            else:
                                ix = self._word_embedder.get_placeholder(ix, is_train)
                                placeholders[wl] = ix

                        context_words[doc_ix, word_ix] = ix

                    if self._char_emb is not None:
                        for char_ix, char in enumerate(word):
                            if char_ix == self.max_char_dim:
                                break
                            context_chars[doc_ix, word_ix, char_ix] = self._char_emb.char_to_ix(char)
                    word_ix += 1

        feed_dict.update(self.answer_encoder.encode(batch_size, context_len, context_word_dim, batch))

        if self.word_featurizer is not None:
            question_word_features = np.zeros((batch_size, ques_word_dim, self.word_featurizer.n_question_features()))
            context_word_features = np.zeros((batch_size, context_word_dim, self.word_featurizer.n_context_features()))
            for doc_ix, doc in enumerate(batch):
                truncated_context = []
                for sent_ix, sent in enumerate(doc.context):
                    if self.sent_size_th is not None and sent_ix == self.sent_size_th:
                        break
                    truncated_context += sent
                q_f, c_f = self.word_featurizer.get_features(doc.question, truncated_context[:self.max_context_word_dim])
                question_word_features[doc_ix, :q_f.shape[0]] = q_f
                context_word_features[doc_ix, :c_f.shape[0]] = c_f
            feed_dict[self.context_features] = context_word_features
            feed_dict[self.question_features] = question_word_features

        return feed_dict

    def __setstate__(self, state):
        if state["version"] == 0:
            if "word_featurizer" in state["state"]:
                raise ValueError()
            state["state"]["word_featurizer"] = None
        if state["version"] <= 1:
            if "answer_encoder" in state["state"]:
                raise ValueError()
            state["state"]["answer_encoder"] = SingleSpanAnswerEncoder()
        super().__setstate__(state)


class MultiContextAndQuestionEncoder(Configurable):

    def __init__(self):
        self._word_embedder = None

        # Internal stuff we need to set on `init`
        self.batch_size = None

        self.context_words = None
        self.context_len = None
        self.question_words = None
        self.question_len = None

    def init(self, batch_size: Optional[int], word_emb: WordEmbedder):
        self._word_embedder = word_emb

        self.context_words = tf.placeholder('int32', [batch_size, None, None], name='context_words')
        self.context_len = tf.placeholder('int32', [batch_size, None], name='context_len')

        self.question_words = tf.placeholder('int32', [batch_size, None], name='question_words')
        self.question_len = tf.placeholder('int32', [batch_size], name='question_len')

    def get_placeholders(self):
        return [x for x in
                [self.question_len, self.question_words, self.context_len, self.context_words]
                if x is not None]

    def encode(self, batch: List[ParagraphAndQuestion], is_train: bool):
        batch_size = len(batch)
        contexts = [x.context for x in batch]

        feed_dict = {}

        para_dim = max(len(c) for c in contexts)
        context_len = np.zeros((batch_size, para_dim), dtype='int32')
        for ix, c in enumerate(contexts):
            context_len[ix, :len(c)] = [len(s) for s in c]
        context_word_dim = context_len.max()

        question_len = np.array([len(x.question) for x in batch], dtype='int32')
        question_word_dim = question_len.max()

        feed_dict[self.context_len] = context_len
        feed_dict[self.question_len] = question_len

        if self._word_embedder is not None:
            context_words = np.zeros([batch_size, para_dim, context_word_dim], dtype='int32')
            question_words = np.zeros([batch_size, question_word_dim], dtype='int32')
            feed_dict[self.context_words] = context_words
            feed_dict[self.question_words] = question_words
        else:
            question_words, context_words = None, None

        for doc_ix, doc in enumerate(batch):
            placeholders = {}
            for word_ix, word in enumerate(doc.question):
                if self._word_embedder is not None:
                    ix = self._word_embedder.question_word_to_ix(word)
                    if ix < 0:
                        wl = word.lower()
                        if wl in placeholders:
                            ix = placeholders[wl]
                        else:
                            ix = self._word_embedder.get_placeholder(ix, is_train)
                            placeholders[wl] = ix

                    question_words[doc_ix, word_ix] = ix

            for para_ix, para in enumerate(contexts[doc_ix]):
                for word_ix, word in enumerate(para):
                    if self._word_embedder is not None:
                        ix = self._word_embedder.context_word_to_ix(word)
                        if ix < 0:
                            raise ValueError()
                        context_words[doc_ix, para_ix, word_ix] = ix

        return feed_dict


class QuestionEncoder(Configurable):
    def __init__(self):
        self._max_char_dim = None

        self._word_embedder = None
        self._char_emb = None
        self.question_words = None
        self.question_chars = None
        self.question_len = None

    def get_placeholders(self):
        return [x for x in [self.question_len, self.question_words,
                            self.question_chars] if x is not None]

    def init(self, batch_size, word_emb: WordEmbedder, char_emb: CharEmbedder):
        self._word_embedder = word_emb
        self._char_emb = char_emb

        if self._char_emb is not None:
            self._max_char_dim = self._char_emb.get_word_size_th()
        else:
            self._max_char_dim = 1

        self.question_words = tf.placeholder('int32', [batch_size, None], name='question_words')
        self.question_len = tf.placeholder('int32', [batch_size], name='question_len')

        if self._char_emb:
            self.question_chars = tf.placeholder('int32', [batch_size, None, self._max_char_dim], name='question_chars')
        else:
            self.question_chars = None

    def encode(self, batch: List[List[str]], is_train: bool):
        max_char_dim = self._max_char_dim
        n_questions = len(batch)

        feed_dict = {}
        question_len = np.array([len(q) for q in batch], dtype='int32')
        feed_dict[self.question_len] = question_len

        ques_word_dim = question_len.max()

        if self._word_embedder is not None:
            question_words = np.zeros([n_questions, ques_word_dim], dtype='int32')
            feed_dict[self.question_words] = question_words
        else:
            question_words = None

        if self._char_emb is not None:
            question_chars = np.zeros([n_questions, ques_word_dim, max_char_dim], dtype='int32')
            feed_dict[self.question_chars] = question_chars
        else:
            question_chars = None

        for question_ix, question in enumerate(batch):
            for word_ix, word in enumerate(question):
                if self._word_embedder is not None:
                    ix = self._word_embedder.question_word_to_ix(word)
                    if ix < 0:
                        raise ValueError("This encoder does not support placeholders")

                    question_words[question_ix, word_ix] = ix
                if self._char_emb is not None:
                    for char_ix, char in enumerate(word):
                        if char_ix == self._max_char_dim:
                            break
                        question_chars[question_ix, word_ix, char_ix] = self._char_emb.char_to_ix(char)
        return feed_dict


class CheatingEncoder(DocumentAndQuestionEncoder):
    def __init__(self, bound=None):
        super().__init__(DenseMultiSpanAnswerEncoder())
        self.bound = bound

    def encode(self, batch: List[ParagraphAndQuestion], is_train: bool):
        batch_size = len(batch)
        if self.batch_size is not None:
            if self.batch_size < batch_size:
                raise ValueError()
            # We have a fixed batch size, so we will pad our inputs with zeros along
            # the batch dimension
            batch_size = self.batch_size
        N = batch_size
        # else dynamically use the batch size of the examples

        context_word_dim, ques_word_dim, max_char_dim = \
            self.max_context_word_dim, self.max_ques_word_dim, self.max_char_dim

        feed_dict = {}

        if is_train and context_word_dim is not None:
            # Context might be truncated
            context_len = np.array([min(sum(len(s) for s in doc.context), context_word_dim)
                                    for doc in batch], dtype='int32')
        else:
            context_len = np.array([sum(len(s) for s in doc.context) for doc in batch], dtype='int32')
            context_word_dim = context_len.max()

        question_len = np.array([len(x.question) for x in batch], dtype='int32')
        if question_len.max() > ques_word_dim:
            raise ValueError("Have a question of len %d but max ques dim is %d" %
                             (question_len.max(), ques_word_dim))
        feed_dict[self.context_len] = context_len
        feed_dict[self.question_len] = question_len

        if self.len_opt:
            ques_word_dim = min(ques_word_dim, question_len.max())
            context_word_dim = min(context_word_dim, context_len.max())

        if self._word_embedder is not None:
            context_words = np.zeros([N, context_word_dim], dtype='int32')
            question_words = np.zeros([N, ques_word_dim], dtype='int32')
            feed_dict[self.context_words] = context_words
            feed_dict[self.question_words] = question_words
        else:
            question_words, context_words = None, None

        if self._char_emb is not None:
            context_chars = np.zeros([N, context_word_dim, max_char_dim], dtype='int32')
            question_chars = np.zeros([N, ques_word_dim, max_char_dim], dtype='int32')
            feed_dict[self.question_chars] = question_chars
            feed_dict[self.context_chars] = context_chars
        else:
            context_chars, question_chars = None, None

        # Build vector encoding of the answers for each question in the batch
        for doc_ix, doc in enumerate(batch):
            context_words[doc_ix] = 0
            if doc.answer is None:
                continue

            for s,e in doc.answer.answer_spans:
                context_words[doc_ix, s] = self._word_embedder.question_word_to_ix("what")
                context_words[doc_ix, e] = self._word_embedder.question_word_to_ix("the")
                for i in range(s+1, e):
                    context_words[doc_ix, i] = self._word_embedder.question_word_to_ix("a")

        feed_dict.update(self.answer_encoder.encode(batch_size, context_len, context_word_dim, batch))

        return feed_dict


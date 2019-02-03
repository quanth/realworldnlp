import math
import random
from collections import Counter

import torch
import torch.optim as optim
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import LabelField
from allennlp.data.instance import Instance
from allennlp.data.iterators import BasicIterator
from allennlp.data.vocabulary import Vocabulary
from allennlp.models import Model
from allennlp.modules.token_embedders import Embedding
from allennlp.training.trainer import Trainer
from overrides import overrides
from torch.nn import CosineSimilarity
from torch.nn import functional
import numpy as np

from scipy.stats import spearmanr

EMBEDDING_DIM = 256
BATCH_SIZE = 256
CUDA_DEVICE = 0

@DatasetReader.register("skip_gram")
class SkipGramReader(DatasetReader):
    def __init__(self, window_size=5, lazy=False, vocab: Vocabulary = None):
        super().__init__(lazy=lazy)
        self.window_size = window_size
        self.reject_probs = None
        if vocab:
            self.reject_probs = {}
            # threshold = 1.e-5
            threshold = 1.e-3
            token_counts = vocab._retained_counter['token_in']  # HACK
            total_counts = sum(token_counts.values())
            for _, token in vocab.get_index_to_token_vocabulary('token_in').items():
                counts = token_counts[token]
                if counts > 0:
                    normalized_counts = counts / total_counts
                    reject_prob = 1. - math.sqrt(threshold / normalized_counts)
                    reject_prob = max(0., reject_prob)
                else:
                    reject_prob = 0.
                self.reject_probs[token] = reject_prob

    def _subsample_tokens(self, tokens):
        new_tokens = []
        for token in tokens:
            reject_prob = self.reject_probs.get(token, 0.)
            if random.random() <= reject_prob:
                new_tokens.append(None)
            else:
                new_tokens.append(token)

        return new_tokens

    @overrides
    def _read(self, file_path: str):
        with open(file_path, "r") as text_file:
            for line in text_file:
                tokens = line.strip().split(' ')
                tokens = tokens[:5000000]  # TODO: remove

                if self.reject_probs:
                    tokens = self._subsample_tokens(tokens)
                    print(tokens[:200])  # for debugging

                for i, token in enumerate(tokens):
                    if token is None:
                        continue

                    token_in = LabelField(token, label_namespace='token_in')

                    for j in range(i - self.window_size, i + self.window_size + 1):
                        if j < 0 or i == j or j > len(tokens) - 1:
                            continue

                        if tokens[j] is None:
                            continue

                        token_out = LabelField(tokens[j], label_namespace='token_out')
                        yield Instance({'token_in': token_in, 'token_out': token_out})


class SkipGramModel(Model):
    def __init__(self, vocab, embedding_in, embedding_out, neg_samples=10, cuda_device=-1):
        super().__init__(vocab)
        self.embedding_in = embedding_in
        self.embedding_out = embedding_out
        self.neg_samples = neg_samples
        self.cuda_device = cuda_device

        token_to_probs = {}
        token_counts = vocab._retained_counter['token_in']  # HACK
        total_counts = sum(token_counts.values())
        total_probs = 0.
        for token, counts in token_counts.items():
            unigram_freq = counts / total_counts
            unigram_freq = math.pow(unigram_freq, 3 / 4)
            token_to_probs[token] = unigram_freq
            total_probs += unigram_freq

        self.neg_sample_probs = np.ndarray((vocab.get_vocab_size('token_in'),))
        for token_id, token in vocab.get_index_to_token_vocabulary('token_in').items():
            self.neg_sample_probs[token_id] = token_to_probs.get(token, 0) / total_probs


    def forward(self, token_in, token_out):
        batch_size = token_out.shape[0]

        embedded_in = self.embedding_in(token_in)
        embedded_out = self.embedding_out(token_out)
        inner_positive = torch.mul(embedded_in, embedded_out).sum(dim=1)
        log_prob = functional.logsigmoid(inner_positive)

        negative_out = np.random.choice(a=self.vocab.get_vocab_size('token_in'),
                                        size=batch_size * self.neg_samples,
                                        p=self.neg_sample_probs)
        negative_out = torch.LongTensor(negative_out).view(batch_size, self.neg_samples)
        if self.cuda_device > -1:
            negative_out = negative_out.to(self.cuda_device)

        embedded_negative_out = self.embedding_out(negative_out)
        inner_negative = torch.bmm(embedded_negative_out, embedded_in.unsqueeze(2)).squeeze()
        log_prob += functional.logsigmoid(-1. * inner_negative).sum(dim=1)

        return {'loss': -log_prob.sum() / batch_size}


def write_embeddings(embedding: Embedding, file_path, vocab: Vocabulary):
    with open(file_path, mode='w') as f:
        for index, token in vocab.get_index_to_token_vocabulary('token_in').items():
            values = ['{:.5f}'.format(val) for val in embedding.weight[index]]
            f.write(' '.join([token] + values))
            f.write('\n')


def get_synonyms(token, embedding, vocab: Vocabulary, num_synonyms: int = 10):
    token_id = vocab.get_token_index(token, 'token_in')
    token_vec = embedding.weight[token_id]
    cosine = CosineSimilarity(dim=0)
    sims = Counter()

    for index, token in vocab.get_index_to_token_vocabulary('token_in').items():
        sim = cosine(token_vec, embedding.weight[index]).item()
        sims[token] = sim

    return sims.most_common(num_synonyms)


def read_simlex999():
    simlex999 = []
    with open('data/SimLex-999/SimLex-999.txt') as f:
        next(f)
        for line in f:
            fields = line.strip().split('\t')
            word1, word2, _, sim = fields[:4]
            sim = float(sim)
            simlex999.append((word1, word2, sim))

    return simlex999


def evaluate_embeddings(embedding, vocab: Vocabulary):
    cosine = CosineSimilarity(dim=0)

    simlex999 = read_simlex999()
    sims_pred = []
    oov_count = 0
    for word1, word2, sim in simlex999:
        word1_id = vocab.get_token_index(word1, 'token_in')
        if word1_id == 1:
            sims_pred.append(0.)
            oov_count += 1
            continue
        word2_id = vocab.get_token_index(word2, 'token_in')
        if word2_id == 1:
            sims_pred.append(0.)
            oov_count += 1
            continue

        sim_pred = cosine(embedding.weight[word1_id],
                          embedding.weight[word2_id]).item()
        sims_pred.append(sim_pred)

    assert len(sims_pred) == len(simlex999)
    print('# of OOV words: {} / {}'.format(oov_count, len(simlex999)))

    return spearmanr(sims_pred, [sim for _, _, sim in simlex999])


def main():
    reader = SkipGramReader()
    text8 = reader.read('data/text8/text8')

    vocab = Vocabulary.from_instances(text8, min_count={'token_in': 5, 'token_out': 5})

    reader = SkipGramReader(vocab=vocab)
    text8 = reader.read('data/text8/text8')

    embedding_in = Embedding(num_embeddings=vocab.get_vocab_size('token_in'),
                             embedding_dim=EMBEDDING_DIM)
    embedding_out = Embedding(num_embeddings=vocab.get_vocab_size('token_out'),
                              embedding_dim=EMBEDDING_DIM)
    iterator = BasicIterator(batch_size=BATCH_SIZE)
    iterator.index_with(vocab)

    model = SkipGramModel(vocab=vocab,
                          embedding_in=embedding_in,
                          embedding_out=embedding_out,
                          neg_samples=10,
                          cuda_device=CUDA_DEVICE)

    optimizer = optim.Adam(model.parameters())

    trainer = Trainer(model=model,
                      optimizer=optimizer,
                      iterator=iterator,
                      train_dataset=text8,
                      num_epochs=5,
                      cuda_device=CUDA_DEVICE)
    trainer.train()

    # write_embeddings(embedding_in, 'data/text8/embeddings.txt', vocab)
    print(get_synonyms('one', embedding_in, vocab))
    print(get_synonyms('december', embedding_in, vocab))
    print(get_synonyms('flower', embedding_in, vocab))
    print(get_synonyms('design', embedding_in, vocab))
    print(get_synonyms('snow', embedding_in, vocab))

    rho = evaluate_embeddings(embedding_in, vocab)
    print('simlex999 speareman correlation: {}'.format(rho))


if __name__ == '__main__':
    main()
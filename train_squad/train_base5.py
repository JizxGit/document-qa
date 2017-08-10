import trainer
from data_processing.qa_data import FixedParagraphQaTrainingData, Batcher
from doc_qa_models import Attention
from encoder import DocumentAndQuestionEncoder, SingleSpanAnswerEncoder
from evaluator import LossEvaluator, BoundedSpanEvaluator, SentenceSpanEvaluator
from nn.attention import StaticAttention, StaticAttentionSelf
from nn.embedder import FixedWordEmbedder, CharWordEmbedder, LearnedCharEmbedder
from nn.layers import NullBiMapper, SequenceMapperSeq, FullyConnectedMerge, DropoutLayer, ChainBiMapper
from nn.recurrent_layers import BiRecurrentMapper, LstmCellSpec, RecurrentEncoder, EncodeOverTime
from nn.similarity_layers import DotProductProject
from nn.span_prediction import BoundsPredictor
from trainer import SerializableOptimizer, TrainParams
from squad.squad import SquadCorpus
from utils import get_output_name_from_cli

"""
-> Increasing the dropout (to 0.75) hurts
-> Adding another LSTM between the self attention and prediction hurts
-> Adding a question encoding merge layer between the self attention and is neutral 
-> MatchWord features (with shared encoders) makes training faster, but hurts in the long run 
"""


def main():
    out = get_output_name_from_cli()

    train_params = TrainParams(SerializableOptimizer("Adadelta", dict(learning_rate=1.0)),
                               num_epochs=20, log_period=20, eval_period=1200, save_period=1200,
                               eval_samples=dict(dev=8000, train=8000))

    enc = SequenceMapperSeq(
        DropoutLayer(0.8),
        BiRecurrentMapper(LstmCellSpec(80)),
        DropoutLayer(0.8),
    )

    model = Attention(
        encoder=DocumentAndQuestionEncoder(SingleSpanAnswerEncoder()),
        word_embed_layer=None,
        word_embed=FixedWordEmbedder(vec_name="glove.840B.300d", word_vec_init_scale=0, learn_unk=False),
        char_embed=CharWordEmbedder(
            LearnedCharEmbedder(word_size_th=14, char_th=50, char_dim=15, init_scale=0.1),
            EncodeOverTime(RecurrentEncoder(LstmCellSpec(50), 'h'), mask=True),
            shared_parameters=True
        ),
        embed_mapper=enc,
        question_mapper=None,
        context_mapper=None,
        memory_builder=NullBiMapper(),
        attention=StaticAttention(DotProductProject(160, bias=True, scale=True, share_project=True),
                                  FullyConnectedMerge(160)),
        match_encoder=SequenceMapperSeq(
            BiRecurrentMapper(LstmCellSpec(80, keep_probs=0.8)),
            DropoutLayer(0.8),
            StaticAttentionSelf(DotProductProject(160, bias=True, scale=True, share_project=True),
                                FullyConnectedMerge(160)),
        ),
        predictor=BoundsPredictor(ChainBiMapper(
            first_layer=BiRecurrentMapper(LstmCellSpec(80, keep_probs=0.8)),
            second_layer=BiRecurrentMapper(LstmCellSpec(80, keep_probs=0.8)),
        ))
    )
    with open(__file__, "r") as f:
        notes = f.read()

    corpus = SquadCorpus()
    train_batching = Batcher(45, "bucket_context_words_3", True, False)
    eval_batching = Batcher(45, "context_words", False, False)
    data = FixedParagraphQaTrainingData(corpus, None, train_batching, eval_batching)

    eval = [LossEvaluator(), BoundedSpanEvaluator(bound=[17]), SentenceSpanEvaluator()]
    trainer.start_training(data, model, train_params, eval, trainer.ModelDir(out), notes, False)


if __name__ == "__main__":
    main()
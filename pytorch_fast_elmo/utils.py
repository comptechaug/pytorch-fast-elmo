from typing import List, Tuple, Iterable, Dict

import torch
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence, PackedSequence
import h5py

from pytorch_fast_elmo.restore import ElmoCharacterEncoderRestorer
from _pytorch_fast_elmo import ElmoCharacterEncoder  # pylint: disable=no-name-in-module


def load_vocab(vocab_txt: str) -> List[str]:
    """
    Use the same format as bilm-tf.
    """
    vocab = []
    with open(vocab_txt) as fin:
        for line in fin:
            word = line.strip()
            if word:
                vocab.append(word)
    return vocab


def build_vocab2id(vocab: List[str]) -> Dict[str, int]:
    """
    Adding one will be applied for padding.
    """
    assert len(vocab) > 3
    assert vocab[:3] == ['<S>', '</S>', '<UNK>']
    return {word: word_id for word_id, word in enumerate(vocab, start=1)}


def load_and_build_vocab2id(vocab_txt: str) -> Dict[str, int]:
    return build_vocab2id(load_vocab(vocab_txt))


def get_lengths_of_zero_padded_batch(inputs: torch.Tensor) -> torch.Tensor:
    if inputs.dim() == 2:
        lengths = (inputs > 0).long().sum(dim=-1)
    elif inputs.dim() == 3:
        lengths = ((inputs > 0).long().sum(dim=-1) > 0).long().sum(dim=-1)
    else:
        raise ValueError("inputs should be 2D or 3D.")

    return lengths


def pack_inputs(inputs: torch.Tensor) -> PackedSequence:
    """
    Pack inputs of shape `(batch_size, timesteps, x)` or `(batch_size, timesteps)`.
    Padding value should be 0.
    """
    lengths = get_lengths_of_zero_padded_batch(inputs)
    return pack_padded_sequence(inputs, lengths, batch_first=True)


def generate_mask_from_lengths(
        batch_size: int,
        max_timesteps: int,
        lengths: torch.Tensor,
) -> torch.Tensor:
    ones = lengths.new_ones(batch_size, max_timesteps, dtype=torch.long)
    range_tensor = ones.cumsum(dim=-1)
    return (lengths.unsqueeze(1) >= range_tensor).long()


def unpack_outputs(
        inputs: PackedSequence,
        skip_mask: bool = False,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Unpack the final result and return `(tensor, mask)`.
    """
    tensor, lengths = pad_packed_sequence(inputs, batch_first=True)
    if skip_mask:
        return tensor, None

    if tensor.is_cuda:
        lengths = lengths.cuda()
    mask = generate_mask_from_lengths(
            tensor.shape[0],
            tensor.shape[1],
            lengths,
    )
    return tensor, mask


class ElmoCharacterIdsConst:
    """
    From Allennlp.
    """
    MAX_WORD_LENGTH = 50

    BEGINNING_OF_SENTENCE_CHARACTER = 256  # <begin sentence>
    END_OF_SENTENCE_CHARACTER = 257  # <end sentence>
    BEGINNING_OF_WORD_CHARACTER = 258  # <begin word>
    END_OF_WORD_CHARACTER = 259  # <end word>
    PADDING_CHARACTER = 260  # <padding>


def make_padded_char_ids(
        char_ids: Iterable[int],  # +1 should have been applied.
        max_word_length: int = ElmoCharacterIdsConst.MAX_WORD_LENGTH,
        padding_character: int = ElmoCharacterIdsConst.PADDING_CHARACTER + 1,
        beginning_of_word_character: int = ElmoCharacterIdsConst.BEGINNING_OF_WORD_CHARACTER + 1,
        end_of_word_character: int = ElmoCharacterIdsConst.END_OF_WORD_CHARACTER + 1,
) -> List[int]:
    padded = [padding_character] * max_word_length

    padded[0] = beginning_of_word_character
    idx = 1
    for char_id in char_ids:
        if idx >= max_word_length:
            break
        padded[idx] = char_id
        idx += 1

    idx = min(idx, max_word_length - 1)
    padded[idx] = end_of_word_character

    return padded


def make_bos(max_word_length: int = ElmoCharacterIdsConst.MAX_WORD_LENGTH) -> List[int]:
    return make_padded_char_ids(
            (ElmoCharacterIdsConst.BEGINNING_OF_SENTENCE_CHARACTER + 1,),
            max_word_length,
    )


def make_eos(max_word_length: int = ElmoCharacterIdsConst.MAX_WORD_LENGTH) -> List[int]:
    return make_padded_char_ids(
            (ElmoCharacterIdsConst.END_OF_SENTENCE_CHARACTER + 1,),
            max_word_length,
    )


def word_to_char_ids(word: str) -> List[int]:
    # +1 is applied here.
    return [char_id + 1 for char_id in word.encode('utf-8', 'ignore')]


_WORD_TO_CHAR_IDS_EXCEPTION = {
        '<S>': make_bos,
        '</S>': make_eos,
}


def batch_to_char_ids(
        batch: List[List[str]],
        max_characters_per_token: int = ElmoCharacterIdsConst.MAX_WORD_LENGTH,
) -> torch.Tensor:
    """
    From Allennlp.

    Note:
    1. `batch` should have been sorted by length in reversed order.
    2. BOS/EOS will be treated specially.
    3. UNK will be treated as normal string, same as bilm-tf.

    Return tensor of shape `(batch_size, max_timesteps, max_characters_per_token)`.
    """
    max_timesteps = len(batch[0])
    zeros = torch.LongTensor([0] * max_characters_per_token)

    rows = []
    for words in batch:
        row = []
        for word in words:
            special_gen = _WORD_TO_CHAR_IDS_EXCEPTION.get(word)
            if special_gen is None:
                char_ids = make_padded_char_ids(
                        word_to_char_ids(word),
                        max_characters_per_token,
                )
            else:
                char_ids = special_gen(max_characters_per_token)

            # of shape `(max_characters_per_token,)`
            row.append(torch.LongTensor(char_ids))

        # Add padding.
        row.extend([zeros] * (max_timesteps - len(row)))
        # Stack to shape `(max_timesteps, max_characters_per_token)`
        rows.append(torch.stack(row))

    # Stack to shape `(batch_size, max_timesteps, max_characters_per_token)`
    return torch.stack(rows)


def batch_to_word_ids(batch: List[List[str]], vocab2id: Dict[str, int]) -> torch.Tensor:
    """
    For word embedding.

    1. `batch` should have been sorted by length in reversed order.
    2. UNK will be mapped to 3 since we assume the vocab starts with `<S>, </S>, <UNK>`.

    Return tensor of shape `(batch_size, max_timesteps)`.
    """
    max_timesteps = len(batch[0])

    rows = []
    for words in batch:
        row = [vocab2id.get(word, 3) for word in words]
        row.extend([0] * (max_timesteps - len(row)))
        rows.append(row)

    return torch.LongTensor(rows)


def get_bos_eos_token_repr(
        char_cnn_restorer: ElmoCharacterEncoderRestorer,
        char_cnn: ElmoCharacterEncoder,
) -> Tuple[torch.Tensor, torch.Tensor]:
    # [<bow>, <bos/eos>, <eow>, max(kernal)...]
    max_characters_per_token = max(kernal_size for kernal_size, _ in char_cnn_restorer.filters)
    max_characters_per_token += 3

    bos_ids = make_bos(max_characters_per_token)
    eos_ids = make_eos(max_characters_per_token)
    bos_eos = torch.LongTensor([bos_ids, eos_ids])

    if char_cnn.parameters()[0].is_cuda:
        bos_eos = bos_eos.cuda()

    with torch.no_grad():
        bos_eos_reprs = char_cnn(bos_eos)

    return bos_eos_reprs[0], bos_eos_reprs[1]


def cache_char_cnn_vocab(
        vocab_txt: str,
        options_file: str,
        weight_file: str,
        hdf5_out: str,
        max_characters_per_token: int = ElmoCharacterIdsConst.MAX_WORD_LENGTH,
        cuda: bool = False,
        batch_size: int = 256,
) -> None:
    """
    1. Load vocab.
    2. Feed vocab to Char CNN.
    3. Feed BOS/EOS to Char CNN.
    4. Dump reprs to HDF5. (will be loaded by `ElmoWordEmbeddingRestorer`).
    """
    # 1.
    vocab = load_vocab(vocab_txt)

    # 2.
    char_cnn_restorer = ElmoCharacterEncoderRestorer(
            options_file,
            weight_file,
    )
    char_cnn = char_cnn_restorer.restore(requires_grad=False)
    if cuda:
        char_cnn.cuda()

    cached = []
    for batch_start in range(0, len(vocab), batch_size):
        batch = vocab[batch_start:batch_start + batch_size]
        # (1, batch_size, max_characters_per_token)
        char_ids = batch_to_char_ids([batch], max_characters_per_token)
        if cuda:
            char_ids = char_ids.cuda()

        inputs = pack_inputs(char_ids)
        output_data = char_cnn(inputs.data)
        # (1, batch_size, output_dim)
        char_reprs, _ = unpack_outputs(
                PackedSequence(output_data, inputs.batch_sizes),
                skip_mask=True,
        )
        # (batch_size, output_dim)
        cached.append(char_reprs.squeeze(0))

    # (vocab, output_dim)
    combined = torch.cat(cached, dim=0)
    if cuda:
        combined = combined.cpu()
    embedding_weight = combined.numpy()

    # 3.
    lstm_bos_repr, lstm_eos_repr = get_bos_eos_token_repr(
            char_cnn_restorer,
            char_cnn,
    )
    if cuda:
        lstm_bos_repr = lstm_bos_repr.cpu()
        lstm_eos_repr = lstm_eos_repr.cpu()

    lstm_bos_weight = lstm_bos_repr.numpy()
    lstm_eos_weight = lstm_eos_repr.numpy()

    embedding_weight[0] = lstm_bos_weight
    embedding_weight[1] = lstm_eos_weight

    # 4.
    with h5py.File(hdf5_out, 'w') as fout:
        dset = fout.create_dataset('embedding', embedding_weight.shape, dtype='float32')
        dset[...] = embedding_weight


def sort_batch_by_length(batch: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Similar to AllenNLP.

    `batch` of shape `(batch_size, max_timesteps, *)` should be zero padded.
    `sequence_lengths` of shape `(batch_size,)`

    Returns (sorted_batch, restoration_index)
    """
    lengths = get_lengths_of_zero_padded_batch(batch)
    _, permutation_index = lengths.sort(0, descending=True)
    sorted_batch = batch.index_select(0, permutation_index)
    _, restoration_index = permutation_index.sort(0, descending=False)
    return sorted_batch, permutation_index, restoration_index

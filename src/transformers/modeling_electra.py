import logging
import os

import torch
import torch.nn as nn

from transformers import BertConfig, ElectraConfig
from transformers.activations import get_activation

from .modeling_bert import BertEmbeddings, BertEncoder, BertLayerNorm, BertPreTrainedModel


logger = logging.getLogger(__name__)


ELECTRA_PRETRAINED_MODEL_ARCHIVE_MAP = {
    "google/electra-small": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-small/pytorch_model.bin",
    "google/electra-base": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-base/pytorch_model.bin",
    "google/electra-large": "https://s3.amazonaws.com/models.huggingface.co/bert/google/electra-large/pytorch_model.bin",
}


def load_tf_weights_in_electra(model, config, tf_checkpoint_path):
    """ Load tf checkpoints in a pytorch model.
    """
    try:
        import re
        import numpy as np
        import tensorflow as tf
    except ImportError:
        logger.error(
            "Loading a TensorFlow model in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions."
        )
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    logger.info("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        logger.info("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)
    for name, array in zip(names, arrays):
        print(name, array.shape)
    for name, array in zip(names, arrays):
        original_name = name

        name = name.replace("electra/embeddings/", "embeddings/")
        name = name.replace("electra", "discriminator")
        name = name.replace("dense_1", "dense_prediction")
        # name = name.replace("discriminator/embeddings_project", "discriminator_embeddings_project")
        # name = name.replace("generator/embeddings_project", "generator_embeddings_project")
        name = name.replace("generator_predictions/output_bias", "bias")

        name = name.split("/")
        print(original_name, name)
        # adam_v and adam_m are variables used in AdamWeightDecayOptimizer to calculated m and v
        # which are not required for using pretrained model
        if any(
            n
            in [
                "adam_v",
                "adam_m",
                "AdamWeightDecayOptimizer",
                "AdamWeightDecayOptimizer_1",
                "global_step",
                "temperature",
            ]
            for n in name
        ):
            print("Skipping {}".format("/".join(name)))
            continue
        pointer = model
        for m_name in name:
            if re.fullmatch(r"[A-Za-z]+_\d+", m_name):
                scope_names = re.split(r"_(\d+)", m_name)
            else:
                scope_names = [m_name]
            if scope_names[0] == "kernel" or scope_names[0] == "gamma":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "output_bias" or scope_names[0] == "beta":
                pointer = getattr(pointer, "bias")
            elif scope_names[0] == "output_weights":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "squad":
                pointer = getattr(pointer, "classifier")
            else:
                try:
                    pointer = getattr(pointer, scope_names[0])
                except AttributeError:
                    print("Skipping {}".format("/".join(name)))
                    continue
            if len(scope_names) >= 2:
                num = int(scope_names[1])
                pointer = pointer[num]
        if m_name[-11:] == "_embeddings":
            pointer = getattr(pointer, "weight")
        elif m_name == "kernel":
            array = np.transpose(array)
        try:
            assert pointer.shape == array.shape, original_name
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        logger.info("Initialize PyTorch weight {}".format(name))
        pointer.data = torch.from_numpy(array)
    return model


class ElectraEmbeddings(BertEmbeddings):
    def __init__(self, config):
        super().__init__(config)
        self.word_embeddings = nn.Embedding(config.vocab_size, config.embedding_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.embedding_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.embedding_size)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = BertLayerNorm(config.embedding_size, eps=config.layer_norm_eps)

    def forward(self, input_ids=None, token_type_ids=None, position_ids=None, inputs_embeds=None):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]
        device = input_ids.device if input_ids is not None else inputs_embeds.device
        if position_ids is None:
            position_ids = torch.arange(seq_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).expand(input_shape)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = inputs_embeds + position_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class ElectraDiscriminatorPredictions(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.dense_prediction = nn.Linear(config.hidden_size, config.num_labels)
        self.config = config

    def forward(self, discriminator_hidden_states, attention_mask, labels):
        hidden_states = self.dense(discriminator_hidden_states)
        hidden_states = get_activation(self.config.hidden_act)(hidden_states)

        logits = self.dense_prediction(hidden_states).squeeze_()
        probs = torch.nn.Sigmoid()(logits)
        preds = torch.round((logits.sign() + 1) / 2)

        if self.config.num_classes == 2:
            loss_fct = nn.BCEWithLogitsLoss()
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, discriminator_hidden_states.shape[1]), labels.float())
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
        else:
            loss_fct = nn.CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))

        return probs, preds, loss


class ElectraGeneratorPredictions(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.LayerNorm = BertLayerNorm(config.embedding_size)
        self.dense = nn.Linear(config.hidden_size, config.embedding_size)

    def forward(self, generator_hidden_states):
        hidden_states = self.dense(generator_hidden_states)
        hidden_states = get_activation("gelu")(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)

        return hidden_states


class ElectraPreTrainedModel(BertPreTrainedModel):

    config_class = ElectraConfig
    # pretrained_model_archive_map = BERT_PRETRAINED_MODEL_ARCHIVE_MAP
    load_tf_weights = load_tf_weights_in_electra
    base_model_prefix = "electra"

    def get_extended_attention_mask(self, attention_mask, input_shape, device):
        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        if attention_mask.dim() == 3:
            extended_attention_mask = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            # Provided a padding mask of dimensions [batch_size, seq_length]
            # - if the model is a decoder, apply a causal mask in addition to the padding mask
            # - if the model is an encoder, make the mask broadcastable to [batch_size, num_heads, seq_length, seq_length]
            if self.config.is_decoder:
                batch_size, seq_length = input_shape
                seq_ids = torch.arange(seq_length, device=device)
                causal_mask = seq_ids[None, None, :].repeat(batch_size, seq_length, 1) <= seq_ids[None, :, None]
                causal_mask = causal_mask.to(
                    attention_mask.dtype
                )  # causal and attention masks must have same type with pytorch version < 1.3
                extended_attention_mask = causal_mask[:, None, :, :] * attention_mask[:, None, None, :]
            else:
                extended_attention_mask = attention_mask[:, None, None, :]
        else:
            raise ValueError(
                "Wrong shape for input_ids (shape {}) or attention_mask (shape {})".format(
                    input_shape, attention_mask.shape
                )
            )

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        return extended_attention_mask

    def get_head_mask(self, head_mask, config=None):
        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        num_hidden_layers = self.config.num_hidden_layers if config is None else config.num_hidden_layers
        if head_mask is not None:
            if head_mask.dim() == 1:
                head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
                head_mask = head_mask.expand(num_hidden_layers, -1, -1, -1, -1)
            elif head_mask.dim() == 2:
                head_mask = (
                    head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)
                )  # We can specify head_mask for each layer
            head_mask = head_mask.to(
                dtype=next(self.parameters()).dtype
            )  # switch to fload if need + fp16 compatibility
        else:
            head_mask = [None] * num_hidden_layers

        return head_mask

    @staticmethod
    def _gather_positions(sequence, positions):
        batch_size, sequence_length, dimension = sequence.shape
        position_shift = (sequence_length * torch.arange(batch_size)).unsqueeze(-1)
        flat_positions = torch.reshape(positions + position_shift, [-1]).long()
        flat_sequence = torch.reshape(sequence, [batch_size * sequence_length, dimension])
        gathered = flat_sequence.index_select(0, flat_positions)
        return torch.reshape(gathered, [batch_size, -1, dimension])


class ElectraForPreTraining(ElectraPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.discriminator_config = config.get_discriminator_config()
        self.generator_config = config.get_generator_config()

        self.embeddings = ElectraEmbeddings(self.discriminator_config)

        self.generator = ElectraTransformer(self.generator_config)
        self.generator_predictions = ElectraGeneratorPredictions(self.generator_config)

        self.discriminator = ElectraTransformer(self.discriminator_config)
        self.discriminator_predictions = ElectraDiscriminatorPredictions(self.discriminator_config)

        self.generator_lm_head = nn.Linear(config.embedding_size, config.vocab_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))
        self.generator_lm_head.bias = self.bias
        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def get_output_embeddings(self):
        return self.generator_lm_head

    def get_fake_data(self, input_ids, masked_lm_positions, mlm_logits, ignore_index=-100):
        sampled = mlm_logits.argmax(dim=-1)
        actual_ids = input_ids.gather(1, masked_lm_positions)
        incorrect_indices = (sampled != actual_ids).long()
        fake_ids = torch.zeros_like(input_ids)

        batch_dimensions = torch.arange(masked_lm_positions.shape[0])
        fake_ids[batch_dimensions, masked_lm_positions.T] = incorrect_indices.T

        return fake_ids


    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        discriminator_head_mask=None,
        generator_head_mask=None,
        inputs_embeds=None,
        masked_lm_labels=None,
        masked_lm_positions=None,
    ):

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape, device)
        discriminator_head_mask = self.get_head_mask(discriminator_head_mask, self.discriminator_config)
        generator_head_mask = self.get_head_mask(generator_head_mask, self.generator_config)

        embedding_output = self.embeddings(
            input_ids=input_ids, position_ids=position_ids, token_type_ids=token_type_ids, inputs_embeds=inputs_embeds
        )

        generator_hidden_states = self.generator(
            embedding_output, attention_mask=extended_attention_mask, head_mask=generator_head_mask
        )
        discriminator_hidden_states = self.discriminator(
            embedding_output, attention_mask=extended_attention_mask, head_mask=discriminator_head_mask
        )

        generator_sequence_output = generator_hidden_states[0]
        generator_pooled_output = generator_hidden_states[0][:, 0]
        discriminator_sequence_output = discriminator_hidden_states[0]

        output = (generator_sequence_output, generator_pooled_output, discriminator_sequence_output)

        # Masked language modeling softmax layer
        if masked_lm_positions is not None:
            # Gather only the relevant values in the indices that were masked
            relevant_hidden = self._gather_positions(generator_sequence_output, masked_lm_positions)

            assert len(masked_lm_labels.shape) == 2, "Masked LM labels should be of shape [batch_size, sequence_length]"
            relevant_masked_lm_labels = self._gather_positions(masked_lm_labels.unsqueeze(-1), masked_lm_positions)

            hidden_states = self.generator_predictions(relevant_hidden)

            # Project to the vocabulary
            hidden_states = self.generator_lm_head(hidden_states)

            # Compute logits, probabilities and predictions
            logits = hidden_states
            probs = torch.softmax(hidden_states, dim=-1)

            log_probs = torch.log_softmax(hidden_states, -1)
            # label_log_probs = -
            predictions = torch.argmax(log_probs, dim=-1)

            loss_fct = nn.CrossEntropyLoss()  # -100 index = padding token
            loss = loss_fct(logits.view(-1, self.config.vocab_size), relevant_masked_lm_labels.view(-1))
            output += (logits, probs, predictions, loss)

            fake_token_labels = self.get_fake_data(masked_lm_labels, masked_lm_positions, logits)
            probs, preds, loss = self.discriminator_predictions(
                discriminator_sequence_output, attention_mask, fake_token_labels
            )

            output += (probs, preds, loss)

        return output  # generator_sequence_output, generator_pooled_output, discriminator_sequence_output, (gen_logits, gen_probs, gen_preds, gen_loss) (discrim_probs, discrim_preds, discrim_loss)


class ElectraForMaskedLM(ElectraPreTrainedModel):
    def __init__(self, config):
        config = config.get_generator_config()
        super().__init__(config)

        self.embeddings = ElectraEmbeddings(config)
        self.generator = ElectraTransformer(config)
        self.generator_predictions = ElectraGeneratorPredictions(config)

        self.generator_lm_head = nn.Linear(config.embedding_size, config.vocab_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))
        self.generator_lm_head.bias = self.bias
        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def get_output_embeddings(self):
        return self.generator_lm_head

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.generator.encoder.layer[layer].attention.prune_heads(heads)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        masked_lm_labels=None,
    ):

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape, device)
        head_mask = self.get_head_mask(head_mask)

        embedding_output = self.embeddings(
            input_ids=input_ids, position_ids=position_ids, token_type_ids=token_type_ids, inputs_embeds=inputs_embeds
        )

        generator_hidden_states = self.generator(
            embedding_output, attention_mask=extended_attention_mask, head_mask=head_mask
        )

        generator_sequence_output = generator_hidden_states[0]

        prediction_scores = self.generator_predictions(generator_sequence_output)
        prediction_scores = self.generator_lm_head(prediction_scores)

        output = (prediction_scores, generator_sequence_output)

        # Masked language modeling softmax layer
        if masked_lm_labels is not None:
            loss_fct = nn.CrossEntropyLoss()  # -100 index = padding token
            loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            output = (loss,) + output

        output += generator_hidden_states[1:]

        return output  # (loss), prediction_scores, generator_sequence_output, (hidden_states), (attentions)


class ElectraForTokenClassification(ElectraPreTrainedModel):
    def __init__(self, config):
        config = config.get_discriminator_config()
        super().__init__(config)

        self.embeddings = ElectraEmbeddings(config)
        self.discriminator = ElectraTransformer(config)
        self.discriminator_predictions = ElectraDiscriminatorPredictions(config)
        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.discriminator.encoder.layer[layer].attention.prune_heads(heads)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        fake_token_labels=None,
    ):

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape, device)
        head_mask = self.get_head_mask(head_mask)

        embedding_output = self.embeddings(
            input_ids=input_ids, position_ids=position_ids, token_type_ids=token_type_ids, inputs_embeds=inputs_embeds
        )

        discriminator_hidden_states = self.discriminator(
            embedding_output, attention_mask=extended_attention_mask, head_mask=head_mask
        )

        discriminator_sequence_output = discriminator_hidden_states[0]

        output = (discriminator_sequence_output,)

        if fake_token_labels is not None:
            probs, preds, loss = self.discriminator_predictions(
                discriminator_sequence_output, attention_mask, fake_token_labels
            )

            output += (probs, preds, loss)

        output += discriminator_hidden_states[1:]

        return output  # (probs, preds, loss), discriminator_hidden_states


class ElectraTransformer(ElectraPreTrainedModel):

    config_class = BertConfig

    def __init__(self, config):
        super().__init__(config)

        self.embeddings_project = nn.Linear(config.embedding_size, config.hidden_size)
        self.encoder = BertEncoder(config)
        self.config = config

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        if hidden_states.shape[-1] != self.config.hidden_size:
            hidden_states = self.embeddings_project(hidden_states)

        hidden_states = self.encoder(hidden_states, attention_mask=attention_mask, head_mask=head_mask)

        return hidden_states

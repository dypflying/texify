import os
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1" # For some reason, transformers decided to use .isin for a simple op, which is not supported on MPS

from torch import nn
import torch
from typing import Optional, Tuple

from transformers import AutoModel, VisionEncoderDecoderModel, GenerationMixin, PretrainedConfig, PreTrainedModel, VisionEncoderDecoderConfig, AutoModelForCausalLM
from transformers.models.donut.modeling_donut_swin import DonutSwinPatchEmbeddings, DonutSwinEmbeddings, DonutSwinModel, \
    DonutSwinEncoder

from texify.model.config import VariableDonutSwinConfig, get_config
from texify.settings import settings

class GenerateVisionEncoderDecoderModel(VisionEncoderDecoderModel, GenerationMixin):
    def __init__(
        self,
        config: Optional[PretrainedConfig] = None,
        encoder: Optional[PreTrainedModel] = None,
        decoder: Optional[PreTrainedModel] = None,
    ):
        config.tie_word_embeddings = False
        PreTrainedModel.__init__(self, config)

        if encoder is None:
            encoder = AutoModel.from_config(config.encoder)

        if decoder is None:
            decoder = AutoModelForCausalLM.from_config(config.decoder)

        self.encoder = encoder
        self.decoder = decoder

        # make sure that the individual model's config refers to the shared config
        # so that the updates to the config will be synced
        self.config.encoder._attn_implementation = self.encoder.config._attn_implementation
        self.config.decoder._attn_implementation = self.decoder.config._attn_implementation
        self.encoder.config = self.config.encoder
        self.decoder.config = self.config.decoder

        # encoder outputs might need to be projected to different dimension for decoder
        if (
            self.encoder.config.hidden_size != self.decoder.config.hidden_size
            and self.decoder.config.cross_attention_hidden_size is None
        ):
            self.enc_to_dec_proj = nn.Linear(self.encoder.config.hidden_size, self.decoder.config.hidden_size)


def load_model(checkpoint=settings.MODEL_CHECKPOINT, device=settings.TORCH_DEVICE_MODEL, dtype=settings.MODEL_DTYPE):
    config = get_config(checkpoint)
    AutoModel.register(VariableDonutSwinConfig, VariableDonutSwinModel)


    model = GenerateVisionEncoderDecoderModel.from_pretrained(checkpoint, config=config, torch_dtype=dtype)
    model = model.to(device)
    model = model.eval()
    print(f"Loaded texify model to {device} with {dtype} dtype")
    return model


class VariableDonutSwinEmbeddings(DonutSwinEmbeddings):
    """
    Construct the patch and position embeddings. Optionally, also the mask token.
    """

    def __init__(self, config, use_mask_token=False, **kwargs):
        super().__init__(config, use_mask_token)

        self.patch_embeddings = DonutSwinPatchEmbeddings(config)
        num_patches = self.patch_embeddings.num_patches
        self.patch_grid = self.patch_embeddings.grid_size
        self.mask_token = nn.Parameter(torch.zeros(1, 1, config.embed_dim)) if use_mask_token else None
        self.position_embeddings = None

        if config.use_absolute_embeddings:
            self.position_embeddings = nn.Parameter(torch.zeros(1, num_patches + 1, config.embed_dim))

        self.row_embeddings = None
        self.column_embeddings = None
        if hasattr(config, "use_2d_embeddings") and config.use_2d_embeddings:
            self.row_embeddings = nn.Parameter(torch.zeros(1, self.patch_grid[0] + 1, config.embed_dim))
            self.column_embeddings = nn.Parameter(torch.zeros(1, self.patch_grid[1] + 1, config.embed_dim))

        self.norm = nn.LayerNorm(config.embed_dim)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(
        self, pixel_values: Optional[torch.FloatTensor], bool_masked_pos: Optional[torch.BoolTensor] = None, **kwargs
    ) -> Tuple[torch.Tensor]:

        embeddings, output_dimensions = self.patch_embeddings(pixel_values)
        # Layernorm across the last dimension (each patch is a single row)
        embeddings = self.norm(embeddings)
        batch_size, seq_len, embed_dim = embeddings.size()

        if bool_masked_pos is not None:
            mask_tokens = self.mask_token.expand(batch_size, seq_len, -1)
            # replace the masked visual tokens by mask_tokens
            mask = bool_masked_pos.unsqueeze(-1).type_as(mask_tokens)
            embeddings = embeddings * (1.0 - mask) + mask_tokens * mask

        if self.position_embeddings is not None:
            embeddings = embeddings + self.position_embeddings[:, :seq_len, :]

        if self.row_embeddings is not None and self.column_embeddings is not None:
            # Repeat the x position embeddings across the y axis like 0, 1, 2, 3, 0, 1, 2, 3, ...
            row_embeddings = self.row_embeddings[:, :output_dimensions[0], :].repeat_interleave(output_dimensions[1], dim=1)
            column_embeddings = self.column_embeddings[:, :output_dimensions[1], :].repeat(1, output_dimensions[0], 1)

            embeddings = embeddings + row_embeddings + column_embeddings

        embeddings = self.dropout(embeddings)

        return embeddings, output_dimensions


class VariableDonutSwinModel(DonutSwinModel):
    config_class = VariableDonutSwinConfig

    def __init__(self, config, add_pooling_layer=True, use_mask_token=False, **kwargs):
        super().__init__(config)
        self.config = config
        self.num_layers = len(config.depths)
        self.num_features = int(config.embed_dim * 2 ** (self.num_layers - 1))

        self.embeddings = VariableDonutSwinEmbeddings(config, use_mask_token=use_mask_token)
        self.encoder = DonutSwinEncoder(config, self.embeddings.patch_grid)

        self.pooler = nn.AdaptiveAvgPool1d(1) if add_pooling_layer else None

        # Initialize weights and apply final processing
        self.post_init()


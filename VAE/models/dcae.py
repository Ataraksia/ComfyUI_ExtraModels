# Copyright 2024 MIT, Tsinghua University, NVIDIA CORPORATION and The HuggingFace Team.
# All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numbers
from typing import Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from diffusers import (
    ModelMixin, 
    ConfigMixin, 
    FromOriginalModelMixin, 
)


ACTIVATION_FUNCTIONS = {
    "swish": nn.SiLU(),
    "silu": nn.SiLU(),
    "mish": nn.Mish(),
    "gelu": nn.GELU(),
    "relu": nn.ReLU(),
}


def get_activation(act_fn: str) -> nn.Module:
    """Helper function to get activation function from string.

    Args:
        act_fn (str): Name of activation function.

    Returns:
        nn.Module: Activation function.
    """

    act_fn = act_fn.lower()
    if act_fn in ACTIVATION_FUNCTIONS:
        return ACTIVATION_FUNCTIONS[act_fn]
    else:
        raise ValueError(f"Unsupported activation function: {act_fn}")


def get_normalization(
    norm_type: str = "batch_norm",
    num_features: Optional[int] = None,
    eps: float = 1e-5,
    elementwise_affine: bool = True,
    bias: bool = True,
) -> nn.Module:
    if norm_type == "rms_norm":
        norm = RMSNorm(num_features, eps=eps, elementwise_affine=elementwise_affine, bias=bias)
    elif norm_type == "layer_norm":
        norm = nn.LayerNorm(num_features, eps=eps, elementwise_affine=elementwise_affine, bias=bias)
    elif norm_type == "batch_norm":
        norm = nn.BatchNorm2d(num_features, eps=eps, affine=elementwise_affine)
    else:
        raise ValueError(f"{norm_type=} is not supported.")
    return norm


class RMSNorm(nn.Module):
    def __init__(self, dim, eps: float, elementwise_affine: bool = True, bias: bool = False):
        super().__init__()

        self.eps = eps
        self.elementwise_affine = elementwise_affine

        if isinstance(dim, numbers.Integral):
            dim = (dim,)

        self.dim = torch.Size(dim)

        self.weight = None
        self.bias = None

        if elementwise_affine:
            self.weight = nn.Parameter(torch.ones(dim))
            if bias:
                self.bias = nn.Parameter(torch.zeros(dim))

    def forward(self, hidden_states):
        input_dtype = hidden_states.dtype
        variance = hidden_states.to(torch.float32).pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.eps)

        if self.weight is not None:
            # convert into half-precision if necessary
            if self.weight.dtype in [torch.float16, torch.bfloat16]:
                hidden_states = hidden_states.to(self.weight.dtype)
            hidden_states = hidden_states * self.weight
            if self.bias is not None:
                hidden_states = hidden_states + self.bias
        else:
            hidden_states = hidden_states.to(input_dtype)

        return hidden_states


class SanaMultiscaleLinearAttention(nn.Module):
    r"""Lightweight multi-scale linear attention"""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        num_attention_heads: Optional[int] = None,
        attention_head_dim: int = 8,
        mult: float = 1.0,
        norm_type: str = "batch_norm",
        kernel_sizes: Tuple[int, ...] = (5,),
        eps: float = 1e-15,
        residual_connection: bool = False,
    ):
        super().__init__()

        self.eps = eps
        self.attention_head_dim = attention_head_dim
        self.norm_type = norm_type
        self.residual_connection = residual_connection

        num_attention_heads = (
            int(in_channels // attention_head_dim * mult) if num_attention_heads is None else num_attention_heads
        )
        inner_dim = num_attention_heads * attention_head_dim

        self.to_q = nn.Linear(in_channels, inner_dim, bias=False)
        self.to_k = nn.Linear(in_channels, inner_dim, bias=False)
        self.to_v = nn.Linear(in_channels, inner_dim, bias=False)

        self.to_qkv_multiscale = nn.ModuleList()
        for kernel_size in kernel_sizes:
            self.to_qkv_multiscale.append(
                SanaMultiscaleAttentionProjection(inner_dim, num_attention_heads, kernel_size)
            )

        self.nonlinearity = nn.ReLU()
        self.to_out = nn.Linear(inner_dim * (1 + len(kernel_sizes)), out_channels, bias=False)
        self.norm_out = get_normalization(norm_type, num_features=out_channels)

        self.processor = SanaMultiscaleAttnProcessor2_0()

    def apply_linear_attention(self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor) -> torch.Tensor:
        value = F.pad(value, (0, 0, 0, 1), mode="constant", value=1)  # Adds padding
        scores = torch.matmul(value, key.transpose(-1, -2))
        hidden_states = torch.matmul(scores, query)

        hidden_states = hidden_states.to(dtype=torch.float32)
        hidden_states = hidden_states[:, :, :-1] / (hidden_states[:, :, -1:] + self.eps)
        return hidden_states

    def apply_quadratic_attention(self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor) -> torch.Tensor:
        scores = torch.matmul(key.transpose(-1, -2), query)
        scores = scores.to(dtype=torch.float32)
        scores = scores / (torch.sum(scores, dim=2, keepdim=True) + self.eps)
        hidden_states = torch.matmul(value, scores)
        return hidden_states

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        return self.processor(self, hidden_states)


class SanaMultiscaleAttnProcessor2_0:
    r"""
    Processor for implementing multiscale quadratic attention.
    """

    def __call__(self, attn: SanaMultiscaleLinearAttention, hidden_states: torch.Tensor) -> torch.Tensor:
        height, width = hidden_states.shape[-2:]
        if height * width > attn.attention_head_dim:
            use_linear_attention = True
        else:
            use_linear_attention = False

        residual = hidden_states

        batch_size, _, height, width = list(hidden_states.size())
        original_dtype = hidden_states.dtype

        hidden_states = hidden_states.movedim(1, -1)
        query = attn.to_q(hidden_states)
        key = attn.to_k(hidden_states)
        value = attn.to_v(hidden_states)
        hidden_states = torch.cat([query, key, value], dim=3)
        hidden_states = hidden_states.movedim(-1, 1)

        multi_scale_qkv = [hidden_states]
        for block in attn.to_qkv_multiscale:
            multi_scale_qkv.append(block(hidden_states))

        hidden_states = torch.cat(multi_scale_qkv, dim=1)

        if use_linear_attention:
            # for linear attention upcast hidden_states to float32
            hidden_states = hidden_states.to(dtype=torch.float32)

        hidden_states = hidden_states.reshape(batch_size, -1, 3 * attn.attention_head_dim, height * width)

        query, key, value = hidden_states.chunk(3, dim=2)
        query = attn.nonlinearity(query)
        key = attn.nonlinearity(key)

        if use_linear_attention:
            hidden_states = attn.apply_linear_attention(query, key, value)
            hidden_states = hidden_states.to(dtype=original_dtype)
        else:
            hidden_states = attn.apply_quadratic_attention(query, key, value)

        hidden_states = torch.reshape(hidden_states, (batch_size, -1, height, width))
        hidden_states = attn.to_out(hidden_states.movedim(1, -1)).movedim(-1, 1)

        if attn.norm_type == "rms_norm":
            hidden_states = attn.norm_out(hidden_states.movedim(1, -1)).movedim(-1, 1)
        else:
            hidden_states = attn.norm_out(hidden_states)

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        return hidden_states


class SanaMultiscaleAttentionProjection(nn.Module):
    def __init__(
        self,
        in_channels: int,
        num_attention_heads: int,
        kernel_size: int,
    ) -> None:
        super().__init__()

        channels = 3 * in_channels
        self.proj_in = nn.Conv2d(
            channels,
            channels,
            kernel_size,
            padding=kernel_size // 2,
            groups=channels,
            bias=False,
        )
        self.proj_out = nn.Conv2d(channels, channels, 1, 1, 0, groups=3 * num_attention_heads, bias=False)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.proj_in(hidden_states)
        hidden_states = self.proj_out(hidden_states)
        return hidden_states


class GLUMBConv(nn.Module):
    def __init__(self, in_channels: int, out_channels: int) -> None:
        super().__init__()

        hidden_channels = 4 * in_channels

        self.nonlinearity = nn.SiLU()

        self.conv_inverted = nn.Conv2d(in_channels, hidden_channels * 2, 1, 1, 0)
        self.conv_depth = nn.Conv2d(hidden_channels * 2, hidden_channels * 2, 3, 1, 1, groups=hidden_channels * 2)
        self.conv_point = nn.Conv2d(hidden_channels, out_channels, 1, 1, 0, bias=False)
        self.norm = RMSNorm(out_channels, eps=1e-5, elementwise_affine=True, bias=True)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        residual = hidden_states

        hidden_states = self.conv_inverted(hidden_states)
        hidden_states = self.nonlinearity(hidden_states)

        hidden_states = self.conv_depth(hidden_states)
        hidden_states, gate = torch.chunk(hidden_states, 2, dim=1)
        hidden_states = hidden_states * self.nonlinearity(gate)

        hidden_states = self.conv_point(hidden_states)
        # move channel to the last dimension so we apply RMSnorm across channel dimension
        hidden_states = self.norm(hidden_states.movedim(1, -1)).movedim(-1, 1)

        return hidden_states + residual


class ResBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        norm_type: str = "batch_norm",
        act_fn: str = "relu6",
    ) -> None:
        super().__init__()

        self.norm_type = norm_type

        self.nonlinearity = get_activation(act_fn) if act_fn is not None else nn.Identity()
        self.conv1 = nn.Conv2d(in_channels, in_channels, 3, 1, 1)
        self.conv2 = nn.Conv2d(in_channels, out_channels, 3, 1, 1, bias=False)
        self.norm = get_normalization(norm_type, out_channels)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        residual = hidden_states
        hidden_states = self.conv1(hidden_states)
        hidden_states = self.nonlinearity(hidden_states)
        hidden_states = self.conv2(hidden_states)

        if self.norm_type == "rms_norm":
            # move channel to the last dimension so we apply RMSnorm across channel dimension
            hidden_states = self.norm(hidden_states.movedim(1, -1)).movedim(-1, 1)
        else:
            hidden_states = self.norm(hidden_states)

        return hidden_states + residual


class EfficientViTBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        mult: float = 1.0,
        attention_head_dim: int = 32,
        qkv_multiscales: Tuple[int, ...] = (5,),
        norm_type: str = "batch_norm",
    ) -> None:
        super().__init__()

        self.attn = SanaMultiscaleLinearAttention(
            in_channels=in_channels,
            out_channels=in_channels,
            mult=mult,
            attention_head_dim=attention_head_dim,
            norm_type=norm_type,
            kernel_sizes=qkv_multiscales,
            residual_connection=True,
        )

        self.conv_out = GLUMBConv(
            in_channels=in_channels,
            out_channels=in_channels,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.attn(x)
        x = self.conv_out(x)
        return x


def get_block(
    block_type: str,
    in_channels: int,
    out_channels: int,
    attention_head_dim: int,
    norm_type: str,
    act_fn: str,
    qkv_mutliscales: Tuple[int] = (),
):
    if block_type == "ResBlock":
        block = ResBlock(in_channels, out_channels, norm_type, act_fn)

    elif block_type == "EfficientViTBlock":
        block = EfficientViTBlock(
            in_channels, attention_head_dim=attention_head_dim, norm_type=norm_type, qkv_multiscales=qkv_mutliscales
        )

    else:
        raise ValueError(f"Block with {block_type=} is not supported.")

    return block


class DCDownBlock2d(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, downsample: bool = False, shortcut: bool = True) -> None:
        super().__init__()

        self.downsample = downsample
        self.factor = 2
        self.stride = 1 if downsample else 2
        self.group_size = in_channels * self.factor**2 // out_channels
        self.shortcut = shortcut

        out_ratio = self.factor**2
        if downsample:
            assert out_channels % out_ratio == 0
            out_channels = out_channels // out_ratio

        self.conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=self.stride,
            padding=1,
        )

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        x = self.conv(hidden_states)
        if self.downsample:
            x = F.pixel_unshuffle(x, self.factor)

        if self.shortcut:
            y = F.pixel_unshuffle(hidden_states, self.factor)
            y = y.unflatten(1, (-1, self.group_size))
            y = y.mean(dim=2)
            hidden_states = x + y
        else:
            hidden_states = x

        return hidden_states


class DCUpBlock2d(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        interpolate: bool = False,
        shortcut: bool = True,
        interpolation_mode: str = "nearest",
    ) -> None:
        super().__init__()

        self.interpolate = interpolate
        self.interpolation_mode = interpolation_mode
        self.shortcut = shortcut
        self.factor = 2
        self.repeats = out_channels * self.factor**2 // in_channels

        out_ratio = self.factor**2

        if not interpolate:
            out_channels = out_channels * out_ratio

        self.conv = nn.Conv2d(in_channels, out_channels, 3, 1, 1)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        if self.interpolate:
            x = F.interpolate(hidden_states, scale_factor=self.factor, mode=self.interpolation_mode)
            x = self.conv(x)
        else:
            x = self.conv(hidden_states)
            x = F.pixel_shuffle(x, self.factor)

        if self.shortcut:
            y = hidden_states.repeat_interleave(self.repeats, dim=1)
            y = F.pixel_shuffle(y, self.factor)
            hidden_states = x + y
        else:
            hidden_states = x

        return hidden_states


class Encoder(nn.Module):
    def __init__(
        self,
        in_channels: int,
        latent_channels: int,
        attention_head_dim: int = 32,
        block_type: Union[str, Tuple[str]] = "ResBlock",
        block_out_channels: Tuple[int] = (128, 256, 512, 512, 1024, 1024),
        layers_per_block: Tuple[int] = (2, 2, 2, 2, 2, 2),
        qkv_multiscales: Tuple[Tuple[int, ...], ...] = ((), (), (), (5,), (5,), (5,)),
        downsample_block_type: str = "pixel_unshuffle",
        out_shortcut: bool = True,
    ):
        super().__init__()

        num_blocks = len(block_out_channels)

        if isinstance(block_type, str):
            block_type = (block_type,) * num_blocks

        if layers_per_block[0] > 0:
            self.conv_in = nn.Conv2d(
                in_channels,
                block_out_channels[0] if layers_per_block[0] > 0 else block_out_channels[1],
                kernel_size=3,
                stride=1,
                padding=1,
            )
        else:
            self.conv_in = DCDownBlock2d(
                in_channels=in_channels,
                out_channels=block_out_channels[0] if layers_per_block[0] > 0 else block_out_channels[1],
                downsample=downsample_block_type == "pixel_unshuffle",
                shortcut=False,
            )

        down_blocks = []
        for i, (out_channel, num_layers) in enumerate(zip(block_out_channels, layers_per_block)):
            down_block_list = []

            for _ in range(num_layers):
                block = get_block(
                    block_type[i],
                    out_channel,
                    out_channel,
                    attention_head_dim=attention_head_dim,
                    norm_type="rms_norm",
                    act_fn="silu",
                    qkv_mutliscales=qkv_multiscales[i],
                )
                down_block_list.append(block)

            if i < num_blocks - 1 and num_layers > 0:
                downsample_block = DCDownBlock2d(
                    in_channels=out_channel,
                    out_channels=block_out_channels[i + 1],
                    downsample=downsample_block_type == "pixel_unshuffle",
                    shortcut=True,
                )
                down_block_list.append(downsample_block)

            down_blocks.append(nn.Sequential(*down_block_list))

        self.down_blocks = nn.ModuleList(down_blocks)

        self.conv_out = nn.Conv2d(block_out_channels[-1], latent_channels, 3, 1, 1)

        self.out_shortcut = out_shortcut
        if out_shortcut:
            self.out_shortcut_average_group_size = block_out_channels[-1] // latent_channels

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.conv_in(hidden_states)
        for down_block in self.down_blocks:
            hidden_states = down_block(hidden_states)

        if self.out_shortcut:
            x = hidden_states.unflatten(1, (-1, self.out_shortcut_average_group_size))
            x = x.mean(dim=2)
            hidden_states = self.conv_out(hidden_states) + x
        else:
            hidden_states = self.conv_out(hidden_states)

        return hidden_states


class Decoder(nn.Module):
    def __init__(
        self,
        in_channels: int,
        latent_channels: int,
        attention_head_dim: int = 32,
        block_type: Union[str, Tuple[str]] = "ResBlock",
        block_out_channels: Tuple[int] = (128, 256, 512, 512, 1024, 1024),
        layers_per_block: Tuple[int] = (2, 2, 2, 2, 2, 2),
        qkv_multiscales: Tuple[Tuple[int, ...], ...] = ((), (), (), (5,), (5,), (5,)),
        norm_type: Union[str, Tuple[str]] = "rms_norm",
        act_fn: Union[str, Tuple[str]] = "silu",
        upsample_block_type: str = "pixel_shuffle",
        in_shortcut: bool = True,
    ):
        super().__init__()

        num_blocks = len(block_out_channels)

        if isinstance(block_type, str):
            block_type = (block_type,) * num_blocks
        if isinstance(norm_type, str):
            norm_type = (norm_type,) * num_blocks
        if isinstance(act_fn, str):
            act_fn = (act_fn,) * num_blocks

        self.conv_in = nn.Conv2d(latent_channels, block_out_channels[-1], 3, 1, 1)

        self.in_shortcut = in_shortcut
        if in_shortcut:
            self.in_shortcut_repeats = block_out_channels[-1] // latent_channels

        up_blocks = []
        for i, (out_channel, num_layers) in reversed(list(enumerate(zip(block_out_channels, layers_per_block)))):
            up_block_list = []

            if i < num_blocks - 1 and num_layers > 0:
                upsample_block = DCUpBlock2d(
                    block_out_channels[i + 1],
                    out_channel,
                    interpolate=upsample_block_type == "interpolate",
                    shortcut=True,
                )
                up_block_list.append(upsample_block)

            for _ in range(num_layers):
                block = get_block(
                    block_type[i],
                    out_channel,
                    out_channel,
                    attention_head_dim=attention_head_dim,
                    norm_type=norm_type[i],
                    act_fn=act_fn[i],
                    qkv_mutliscales=qkv_multiscales[i],
                )
                up_block_list.append(block)

            up_blocks.insert(0, nn.Sequential(*up_block_list))

        self.up_blocks = nn.ModuleList(up_blocks)

        channels = block_out_channels[0] if layers_per_block[0] > 0 else block_out_channels[1]

        self.norm_out = RMSNorm(channels, 1e-5, elementwise_affine=True, bias=True)
        self.conv_act = nn.ReLU()
        self.conv_out = None

        if layers_per_block[0] > 0:
            self.conv_out = nn.Conv2d(channels, in_channels, 3, 1, 1)
        else:
            self.conv_out = DCUpBlock2d(
                channels, in_channels, interpolate=upsample_block_type == "interpolate", shortcut=False
            )

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        if self.in_shortcut:
            x = hidden_states.repeat_interleave(self.in_shortcut_repeats, dim=1)
            hidden_states = self.conv_in(hidden_states) + x
        else:
            hidden_states = self.conv_in(hidden_states)

        for up_block in reversed(self.up_blocks):
            hidden_states = up_block(hidden_states)

        hidden_states = self.norm_out(hidden_states.movedim(1, -1)).movedim(-1, 1)
        hidden_states = self.conv_act(hidden_states)
        hidden_states = self.conv_out(hidden_states)
        return hidden_states


class AutoencoderDC(ModelMixin, ConfigMixin, FromOriginalModelMixin):
    r"""
    An Autoencoder model introduced in [DCAE](https://arxiv.org/abs/2410.10733) and used in
    [SANA](https://arxiv.org/abs/2410.10629).

    This model inherits from [`ModelMixin`]. Check the superclass documentation for it's generic methods implemented
    for all models (such as downloading or saving).

    Args:
        in_channels (`int`, defaults to `3`):
            The number of input channels in samples.
        latent_channels (`int`, defaults to `32`):
            The number of channels in the latent space representation.
        encoder_block_types (`Union[str, Tuple[str]]`, defaults to `"ResBlock"`):
            The type(s) of block to use in the encoder.
        decoder_block_types (`Union[str, Tuple[str]]`, defaults to `"ResBlock"`):
            The type(s) of block to use in the decoder.
        encoder_block_out_channels (`Tuple[int, ...]`, defaults to `(128, 256, 512, 512, 1024, 1024)`):
            The number of output channels for each block in the encoder.
        decoder_block_out_channels (`Tuple[int, ...]`, defaults to `(128, 256, 512, 512, 1024, 1024)`):
            The number of output channels for each block in the decoder.
        encoder_layers_per_block (`Tuple[int]`, defaults to `(2, 2, 2, 3, 3, 3)`):
            The number of layers per block in the encoder.
        decoder_layers_per_block (`Tuple[int]`, defaults to `(3, 3, 3, 3, 3, 3)`):
            The number of layers per block in the decoder.
        encoder_qkv_multiscales (`Tuple[Tuple[int, ...], ...]`, defaults to `((), (), (), (5,), (5,), (5,))`):
            Multi-scale configurations for the encoder's QKV (query-key-value) transformations.
        decoder_qkv_multiscales (`Tuple[Tuple[int, ...], ...]`, defaults to `((), (), (), (5,), (5,), (5,))`):
            Multi-scale configurations for the decoder's QKV (query-key-value) transformations.
        upsample_block_type (`str`, defaults to `"pixel_shuffle"`):
            The type of block to use for upsampling in the decoder.
        downsample_block_type (`str`, defaults to `"pixel_unshuffle"`):
            The type of block to use for downsampling in the encoder.
        decoder_norm_types (`Union[str, Tuple[str]]`, defaults to `"rms_norm"`):
            The normalization type(s) to use in the decoder.
        decoder_act_fns (`Union[str, Tuple[str]]`, defaults to `"silu"`):
            The activation function(s) to use in the decoder.
        scaling_factor (`float`, defaults to `1.0`):
            The multiplicative inverse of the root mean square of the latent features. This is used to scale the latent
            space to have unit variance when training the diffusion model. The latents are scaled with the formula `z =
            z * scaling_factor` before being passed to the diffusion model. When decoding, the latents are scaled back
            to the original scale with the formula: `z = 1 / scaling_factor * z`.
    """

    _supports_gradient_checkpointing = False

    def __init__(
        self,
        in_channels: int = 3,
        latent_channels: int = 32,
        attention_head_dim: int = 32,
        encoder_block_types: Union[str, Tuple[str]] = "ResBlock",
        decoder_block_types: Union[str, Tuple[str]] = "ResBlock",
        encoder_block_out_channels: Tuple[int, ...] = (128, 256, 512, 512, 1024, 1024),
        decoder_block_out_channels: Tuple[int, ...] = (128, 256, 512, 512, 1024, 1024),
        encoder_layers_per_block: Tuple[int] = (2, 2, 2, 3, 3, 3),
        decoder_layers_per_block: Tuple[int] = (3, 3, 3, 3, 3, 3),
        encoder_qkv_multiscales: Tuple[Tuple[int, ...], ...] = ((), (), (), (5,), (5,), (5,)),
        decoder_qkv_multiscales: Tuple[Tuple[int, ...], ...] = ((), (), (), (5,), (5,), (5,)),
        upsample_block_type: str = "pixel_shuffle",
        downsample_block_type: str = "pixel_unshuffle",
        decoder_norm_types: Union[str, Tuple[str]] = "rms_norm",
        decoder_act_fns: Union[str, Tuple[str]] = "silu",
        scaling_factor: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__()

        self.encoder = Encoder(
            in_channels=in_channels,
            latent_channels=latent_channels,
            attention_head_dim=attention_head_dim,
            block_type=encoder_block_types,
            block_out_channels=encoder_block_out_channels,
            layers_per_block=encoder_layers_per_block,
            qkv_multiscales=encoder_qkv_multiscales,
            downsample_block_type=downsample_block_type,
        )
        self.decoder = Decoder(
            in_channels=in_channels,
            latent_channels=latent_channels,
            attention_head_dim=attention_head_dim,
            block_type=decoder_block_types,
            block_out_channels=decoder_block_out_channels,
            layers_per_block=decoder_layers_per_block,
            qkv_multiscales=decoder_qkv_multiscales,
            norm_type=decoder_norm_types,
            act_fn=decoder_act_fns,
            upsample_block_type=upsample_block_type,
        )

        self.spatial_compression_ratio = 2 ** (len(encoder_block_out_channels) - 1)
        self.temporal_compression_ratio = 1

        # When decoding a batch of video latents at a time, one can save memory by slicing across the batch dimension
        # to perform decoding of a single video latent at a time.
        self.use_slicing = False

        # When decoding spatially large video latents, the memory requirement is very high. By breaking the video latent
        # frames spatially into smaller tiles and performing multiple forward passes for decoding, and then blending the
        # intermediate tiles together, the memory requirement can be lowered.
        self.use_tiling = False

        # The minimal tile height and width for spatial tiling to be used
        self.tile_sample_min_height = 512
        self.tile_sample_min_width = 512

        # The minimal distance between two spatial tiles
        self.tile_sample_stride_height = 448
        self.tile_sample_stride_width = 448

    def enable_tiling(
        self,
        tile_sample_min_height: Optional[int] = None,
        tile_sample_min_width: Optional[int] = None,
        tile_sample_stride_height: Optional[float] = None,
        tile_sample_stride_width: Optional[float] = None,
    ) -> None:
        r"""
        Enable tiled AE decoding. When this option is enabled, the AE will split the input tensor into tiles to compute
        decoding and encoding in several steps. This is useful for saving a large amount of memory and to allow
        processing larger images.

        Args:
            tile_sample_min_height (`int`, *optional*):
                The minimum height required for a sample to be separated into tiles across the height dimension.
            tile_sample_min_width (`int`, *optional*):
                The minimum width required for a sample to be separated into tiles across the width dimension.
            tile_sample_stride_height (`int`, *optional*):
                The minimum amount of overlap between two consecutive vertical tiles. This is to ensure that there are
                no tiling artifacts produced across the height dimension.
            tile_sample_stride_width (`int`, *optional*):
                The stride between two consecutive horizontal tiles. This is to ensure that there are no tiling
                artifacts produced across the width dimension.
        """
        self.use_tiling = True
        self.tile_sample_min_height = tile_sample_min_height or self.tile_sample_min_height
        self.tile_sample_min_width = tile_sample_min_width or self.tile_sample_min_width
        self.tile_sample_stride_height = tile_sample_stride_height or self.tile_sample_stride_height
        self.tile_sample_stride_width = tile_sample_stride_width or self.tile_sample_stride_width

    def disable_tiling(self) -> None:
        r"""
        Disable tiled AE decoding. If `enable_tiling` was previously enabled, this method will go back to computing
        decoding in one step.
        """
        self.use_tiling = False

    def enable_slicing(self) -> None:
        r"""
        Enable sliced AE decoding. When this option is enabled, the AE will split the input tensor in slices to compute
        decoding in several steps. This is useful to save some memory and allow larger batch sizes.
        """
        self.use_slicing = True

    def disable_slicing(self) -> None:
        r"""
        Disable sliced AE decoding. If `enable_slicing` was previously enabled, this method will go back to computing
        decoding in one step.
        """
        self.use_slicing = False

    def _encode(self, x: torch.Tensor) -> torch.Tensor:
        batch_size, num_channels, height, width = x.shape

        if self.use_tiling and (width > self.tile_sample_min_width or height > self.tile_sample_min_height):
            return self.tiled_encode(x, return_dict=False)[0]

        encoded = self.encoder(x)

        return encoded

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        r"""
        Encode a batch of images into latents.

        Args:
            x (`torch.Tensor`): Input batch of images.

        """
        if self.use_slicing and x.shape[0] > 1:
            encoded_slices = [self._encode(x_slice) for x_slice in x.split(1)]
            encoded = torch.cat(encoded_slices)
        else:
            encoded = self._encode(x)

        return encoded

    def _decode(self, z: torch.Tensor) -> torch.Tensor:
        batch_size, num_channels, height, width = z.shape

        if self.use_tiling and (width > self.tile_latent_min_width or height > self.tile_latent_min_height):
            return self.tiled_decode(z, return_dict=False)[0]

        decoded = self.decoder(z)

        return decoded

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        r"""
        Decode a batch of images.

        Args:
            z (`torch.Tensor`): Input batch of latent vectors.

        """
        if self.use_slicing and z.size(0) > 1:
            decoded_slices = [self._decode(z_slice).sample for z_slice in z.split(1)]
            decoded = torch.cat(decoded_slices)
        else:
            decoded = self._decode(z)

        return decoded

    def forward(self, sample: torch.Tensor) -> torch.Tensor:
        encoded = self.encode(sample)
        decoded = self.decode(encoded)
        return decoded, torch.tensor(0), {}
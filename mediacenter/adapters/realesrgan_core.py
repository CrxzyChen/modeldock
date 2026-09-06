"""Real-ESRGAN network and tiled inference primitives."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn
from torch.nn import functional as F


class ResidualDenseBlock(nn.Module):
    def __init__(self, channels: int = 64, growth: int = 32):
        super().__init__()
        self.conv1 = nn.Conv2d(channels, growth, 3, 1, 1)
        self.conv2 = nn.Conv2d(channels + growth, growth, 3, 1, 1)
        self.conv3 = nn.Conv2d(channels + growth * 2, growth, 3, 1, 1)
        self.conv4 = nn.Conv2d(channels + growth * 3, growth, 3, 1, 1)
        self.conv5 = nn.Conv2d(channels + growth * 4, channels, 3, 1, 1)

    def forward(self, value):
        first = F.leaky_relu(self.conv1(value), 0.2, inplace=True)
        second = F.leaky_relu(self.conv2(torch.cat((value, first), 1)), 0.2, inplace=True)
        third = F.leaky_relu(self.conv3(torch.cat((value, first, second), 1)), 0.2, inplace=True)
        fourth = F.leaky_relu(self.conv4(torch.cat((value, first, second, third), 1)), 0.2, inplace=True)
        return self.conv5(torch.cat((value, first, second, third, fourth), 1)) * 0.2 + value


class RRDB(nn.Module):
    def __init__(self, channels: int = 64, growth: int = 32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(channels, growth)
        self.rdb2 = ResidualDenseBlock(channels, growth)
        self.rdb3 = ResidualDenseBlock(channels, growth)

    def forward(self, value):
        return self.rdb3(self.rdb2(self.rdb1(value))) * 0.2 + value


class RRDBNet(nn.Module):
    def __init__(self, blocks: int, scale: int):
        super().__init__()
        self.scale = scale
        self.conv_first = nn.Conv2d(12 if scale == 2 else 3, 64, 3, 1, 1)
        self.body = nn.Sequential(*(RRDB() for _ in range(blocks)))
        self.conv_body = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_hr = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_last = nn.Conv2d(64, 3, 3, 1, 1)

    def forward(self, value):
        if self.scale == 2:
            value = F.pixel_unshuffle(value, 2)
        feature = self.conv_first(value)
        body = self.conv_body(self.body(feature)) + feature
        body = F.leaky_relu(self.conv_up1(F.interpolate(body, scale_factor=2, mode="nearest")), 0.2, inplace=True)
        body = F.leaky_relu(self.conv_up2(F.interpolate(body, scale_factor=2, mode="nearest")), 0.2, inplace=True)
        return self.conv_last(F.leaky_relu(self.conv_hr(body), 0.2, inplace=True))


def load_model(weights: Path, scale: int, blocks: int) -> RRDBNet:
    model = RRDBNet(blocks, scale)
    payload = torch.load(weights, map_location="cpu", weights_only=True)
    state = payload.get("params_ema") or payload.get("params") or payload
    model.load_state_dict(state, strict=True)
    return model.eval().half().cuda()


def auto_tile() -> int:
    free, _total = torch.cuda.mem_get_info()
    free_gib = free / 1024**3
    return 640 if free_gib >= 24 else 512 if free_gib >= 16 else 384 if free_gib >= 10 else 256


@torch.inference_mode()
def enhance(model: RRDBNet, image: Image.Image, scale: int, tile: int, *,
            checkpoint=lambda: None, progress=lambda _completed, _total: None) -> Image.Image:
    source = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    height, width = source.shape[:2]
    tensor = torch.from_numpy(source.transpose(2, 0, 1)).unsqueeze(0).half().cuda()
    if scale == 2:
        tensor = F.pad(tensor, (0, width % 2, 0, height % 2), mode="replicate")
    padded_height, padded_width = tensor.shape[-2:]
    output = torch.empty((1, 3, padded_height * scale, padded_width * scale),
                         device="cuda", dtype=torch.float16)
    overlap = 16
    tops, lefts = range(0, padded_height, tile), range(0, padded_width, tile)
    total, completed = len(tops) * len(lefts), 0
    checkpoint()
    for top in tops:
        for left in lefts:
            checkpoint()
            bottom, right = min(top + tile, padded_height), min(left + tile, padded_width)
            padded_top, padded_left = max(0, top - overlap), max(0, left - overlap)
            padded_bottom = min(padded_height, bottom + overlap)
            padded_right = min(padded_width, right + overlap)
            patch = tensor[:, :, padded_top:padded_bottom, padded_left:padded_right]
            result = model(patch).clamp_(0, 1)
            crop_top, crop_left = (top - padded_top) * scale, (left - padded_left) * scale
            crop_bottom = crop_top + (bottom - top) * scale
            crop_right = crop_left + (right - left) * scale
            output[:, :, top * scale:bottom * scale, left * scale:right * scale] = (
                result[:, :, crop_top:crop_bottom, crop_left:crop_right])
            completed += 1
            progress(completed, total)
            checkpoint()
    output = output[:, :, :height * scale, :width * scale]
    array = (output[0].float().cpu().numpy().transpose(1, 2, 0) * 255.0).round().astype(np.uint8)
    return Image.fromarray(array, "RGB")

from __future__ import annotations

import io
import unittest

try:
    from PIL import Image
except ImportError:  # The minimal Linux control runtime intentionally omits media decoders.
    Image = None

from mediacenter.capabilities import validate_options
from mediacenter.domain import ServiceKind
from scripts.run_model_acceptance_matrix import MODELS, png_fixture


class ModelAcceptanceMatrixTests(unittest.TestCase):
    def test_matrix_is_exactly_the_formal_fourteen_model_set(self):
        self.assertEqual([row["instance"] for row in MODELS], [
            "musicgen-small", "sdxl-base-1.0", "realesrgan-x2plus",
            "realesrgan-x4plus", "realesrgan-x4plus-anime-6b", "z-image-turbo",
            "qwen-image-2512", "illustrious-xl-v2.0", "cosyvoice2-0.5b",
            "minimax-h3-ref2va", "ltx-2.3", "wan2.2-i2v",
            "hunyuanvideo-1.5", "wan2.1-t2v-1.3b",
        ])
        catalogs = {"ltx-2.3": "ltx-2.3-distilled", "wan2.2-i2v": "wan2.2-i2v-a14b",
                    "hunyuanvideo-1.5": "hunyuanvideo-1.5-720p-t2v"}
        for row in MODELS:
            validate_options(catalogs.get(row["instance"], row["instance"]),
                             ServiceKind(row["service"]), row["options"])

    @unittest.skipIf(Image is None, "Pillow unavailable in minimal control runtime")
    def test_reference_fixture_is_a_small_fully_decodable_png(self):
        raw = png_fixture()
        self.assertLess(len(raw), 16 * 1024)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual((image.format, image.size, image.mode), ("PNG", (64, 64), "RGB"))
            image.load()


if __name__ == "__main__":
    unittest.main()

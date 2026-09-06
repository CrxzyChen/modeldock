from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GenerationUITests(unittest.TestCase):
    def source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_four_media_workspaces_use_server_capability_contracts(self) -> None:
        app = self.source("client/src/App.vue")
        image = self.source("client/src/views/ImageWorkspace.vue")
        video = self.source("client/src/views/VideoWorkspace.vue")
        audio = self.source("client/src/views/GenerationWorkspace.vue")
        for view in ("image", "video", "speech", "music"):
            self.assertIn(f"store.activeView === '{view}'", app)
        for source in (image, video, audio):
            self.assertIn("capabilities", source)
            self.assertIn("OptionFields", source)
        self.assertIn("size_presets", image)
        self.assertIn("workflow_guide", video)
        self.assertIn("input_contract", video + audio)

    def test_image_workspace_preserves_one_editing_document_and_canvas_tools(self) -> None:
        image = self.source("client/src/views/ImageWorkspace.vue")
        styles = self.source("client/src/styles/image.css")
        for contract in ("documentState", "replaceBlob", "undo", "redo", "applyCrop", "applyMosaic",
                         "upscale", "saveDocument", "exportDocument", "cropDown", "cropMove"):
            self.assertIn(contract, image)
        self.assertIn("translate(-50%, -50%)", image)
        self.assertIn("ResizeObserver", image)
        self.assertIn("@wheel=\"historyWheel\"", image)
        self.assertIn("@drop.prevent.stop=\"dropLocal\"", image)
        self.assertIn("border-radius: 0", styles)
        self.assertNotIn("innerHTML", image)

    def test_video_workspace_supports_all_declared_workflows_and_mixed_assets(self) -> None:
        video = self.source("client/src/views/VideoWorkspace.vue")
        main = self.source("electron/main.js")
        for workflow in ("t2v", "i2v", "ref2va"):
            self.assertIn(workflow, video)
        self.assertIn("referenceOf", video)
        self.assertIn("moveAsset", video)
        self.assertIn("uploadDroppedAssets", video)
        self.assertIn("acceptedMediaExtensions", main)
        for prefix in ('"image/"', '"video/"', '"audio/"'):
            self.assertIn(prefix, main)

    def test_reactive_renderer_does_not_replace_whole_pages(self) -> None:
        sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "client/src").rglob("*.vue"))
        sources += "\n" + "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "client/src").rglob("*.ts"))
        self.assertNotIn("innerHTML =", sources)
        self.assertNotIn("setInterval(", sources)
        self.assertIn("v-show", self.source("client/src/App.vue"))
        self.assertIn("scheduleResourceEvent", self.source("client/src/stores/app.ts"))

    def test_gpu_ownership_is_never_inferred_or_hardcoded_in_ui(self) -> None:
        sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "client/src").rglob("*.*") if path.suffix in {".ts", ".vue", ".css"})
        for forbidden in ("GPU 1 / 2", "GPU 3 / 4", "vLLM", "reserved_gpu_indices", "assignable_gpu_indices"):
            self.assertNotIn(forbidden, sources)
        self.assertIn("configured_for_mediacenter", self.source("client/src/views/ModelCenterView.vue"))


if __name__ == "__main__":
    unittest.main()

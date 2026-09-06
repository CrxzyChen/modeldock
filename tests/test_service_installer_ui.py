from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ServiceInstallerUIContracts(unittest.TestCase):
    def source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_install_center_filters_and_validates_real_recipe_requirements(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        for value in ("全部", "已安装", "未安装", "prerequisites_ready", "runtime_available",
                      "min_gpus", "max_gpus", "license", "authentication"):
            self.assertIn(value, view)
        self.assertIn("请先安装：", view)
        self.assertIn("接受许可证后才能安装", view)
        self.assertIn("来源授权", view)

    def test_install_uninstall_and_transfer_controls_use_server_contracts(self) -> None:
        store = self.source("client/src/stores/app.ts")
        status = self.source("client/src/components/StatusCenter.vue")
        self.assertIn("/api/v1/service-installations", store)
        self.assertIn("adopt_existing: true", store)
        self.assertIn("/api/v1/service-catalog/", store)
        self.assertIn("controlInstallation", store)
        self.assertIn("controlTransfer", store)
        for action in ("pause", "resume", "cancel", "retry"):
            self.assertIn(action, status)

    def test_model_assets_remain_separate_from_service_uninstall(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        store = self.source("client/src/stores/app.ts")
        self.assertIn("模型物理资源", view)
        self.assertIn("archive", view)
        self.assertIn("restore", view)
        self.assertIn("模型权重、镜像缓存和安装历史保留", view)
        self.assertIn("模型权重与安装历史会保留", store)


if __name__ == "__main__":
    unittest.main()

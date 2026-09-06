from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelCenterUIContracts(unittest.TestCase):
    def source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_service_models_assets_and_installation_are_one_workspace(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        self.assertIn("服务模型", view)
        self.assertIn("模型资产", view)
        self.assertIn("安装中心", view)
        self.assertIn("installOpen", view)
        self.assertIn("installedModels", view)
        self.assertIn("installModels", view)
        self.assertNotIn("model-center-install-panel", view)
        self.assertNotIn("model-center-assets-panel", view)

    def test_installed_models_are_grouped_by_runtime_state_and_have_lifecycle_actions(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        store = self.source("client/src/stores/app.ts")
        for label in ("已启动", "已停止", "启动容器服务", "停止容器服务", "配置显存策略", "INSTANCE SETTINGS"):
            self.assertIn(label, view)
        for label in ("statusFilter", "查看安装日志", "打开生成工作台", "groupRow(item).summary"):
            self.assertIn(label, view)
        for endpoint in ("/${action}", "/policy", "/uninstall"):
            self.assertIn(endpoint, store)
        self.assertIn("restart_pending", self.source("client/src/lib/models.ts"))
        self.assertIn("gpu_capacity_unavailable", self.source("client/src/lib/models.ts"))
        self.assertIn('icon="power"', view)
        self.assertIn('icon="sliders"', view)
        self.assertIn("模型权重与安装历史会保留", store)
        self.assertIn("residency_mode_unsupported", self.source("client/src/lib/models.ts"))

    def test_settings_only_expose_public_gpu_and_residency_policy(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        for value in ("gpu_uuids", "sharing_mode", "external_reserve_mib", "residency",
                      "idle_minutes", "restart_recovery", "任务时加载", "保持显存就绪",
                      "当前运行器不支持"):
            self.assertIn(value, view)
        for forbidden in ("data-setting=\"backend\"", "data-setting=\"binding\""):
            self.assertNotIn(forbidden, view)
        self.assertIn("residency_modes", view)
        self.assertIn('role="button"', view)
        self.assertIn('tabindex="0"', view)
        self.assertIn('@keydown.enter.prevent="selectModel(item)"', view)

    def test_instance_settings_is_an_explicit_closeable_inspector(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        theme = self.source("client/src/styles/theme.css")
        for value in ("settingsOpen", "openSettings", "settings-open", "关闭实例设置"):
            self.assertIn(value, view)
        self.assertIn("deployment && settingsOpen", view)
        self.assertIn(".model-body.settings-open", theme)
        self.assertIn(".model-settings-close", theme)

    def test_unsupported_residency_modes_remain_visible_and_are_clearly_disabled(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        theme = self.source("client/src/styles/theme.css")
        self.assertIn('class="residency-choice"', view)
        self.assertIn(':disabled="!residencySupported(item.value)"', view)
        self.assertIn("当前运行器不支持", view)
        self.assertIn(".residency-choice.disabled", theme)
        self.assertIn("cursor: not-allowed", theme)
        self.assertIn("var(--mw-text-faint)", theme)

    def test_overview_is_a_continuous_desktop_console(self) -> None:
        view = self.source("client/src/views/OverviewView.vue")
        theme = self.source("client/src/styles/theme.css")
        for value in ("overview-command", "overview-launchbar", "overview-console", "overview-services-pane", "overview-activity-pane"):
            self.assertIn(value, view)
            self.assertIn(f".{value}", theme)
        for retired in ("metric-strip", "dashboard-grid", "launcher-surface", "launcher-grid"):
            self.assertNotIn(retired, view)
        self.assertIn("overview-work-state", view)

    def test_service_and_model_status_are_continuous_operational_surfaces(self) -> None:
        services = self.source("client/src/views/ServicesView.vue")
        models = self.source("client/src/views/ModelCenterView.vue")
        theme = self.source("client/src/styles/theme.css")
        self.assertIn("service-config-list", services)
        self.assertIn("service-config-row", services)
        self.assertIn("service-work-state", services)
        self.assertNotIn("service-config-grid", services)
        self.assertNotIn("service-config-card", services)
        self.assertIn("model-runtime-summary", models)
        self.assertNotIn("model-status-board", models)
        self.assertIn(".model-runtime-summary", theme)
        self.assertIn(".deployment-facts span:nth-child(even)", theme)

    def test_cards_use_midnight_workshop_geometry_without_decorative_left_rail(self) -> None:
        styles = self.source("client/src/styles/base.css") + self.source("client/src/styles/views.css") + self.source("client/src/styles/model.css") + self.source("client/src/styles/theme.css")
        for selector in (".model-row", ".surface", ".install-center", ".asset-dialog"):
            self.assertIn(selector, styles)
        self.assertNotRegex(styles, re.compile(r"\.model-row::?before"))
        self.assertIn("--mw-radius-control: 5px", styles)
        self.assertIn("--mw-radius-panel: 6px", styles)
        self.assertIn("--mw-radius-dialog: 10px", styles)

    def test_user_model_flow_is_one_five_step_desktop_wizard(self) -> None:
        view = self.source("client/src/views/ModelCenterView.vue")
        wizard = self.source("client/src/components/ModelImportWizard.vue")
        store = self.source("client/src/stores/app.ts")
        self.assertIn("ModelImportWizard", view)
        self.assertIn("导入并部署", view)
        for value in ("来源", "识别", "资产", "部署", "确认"):
            self.assertIn(value, wizard)
        for method in ("pickModelForImport", "listModelUploadSessions", "startModelUpload",
                       "resumeModelUpload", "pauseModelUpload"):
            self.assertIn(method, wizard)
        for endpoint in ("/api/v1/deployment-plans", "/api/v1/deployment-operations"):
            self.assertIn(endpoint, store)
        self.assertNotIn("pickAndUploadModel(upload)", view)

    def test_deployment_choices_show_disabled_reasons_and_do_not_expose_local_paths(self) -> None:
        wizard = self.source("client/src/components/ModelImportWizard.vue")
        styles = self.source("client/src/styles/model.css")
        for reason in ("未加入当前服务器的 MediaCenter 资源池", "显存遥测不可用",
                       "检测到外部进程，不能独占", "当前不能部署"):
            self.assertIn(reason, wizard)
        for selector in (".runtime-choice.disabled", ".import-gpu-choice.disabled",
                         ".deploy-residency label.disabled"):
            self.assertIn(selector, styles)
        self.assertIn("cursor: not-allowed", styles)
        self.assertNotIn("absolutePath", wizard)

    def test_user_deployment_exposes_real_cancel_retry_and_failure_actions(self) -> None:
        wizard = self.source("client/src/components/ModelImportWizard.vue")
        store = self.source("client/src/stores/app.ts")
        for value in ("cancelCurrentDeployment", "retryDeployment", "reviseDeployment",
                      "error_message", "error_class", "关闭窗口不会中断执行"):
            self.assertIn(value, wizard)
        self.assertIn("cancelDeploymentOperation", store)
        self.assertIn("模型资产与实例配置已保留", store)


if __name__ == "__main__":
    unittest.main()

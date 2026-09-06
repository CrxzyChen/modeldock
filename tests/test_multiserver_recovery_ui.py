from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MultiServerRecoveryUiTests(unittest.TestCase):
    def source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_credentials_are_encrypted_per_profile_and_never_rendered(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        renderer = self.source("client/src/stores/app.ts")
        self.assertIn("safeStorage.encryptString", main)
        self.assertIn("safeStorage.decryptString", main)
        self.assertIn("activeProfileId", main)
        self.assertNotIn("connectionCredentials", preload + renderer)

    def test_server_switch_scopes_sse_and_releases_media_urls(self) -> None:
        store = self.source("client/src/stores/app.ts")
        image = self.source("client/src/views/ImageWorkspace.vue")
        self.assertIn("connectionEpoch", store)
        self.assertIn("envelope.profileId !== connections.value.activeProfileId", store)
        self.assertIn("envelope.revision !== connections.value.revision", store)
        self.assertIn("resetServerResources", store)
        self.assertIn("URL.revokeObjectURL", image)


if __name__ == "__main__":
    unittest.main()

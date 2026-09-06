"""Immutable installation dependencies, using public local asset publication."""
import unittest
from unittest.mock import patch

from mediacenter.container_releases import RuntimeContractError
from mediacenter.service_installer import ServiceInstallerError
from tests import test_service_installer as fixtures


class DeploymentDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceInstallerTests(); self.fixture.setUp()
        self.runtime = self.fixture.installer.installation_runtime

    def tearDown(self): self.fixture.tearDown()

    def install(self, key):
        f = self.fixture
        f.ready_asset(f.installer.recipes[key]['recommended_revision'])
        with patch.object(f.installer, '_spawn'):
            item = f.installer.start({'recipe_key':key,'gpu_indices':[0],'license_accepted':True})
        f.installer._run_guarded(item['id'])
        result = f.installer.get(item['id'])
        self.assertEqual(result['state'], 'ready', result)
        return self.runtime.get(key)

    def test_missing_dependency_is_not_replaced_by_an_arbitrary_ready_asset(self):
        self.fixture.ready_asset()
        with self.assertRaises(ServiceInstallerError): self.install('dependent-image')
        self.assertEqual(self.fixture.repository.list_service_installations(), [])

    def test_dependency_binding_is_exact_and_drift_closes_readback_without_probe(self):
        parent = self.install('test-image')
        child = self.install('dependent-image')
        dependency = child['dependencies'][0]
        self.assertEqual((dependency['deployment_id'],dependency['incarnation'],dependency['asset_id'],dependency['revision']),
                         ('test-image',parent['incarnation'],parent['asset_id'],parent['asset_revision']))
        self.assertTrue(self.runtime.levels('dependent-image')['installed'])
        self.assertEqual(self.fixture.installer.runtime_importer.loads, 1)
        self.assertEqual(self.fixture.image_reads, [0])
        with self.fixture.repository._connect() as db:
            db.execute("UPDATE model_assets SET role='lora' WHERE id=?", (parent['asset_id'],))
        with patch('subprocess.run', side_effect=AssertionError('readback is not a probe')):
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_installation_binding_changed'):
                self.runtime.levels('dependent-image')

    def test_dependency_revision_changed_after_deployment_creation_aborts_entire_activation(self):
        self.install('test-image')
        f = self.fixture; f.ready_asset('b'*40)
        with patch.object(f.installer, '_spawn'):
            operation = f.installer.start({'recipe_key':'dependent-image','gpu_indices':[0],'license_accepted':True})
        original = f.deployments.create
        def drift(*args, **kwargs):
            result = original(*args, **kwargs)
            with f.repository._connect() as db:
                db.execute("UPDATE model_deployments SET revision='drifted-revision' WHERE id='test-image'")
            return result
        with patch.object(f.deployments, 'create', side_effect=drift):
            f.installer._run_guarded(operation['id'])
        self.assertEqual(f.installer.get(operation['id'])['state'], 'failed')
        self.assertIsNone(f.repository.get_deployment('dependent-image'))
        self.assertIsNone(self.runtime.get('dependent-image'))


if __name__ == '__main__': unittest.main()

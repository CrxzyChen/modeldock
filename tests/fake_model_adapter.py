"""CPU-only adapter for real spawned-process Worker contract tests."""
from mediacenter.adapter import Adapter
from mediacenter.capabilities import worker_capability_for


class FixtureOutOfMemoryError(RuntimeError):
    """CPU-only typed stand-in; never evidence of a real CUDA allocation."""


class FakeAdapter(Adapter):
    def __init__(self, entered=None, release=None, counter=None, resets=None,
                 cooperative=True, reset_fail=False, execute_fail=False, load_fail=False, forge_proof=False, capability_mismatch=False,
                 capability_entered=None, capability_release=None, execution_oom=None):
        self.entered, self.release, self.counter, self.resets = entered, release, counter, resets
        self.cooperative, self.reset_fail, self.execute_fail = cooperative, reset_fail, execute_fail
        self.load_fail,self.forge_proof=load_fail,forge_proof
        self.capability_mismatch=capability_mismatch
        self.capability_entered,self.capability_release=capability_entered,capability_release
        self.loaded = False
        self.execution_oom = execution_oom

    def describe_capabilities(self):
        if self.capability_entered: self.capability_entered.set()
        if self.capability_release and not self.capability_release.wait(10): raise RuntimeError('capability barrier timeout')
        return worker_capability_for("z-image-turbo" if self.capability_mismatch else "sdxl-base-1.0")

    def load(self, binding):
        self.loaded = True
        if self.load_fail: raise RuntimeError('fixture partial load')

    def execute(self, request, progress, cancellation):
        if self.counter is not None:
            with self.counter.get_lock():
                self.counter.value += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            while not self.release.wait(0.02):
                if self.cooperative and cancellation.is_set():
                    break
        if self.execute_fail:
            raise RuntimeError("fixture failure")
        if self.execution_oom:
            import sys
            from types import SimpleNamespace
            # This fixture runs in the isolated inference child only.
            sys.modules['torch'] = SimpleNamespace(OutOfMemoryError=FixtureOutOfMemoryError)
            kind, self.execution_oom = self.execution_oom, None
            error_type = FixtureOutOfMemoryError if kind == 'typed' else RuntimeError
            raise error_type('CUDA out of memory: secret=/private/model-key prompt=private')
        manifest={"asset_id": "fixture-artifact", "revision": "r1", "sha256": "a" * 64}
        if self.forge_proof: manifest['execution_quiescence']={'kind':'quiescent'}
        return manifest

    def reset_task_state(self):
        if self.resets is not None:
            with self.resets.get_lock():
                self.resets.value += 1
        if self.reset_fail:
            raise RuntimeError("fixture reset failure")

    def unload(self):
        self.loaded = False

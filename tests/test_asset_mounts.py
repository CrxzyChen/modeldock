import os
import tempfile
import unittest
from pathlib import Path

from mediacenter.artifacts import ArtifactStore, open_regular
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState, TaskStateError


class AssetMountTests(unittest.TestCase):
    def test_root_overlap_in_both_directions_and_identity_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);parent=root/"parent";parent.mkdir();child=parent/"child";child.mkdir()
            state=TaskState(Repository(root/"state.db"))
            for sealed,writable in ((parent,parent),(parent,child),(child,parent)):
                with self.assertRaisesRegex(TaskStateError,"overlap"):
                    ArtifactStore(state,sealed,writable_roots=[writable])
            store=ArtifactStore(state,root/"sealed")
            store.root.rename(root/"preserved");store.root.mkdir()
            with self.assertRaisesRegex(TaskStateError,"artifact_root_changed"):store.check_boundary()
    @unittest.skipUnless(os.name=="posix","symlink fixture needs POSIX")
    def test_symlink_parent_final_and_special_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);real=root/"real";real.mkdir();(real/"data").write_bytes(b"data")
            (root/"alias").symlink_to(real,target_is_directory=True)
            (root/"file").symlink_to(real/"data")
            os.mkfifo(root/"fifo")
            for path in (root/"alias"/"data",root/"file",root/"fifo"):
                with self.assertRaises((TaskStateError,OSError)):
                    with open_regular(path):pass

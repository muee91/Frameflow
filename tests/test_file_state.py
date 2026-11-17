"""Native metadata guards and full-content verification are distinct checks."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from frameflow.errors import TaskError
from frameflow.file_state import descriptor_signature, path_signature
from frameflow.media import probe_and_hash, verify_identity
from frameflow.store import Store


class Context:
    def check_cancel(self):
        pass

    def heartbeat(self):
        pass

    def progress(self, *args, **kwargs):
        pass


class FileStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "source.wav"
        self.path.write_bytes(b"synthetic media content")

    def replace_one_byte_preserving_mtime(self):
        before = self.path.stat()
        with self.path.open("r+b") as source:
            initial = source.read(1)
            source.seek(0)
            source.write(bytes([initial[0] ^ 1]))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def asset(self):
        metadata = '{"format":{"duration":"1"},"streams":[{"codec_type":"audio","channels":1}]}'
        with patch("frameflow.media.run_command", return_value=metadata):
            return probe_and_hash({"path": self.path, "camera": "A", "kind": "recorder"}, Context())

    def store_and_project(self):
        store = Store(self.root / "state.sqlite3")
        project = store.create_project("Signature regression", [
            {"path": str(self.root), "label": "A", "kind": "recorder"}])
        return store, project["id"]

    def test_path_and_handle_agree_after_rename_and_content_change(self):
        # The old path.stat/fstat ctime comparison failed on the second and
        # third cases under Windows Python 3.12 even though reads were stable.
        for operation in ("initial", "rename", "rewrite"):
            if operation == "rename":
                renamed = self.path.with_name("renamed.wav")
                self.path.rename(renamed)
                self.path = renamed
            elif operation == "rewrite":
                self.replace_one_byte_preserving_mtime()
            with self.subTest(operation=operation), self.path.open("rb") as source:
                expected = path_signature(self.path)
                self.assertEqual(descriptor_signature(source.fileno()), expected)
                source.read()
                self.assertEqual(descriptor_signature(source.fileno()), expected)
                self.assertEqual(path_signature(self.path), expected)

    def test_identity_recheck_rejects_rewrite_even_when_metadata_collides(self):
        asset = self.asset()
        before = path_signature(self.path)
        self.replace_one_byte_preserving_mtime()
        after = path_signature(self.path)
        self.assertEqual(before["size"], after["size"])
        self.assertEqual(before["modified_ns"], after["modified_ns"])
        # Windows CI demonstrated equal ChangeTime for this real quick write.
        # Force the collision on every platform; no sleep or timestamp-order
        # assumption may substitute for checking the expected content hash.
        with patch("frameflow.media.path_signature", return_value=before), \
                patch("frameflow.media.descriptor_signature", return_value=before):
            with self.assertRaises(TaskError) as caught:
                verify_identity(self.path, asset["media_id"], Context())
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")

    def test_binding_cannot_pair_old_hash_with_changed_source_state(self):
        asset = self.asset()
        store, project_id = self.store_and_project()
        # This is specifically the inexpensive metadata handoff guard. A size
        # change is deterministic even if two writes share a timestamp tick.
        with self.path.open("ab") as output:
            output.write(b" changed size")
        with self.assertRaises(TaskError) as caught:
            store.save_asset(project_id, asset)
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")
        with self.assertRaises(KeyError):
            store.binding(project_id, asset["media_id"])

    def test_probe_signature_is_private_and_range_checks_do_not_hash_content(self):
        asset = self.asset()
        store, project_id = self.store_and_project()
        store.save_asset(project_id, asset)
        with store.connect() as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM assets").fetchone()[0])
        self.assertNotIn("_source_signature", metadata)
        self.assertNotIn("local_path", metadata)
        with patch("os.read", side_effect=AssertionError("metadata checks must not read content")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("metadata checks must not hash content")):
            self.assertEqual(store.checked_binding(project_id, asset["media_id"]), self.path)

    def test_unavailable_or_legacy_change_state_is_not_trusted(self):
        asset = self.asset()
        store, project_id = self.store_and_project()
        store.save_asset(project_id, asset)
        with patch("frameflow.store.path_signature", side_effect=OSError("native file state unavailable")):
            with self.assertRaises(TaskError) as caught:
                store.checked_binding(project_id, asset["media_id"])
            self.assertEqual(caught.exception.code, "MEDIA_UNREADABLE")
        with store.transaction() as db:
            db.execute("UPDATE bindings SET signature_json=?", ('[1,2,3]',))
        with self.assertRaises(TaskError) as caught:
            store.checked_binding(project_id, asset["media_id"])
        self.assertEqual(caught.exception.code, "SOURCE_CHANGED")


if __name__ == "__main__":
    unittest.main()

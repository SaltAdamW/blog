import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_package", ROOT / "release" / "package.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


class ReleaseTests(unittest.TestCase):
    def test_archive_is_allowlisted_and_all_hashes_match(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = PACKAGE.build(ROOT, temp)
            with tarfile.open(archive) as tar:
                entries = {m.name.split("/", 1)[1]: tar.extractfile(m).read() for m in tar.getmembers()}
            self.assertEqual(set(entries), {*PACKAGE.FILES, ".gitignore", "MANIFEST.sha256"})
            self.assertNotIn("config.json", entries)
            self.assertFalse(json.loads(entries["config.example.json"])["publication"]["enabled"])
            for line in entries["MANIFEST.sha256"].decode().splitlines():
                expected, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(entries[name]).hexdigest(), expected)
            self.assertIn(hashlib.sha256(archive.read_bytes()).hexdigest(), (Path(temp) / "SHA256SUMS").read_text())

    def test_rebuild_is_reproducible_and_existing_archive_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = PACKAGE.build(ROOT, first)
            b = PACKAGE.build(ROOT, second)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with self.assertRaises(FileExistsError):
                PACKAGE.build(ROOT, first)


if __name__ == "__main__":
    unittest.main()

"""Use invented credentials and isolated files; never open school accounts."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from credential_store import CredentialStore
from seat_core import Problem

TEMP = ROOT.parents[1] / "tmp" / ROOT.name / "tests"
TEMP.mkdir(parents=True, exist_ok=True)


@unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEMP)
        self.path = Path(self.directory.name) / "credentials.dpapi"
        self.store = CredentialStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_roundtrip_keeps_password_whitespace_and_unicode(self):
        password = " fictional 密码 with spaces  "
        self.store.save("A", " fictional-account-A ", password)
        self.assertEqual(CredentialStore(self.path).get("A"),
                         {"username": "fictional-account-A", "password": password})
        for path in self.path.parent.iterdir():
            data = path.read_bytes()
            self.assertNotIn(b"fictional-account-A", data)
            self.assertNotIn(password.encode("utf-8"), data)

    def test_saving_a_does_not_replace_b(self):
        self.store.save("B", "fictional-B", "sample-B")
        self.store.save("A", "fictional-A", "sample-A")
        self.store.save("A", "fictional-A2", "sample-A2")
        self.assertEqual(self.store.get("B"), {"username": "fictional-B", "password": "sample-B"})

    def test_invalid_role_or_empty_value_preserves_saved_file(self):
        self.store.save("A", "fictional-A", "sample-A")
        original = self.path.read_bytes()
        for role, username, password in [("C", "x", "y"), ("A", "", "y"), ("A", "x", ""), ("B", {}, "y")]:
            with self.subTest(role=role, username=username):
                with self.assertRaises(Problem):
                    self.store.save(role, username, password)
                self.assertEqual(original, self.path.read_bytes())

    def test_corrupted_file_is_not_silently_overwritten(self):
        self.path.write_bytes(b"not a DPAPI file")
        for operation in [lambda: self.store.get("A"), lambda: self.store.save("B", "fictional-B", "sample-B")]:
            with self.assertRaises(Problem):
                operation()
            self.assertEqual(self.path.read_bytes(), b"not a DPAPI file")

    def test_unconfigured_account_returns_empty_fields(self):
        self.assertEqual(self.store.get("A"), {"username": "", "password": ""})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

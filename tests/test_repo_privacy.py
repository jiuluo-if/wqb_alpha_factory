"""Privacy regression for the public tree.

The repository keeps raw research exports local-only; this test proves the
guard detects the classes it claims to detect and that the tracked tree is free
of them right now. Synthetic samples are built by concatenation so the test file
itself never contains a literal private path.
"""

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD_BLIND_SPOT_BYTES = 4 * 1024 * 1024


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "check_repo_privacy", ROOT / "scripts" / "check_repo_privacy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_repo_privacy"] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load_guard()


class TestRepositoryPrivacyGuard(unittest.TestCase):
    def _synthetic_root(self):
        directory = tempfile.mkdtemp(prefix="privacy_scan_")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return pathlib.Path(directory)

    def _write_large(self, root, name, *, private=False, binary=False):
        """Write a >4 MB tracked file (the previous scan blind spot)."""
        chunk = b"clean synthetic line without private data\n"
        with (root / name).open("wb") as handle:
            if binary:
                handle.write(b"\x00" * 64)
            total = 0
            while total < 5 * 1024 * 1024:
                handle.write(chunk)
                total += len(chunk)
            if private:
                marker = "root=" + "F" + ":" + "\\" + "Users" + "\\" + "researcher"
                handle.write(marker.encode("utf-8") + b"\n")
        return name

    def test_tracked_tree_has_no_private_research_data(self):
        files = GUARD.tracked_files(ROOT)
        findings, skipped = GUARD.scan(ROOT, files)
        self.assertEqual(skipped, [])
        self.assertEqual(
            [item.as_dict() for item in findings],
            [],
            "tracked files must not expose machine paths or private research artifacts",
        )

    def test_large_tracked_text_is_scanned_instead_of_skipped(self):
        root = self._synthetic_root()
        name = self._write_large(root, "large_private.txt", private=True)
        self.assertGreater((root / name).stat().st_size, OLD_BLIND_SPOT_BYTES)
        findings, skipped_binary = GUARD.scan(root, [name])
        self.assertEqual(skipped_binary, [])
        self.assertIn("ABSOLUTE_LOCAL_PATH", {item.code for item in findings})
        self.assertGreater(min(item.line or 0 for item in findings), 0)

    def test_large_clean_tracked_text_stays_clean(self):
        root = self._synthetic_root()
        name = self._write_large(root, "large_clean.txt")
        self.assertGreater((root / name).stat().st_size, OLD_BLIND_SPOT_BYTES)
        findings, skipped_binary = GUARD.scan(root, [name])
        self.assertEqual([item.as_dict() for item in findings], [])
        self.assertEqual(skipped_binary, [])

    def test_large_binary_is_reported_as_binary_skip(self):
        root = self._synthetic_root()
        name = self._write_large(root, "large_blob.bin", binary=True)
        self.assertGreater((root / name).stat().st_size, OLD_BLIND_SPOT_BYTES)
        findings, skipped_binary = GUARD.scan(root, [name])
        self.assertEqual([item.as_dict() for item in findings], [])
        self.assertEqual(skipped_binary, [name])

    def test_unreadable_tracked_file_fails_closed(self):
        root = self._synthetic_root()
        name = "unreadable.txt"
        (root / name).write_text("tracked text\n", encoding="utf-8")
        with mock.patch.object(pathlib.Path, "open", side_effect=PermissionError("denied")):
            findings, _ = GUARD.scan(root, [name])
        self.assertTrue(findings, "unreadable tracked text must produce a finding (exit code 1)")
        self.assertIn("UNREADABLE_TRACKED_FILE", {item.code for item in findings})

    def test_windows_drive_path_is_detected(self):
        text = "root = " + "C" + ":" + "\\" + "Users" + "\\" + "researcher" + "\\" + "state"
        codes = {item.code for item in GUARD._scan_text("sample.py", text)}
        self.assertIn("ABSOLUTE_LOCAL_PATH", codes)

    def test_private_attachment_path_is_detected(self):
        text = "spec=" + "attachments" + "/" + "7e3fc2fc-7fdc-41c6-a100-61d212d98f8c" + "/goal.md"
        codes = {item.code for item in GUARD._scan_text("sample.md", text)}
        self.assertIn("PRIVATE_ATTACHMENT_PATH", codes)

    def test_alpha_identifier_is_detected(self):
        text = "alpha_id=" + "alpha-" + "1234567890"
        codes = {item.code for item in GUARD._scan_text("sample.json", text)}
        self.assertIn("ALPHA_IDENTIFIER", codes)

    def test_submission_identifier_is_detected(self):
        text = "proposal_id=" + "p-" + "52b9c26b61b4e283"
        codes = {item.code for item in GUARD._scan_text("sample.json", text)}
        self.assertIn("SUBMISSION_IDENTIFIER", codes)

    def test_remote_simulation_url_is_detected(self):
        text = (
            "url=https://api.worldquantbrain.com/simulations/"
            + "9f2a4c6b8d0e1234"
        )
        codes = {item.code for item in GUARD._scan_text("sample.py", text)}
        self.assertIn("SIMULATION_PROGRESS_URL", codes)

    def test_raw_audit_artifact_path_is_detected(self):
        findings, _ = GUARD.scan(ROOT, ["docs/research_quality_audit_x/audit.json"])
        codes = {item.code for item in findings}
        self.assertIn("RAW_AUDIT_ARTIFACT", codes)

    def test_weak_credential_literal_is_not_flagged(self):
        text = "password=" + '"' + "explicit-password" + '"'
        codes = {item.code for item in GUARD._scan_text("sample.py", text)}
        self.assertNotIn("HARDCODED_CREDENTIAL", codes)

    def test_plain_architecture_reference_is_not_flagged(self):
        text = "state_dir = '" + ".wqb_state" + "'  # relative, project-owned"
        codes = {item.code for item in GUARD._scan_text("sample.py", text)}
        self.assertEqual(codes, set())

    def test_operator_reference_research_history_is_detected(self):
        root = self._synthetic_root()
        name = "docs/reference/OPERATORS_CHEATSHEET.md"
        (root / "docs/reference").mkdir(parents=True)
        (root / name).write_text(
            "`rank(x)` usage " + "count: 12\n", encoding="utf-8"
        )
        findings, _ = GUARD.scan(root, [name])
        self.assertIn(
            "OPERATOR_REFERENCE_RESEARCH_HISTORY",
            {item.code for item in findings},
        )


if __name__ == "__main__":
    unittest.main()

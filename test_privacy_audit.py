"""Privacy regression tests use synthetic data and reserved example domains only."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('privacy_audit', Path(__file__).parent / 'scripts' / 'privacy_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PrivacyAuditTests(unittest.TestCase):
    def test_current_untracked_files_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.git(root, 'init')
            (root / 'new.py').write_text('name = "fictional-private-marker"\n')
            findings, count = audit.audit(root, markers=('fictional-private-marker',))
            self.assertEqual(count, 1)
            self.assertTrue(any('private marker' in item for item in findings))

    def test_decodes_escaped_and_concatenated_literals(self):
        source = 'name = "fictional-" + "private-marker"\nother = "\\u0066ictional-private-marker"\n'
        findings = audit.scan_text(source, 'fixture.py', ('fictional-private-marker',), python=True)
        self.assertEqual(len([item for item in findings if 'private marker' in item]), 2)

    def test_generic_rules_and_reserved_examples(self):
        private_mail = 'synthetic@example.com'
        secret = 'sk-' + 'z' * 25
        source = '\n'.join((private_mail, '/'.join(('', 'home', 'fictional-user', 'file')), '192.0.2.10', secret))
        with patch.object(audit, 'allowed_email', return_value=False), patch.object(audit, 'DOCUMENTATION_NETS', ()):
            findings = audit.scan_text(source, 'sample')
        for expected in ('email address', 'absolute home path', 'non-local IP address', 'possible credential'):
            self.assertTrue(any(expected in item for item in findings), expected)
        self.assertEqual(audit.scan_text('demo@example.com 192.0.2.10 127.0.0.1 12345+demo@users.noreply.github.com', 'sample'), [])

    def test_deleted_history_and_commit_email(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.git(root, 'init')
            self.git(root, 'config', 'user.name', 'Synthetic Test')
            self.git(root, 'config', 'user.email', 'demo@example.com')
            (root / 'old.py').write_text('name = "fictional-" + "private-marker"\n')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-m', 'Synthetic test fixture', env={'GIT_AUTHOR_EMAIL': 'synthetic@example.com'})
            self.git(root, 'rm', 'old.py')
            (root / 'README.md').write_text('Clean current state\n')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-m', 'Remove fixture')
            self.assertEqual(audit.audit(root)[0], [])
            with patch.object(audit, 'allowed_email', side_effect=lambda value: value != 'synthetic@example.com'):
                findings, _ = audit.audit(root, history=True, markers=('fictional-private-marker',))
            self.assertTrue(any('private marker' in item and 'old.py' in item for item in findings))
            self.assertTrue(any('private author email' in item for item in findings))
            self.assertFalse(any('private committer email' in item for item in findings))

    def test_unreadable_binary_is_not_silently_skipped(self):
        self.assertTrue(audit.scan_file('opaque.bin', b'\xff\x00', 'opaque.bin', ()))

    def test_external_marker_file_and_inside_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / 'repo'
            root.mkdir()
            markers = base / 'markers.txt'
            markers.write_text('fictional-private-marker\n')
            (root / 'fixture.txt').write_text('fictional-private-marker\n')
            env = {**os.environ, 'HOMEWORK_PRIVACY_MARKERS_FILE': str(markers)}
            result = subprocess.run(['python3', str(audit.ROOT / 'scripts/privacy_audit.py'), '--root', str(root)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('fictional-private-marker', result.stdout)
            env['HOMEWORK_PRIVACY_MARKERS_FILE'] = str(root / 'fixture.txt')
            result = subprocess.run(['python3', str(audit.ROOT / 'scripts/privacy_audit.py'), '--root', str(root)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    @staticmethod
    def git(root, *args, env=None):
        return subprocess.run(['git', *args], cwd=root, env={**os.environ, **(env or {})}, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()

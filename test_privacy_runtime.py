"""Privacy boundaries, using synthetic images and records only."""
import base64
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import web_app

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS9sAAAAASUVORK5CYII=')

class RuntimePrivacyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'data'
        self.data.mkdir()
        self.config = patch.multiple(web_app, DATA_DIR=self.data, DB_PATH=self.data / 'test.sqlite3')
        self.config.start()
        for name in web_app.IMAGE_DIRECTORIES:
            (self.data / name).mkdir()
        with contextlib.closing(web_app.connect_db()) as conn:
            self.card = web_app.save_card(conn, {'question_text': '虚构题目：1+1', 'subject': '数学', 'topic': '虚构加法', 'knowledge_points': ['加法'], 'correct_answer': '2'})

    def tearDown(self):
        self.config.stop()
        self.tmp.cleanup()

    def test_valid_image_in_each_dedicated_directory_and_relative_path(self):
        for directory in sorted(web_app.IMAGE_DIRECTORIES):
            path = self.data / directory / 'question.png'
            path.write_bytes(PNG)
            self.assertEqual(web_app.read_allowed_image(str(path)), (PNG, 'image/png'))
            self.assertEqual(web_app.read_allowed_image(f'{directory}/question.png'), (PNG, 'image/png'))

    def test_outside_path_traversal_and_non_image_data_directory_are_denied(self):
        outside = self.root / 'private.png'
        outside.write_bytes(PNG)
        (self.data / 'private.png').write_bytes(PNG)
        for value in (str(outside), str(self.data / 'private.png'), 'images/../../private.png'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                web_app.read_allowed_image(value)

    def test_configured_data_directory_alias_is_allowed(self):
        alias = self.root / 'data-alias'
        alias.symlink_to(self.data, target_is_directory=True)
        image = self.data / 'images' / 'question.png'
        image.write_bytes(PNG)
        with patch.object(web_app, 'DATA_DIR', alias):
            self.assertEqual(web_app.read_allowed_image(str(alias / 'images' / 'question.png')), (PNG, 'image/png'))

    def test_unsupported_platform_fails_closed(self):
        with patch.object(os, 'supports_dir_fd', set()), self.assertRaises(ValueError):
            web_app.read_allowed_image('images/question.png')

    def test_symlink_file_and_directory_cannot_escape(self):
        outside = self.root / 'private.png'
        outside.write_bytes(PNG)
        (self.data / 'images' / 'link.png').symlink_to(outside)
        (self.data / 'images' / 'linked-dir').symlink_to(self.root, target_is_directory=True)
        for value in ('images/link.png', 'images/linked-dir/private.png'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                web_app.read_allowed_image(value)
        (self.data / 'source-images').rmdir()
        (self.data / 'source-images').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            web_app.read_allowed_image('source-images/private.png')

    def test_extension_cannot_disguise_html_svg_or_plaintext(self):
        path = self.data / 'images' / 'fake.jpg'
        for payload in (b'<svg onload="alert(1)"></svg>', b'<html>private</html>', b'private text', PNG[:24]):
            path.write_bytes(payload)
            with self.subTest(payload=payload[:8]), self.assertRaises(ValueError):
                web_app.read_allowed_image(str(path))

    def test_fifo_and_oversized_file_are_rejected(self):
        path = self.data / 'images' / 'pipe.png'
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            web_app.read_allowed_image(str(path))
        path.unlink()
        with path.open('wb') as f:
            f.truncate(web_app.MAX_IMAGE_BYTES + 1)
        with self.assertRaises(ValueError):
            web_app.read_allowed_image(str(path))

    def test_http_image_boundary_and_private_logging(self):
        image = self.data / 'images' / 'question.png'
        image.write_bytes(PNG)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            server = web_app.ThreadingHTTPServer(('127.0.0.1', 0), web_app.AppHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path, allowed in ((image, True), (self.root / 'private.png', False)):
                    path.write_bytes(PNG)
                    with contextlib.closing(web_app.connect_db()) as conn:
                        conn.execute('UPDATE mistake_cards SET image_path=? WHERE id=?', (str(path), self.card['id']))
                        conn.commit()
                    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    client.request('GET', '/api/cards/' + self.card['id'] + '/image?child=synthetic-private-marker')
                    response = client.getresponse()
                    payload = response.read()
                    if allowed:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(payload, PNG)
                        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
                        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                    else:
                        self.assertNotEqual(response.status, 200)
                        self.assertNotIn(PNG, payload)
                        self.assertNotIn(str(self.root).encode(), payload)
                    client.close()
                for url in ('/api/backup', '/api/cards'):
                    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    client.request('GET', url)
                    response = client.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                    self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
                    client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertNotIn('synthetic-private-marker', output.getvalue())
        self.assertNotIn('127.0.0.1', output.getvalue())
        self.assertNotIn(self.card['id'], output.getvalue())
        self.assertIn('HTTP 200', output.getvalue())

    def test_private_backup_preserves_restore_data(self):
        with contextlib.closing(web_app.connect_db()) as conn:
            conn.execute('UPDATE mistake_cards SET source_session=?, image_path=? WHERE id=?',
                         ('synthetic-private-session', 'images/question.png', self.card['id']))
            conn.commit()
            backup = web_app.build_backup(conn)
            conn.execute('DELETE FROM mistake_cards')
            conn.commit()
            web_app.restore_backup(conn, backup)
            restored = web_app.build_backup(conn)
        self.assertEqual(backup['mistake_cards'], restored['mistake_cards'])
        self.assertIn('synthetic-private-session', json.dumps(restored))

if __name__ == '__main__':
    unittest.main()

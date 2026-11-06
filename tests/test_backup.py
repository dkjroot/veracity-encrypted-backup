import base64
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'verwrity/user_files'
        self.source.mkdir(parents=True)
        (self.source / 'A story').mkdir()
        (self.source / 'A story/scene.txt').write_text('My private story — café 🦊', encoding='utf-8')
        (self.source / 'empty').mkdir()
        (self.source / 'binary.dat').write_bytes(bytes(range(256)) * 10000)
        self.cloud = self.root / 'cloud'
        self.cloud.mkdir()
        self.key = os.urandom(32)

    def make_backup(self):
        return backup.create_backup(self.source, self.cloud, self.key)

    def encrypted_zip(self, names):
        plain = io.BytesIO()
        with zipfile.ZipFile(plain, 'w') as z:
            for name in names:
                z.writestr(name, b'test')
        plain.seek(0)
        target = self.cloud / 'crafted.vbackup'
        with target.open('wb') as output:
            backup.encrypt_stream(plain, output, self.key)
        return target

    def test_roundtrip_and_numbered_whole_folder_preservation(self):
        archive = self.make_backup()
        old_text = (self.source / 'A story/scene.txt').read_bytes()
        (self.source / 'A story/scene.txt').write_text('New work')
        (self.source / 'not-in-backup.txt').write_text('Keep me too')
        previous = backup.restore_backup(archive, self.source, self.key)
        self.assertEqual(previous.name, 'user_files.bu1')
        self.assertEqual((previous / 'A story/scene.txt').read_text(), 'New work')
        self.assertEqual((previous / 'not-in-backup.txt').read_text(), 'Keep me too')
        self.assertEqual((self.source / 'A story/scene.txt').read_bytes(), old_text)
        self.assertTrue((self.source / 'empty').is_dir())
        self.assertEqual((self.source / 'binary.dat').read_bytes(), bytes(range(256)) * 10000)
        self.assertEqual(backup.restore_backup(archive, self.source, self.key).name, 'user_files.bu2')
        self.assertEqual(len(list(self.cloud.iterdir())), 1)
        self.assertNotIn(b'My private story', archive.read_bytes())
        self.assertNotIn(b'scene.txt', archive.read_bytes())

    def test_restore_to_new_machine(self):
        archive = self.make_backup()
        destination = self.root / 'new-machine/verwrity/user_files'
        self.assertIsNone(backup.restore_backup(archive, destination, self.key))
        self.assertTrue((destination / 'A story/scene.txt').is_file())

    def test_wrong_key_never_touches_destination(self):
        archive = self.make_backup()
        destination = self.root / 'new/untouched'
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(archive, destination, os.urandom(32))
        self.assertFalse(destination.parent.exists())
        self.assertFalse(self.source.with_name('user_files.bu1').exists())

    def test_tampering_and_truncation(self):
        archive = self.make_backup()
        original = archive.read_bytes()
        for data in (original[:5], original[:-1], original[:50] + bytes([original[50] ^ 1]) + original[51:]):
            archive.write_bytes(data)
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(archive, self.source, self.key)
            self.assertFalse(self.source.with_name('user_files.bu1').exists())

    def test_unsafe_paths_and_case_collisions(self):
        for names in (['../escape'], ['/absolute'], ['C:/escape'], ['a\\b'], ['CON.txt'],
                      ['file.'], ['a/./b'], ['a//b'], ['A.txt', 'a.txt'], ['a', 'a/b'],
                      ['caf\u00e9.txt', 'cafe\u0301.txt']):
            with self.subTest(names=names):
                archive = self.encrypted_zip(names)
                with self.assertRaises(backup.BackupError):
                    backup.restore_backup(archive, self.source, self.key)
                self.assertFalse(self.source.with_name('user_files.bu1').exists())

    def test_symlink_in_zip_rejected(self):
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.encrypted_zip([info])
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(archive, self.source, self.key)

    def test_source_symlink_rejected_and_partial_removed(self):
        (self.source / 'link').symlink_to(self.root)
        with self.assertRaises(backup.BackupError):
            self.make_backup()
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_nested_backup_destination_rejected(self):
        destination = self.source / 'backups'
        destination.mkdir()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.source, destination, self.key)

    def test_unavailable_destination_not_created(self):
        destination = self.root / 'disconnected/backups'
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.source, destination, self.key)
        self.assertFalse(destination.parent.exists())

    def test_nonportable_source_does_not_publish_unrestorable_backup(self):
        (self.source / 'CON.txt').write_text('not portable')
        with self.assertRaises(backup.BackupError):
            self.make_backup()
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_cancelled_backup_removes_partial(self):
        with patch.object(backup, 'encrypt_stream', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.make_backup()
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_failed_encryption_cleans_partial(self):
        with patch.object(backup, 'encrypt_stream', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.make_backup()
        self.assertEqual(list(self.cloud.iterdir()), [])

    def test_failed_publish_rolls_back_original_folder(self):
        archive = self.make_backup()
        real_rename = Path.rename
        def fail_staged(path, target):
            if '.verwrity-restore-' in str(path):
                raise OSError('simulated failed publish')
            return real_rename(path, target)
        with patch.object(Path, 'rename', fail_staged):
            with self.assertRaises(OSError):
                backup.restore_backup(archive, self.source, self.key)
        self.assertTrue((self.source / 'A story/scene.txt').exists())
        self.assertFalse(self.source.with_name('user_files.bu1').exists())
        self.assertEqual(list(self.source.parent.glob('.verwrity-restore-*')), [])

    def test_corrupt_zip_never_touches_original(self):
        plain = io.BytesIO(b'not a zip')
        archive = self.cloud / 'bad.vbackup'
        with archive.open('wb') as output:
            backup.encrypt_stream(plain, output, self.key)
        with self.assertRaises(zipfile.BadZipFile):
            backup.restore_backup(archive, self.source, self.key)
        self.assertFalse(self.source.with_name('user_files.bu1').exists())

    def test_dangling_backup_suffix_is_not_reused(self):
        self.source.with_name('user_files.bu1').symlink_to(self.root / 'missing')
        self.assertEqual(backup.next_previous(self.source).name, 'user_files.bu2')

    def test_key_export_import_and_no_overwrite(self):
        config = self.root / 'config.json'
        key = base64.b64encode(self.key).decode('ascii')
        data = {'version': 1, 'key': key, 'source': str(self.source), 'destinations': [str(self.cloud)]}
        backup.private_json(config, data, exclusive=True)
        self.assertEqual(backup.load_config(config), data)
        self.assertEqual(backup.import_key_file(config), key)
        with self.assertRaises(FileExistsError):
            backup.private_json(config, {'key': 'wrong'}, exclusive=True)
        self.assertEqual(backup.import_key_file(config), key)
        if os.name != 'nt':
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_invalid_config_and_keys(self):
        path = self.root / 'config.json'
        for value in ('{}', 'broken', '[]', json.dumps({'version': 2})):
            path.write_text(value)
            with self.assertRaises(backup.BackupError):
                backup.load_config(path)
        for key in ('bad!', base64.b64encode(b'short').decode()):
            with self.assertRaises(backup.BackupError):
                backup.decode_key(key)

    def test_macos_cloud_detection_multiple_accounts(self):
        icloud = self.root / 'Library/Mobile Documents/com~apple~CloudDocs'
        google1 = self.root / 'Library/CloudStorage/GoogleDrive-one/My Drive'
        google2 = self.root / 'Library/CloudStorage/GoogleDrive-two/My Drive'
        for folder in (icloud, google1, google2):
            folder.mkdir(parents=True)
        found = backup.detect_cloud_folders(self.root, 'darwin')
        self.assertIn(('iCloud Drive', icloud), found)
        self.assertIn(('Google Drive', google1), found)
        self.assertIn(('Google Drive', google2), found)

    def test_windows_home_cloud_detection(self):
        icloud = self.root / 'iCloudDrive'
        google = self.root / 'Google Drive/My Drive'
        icloud.mkdir()
        google.mkdir(parents=True)
        found = backup.detect_cloud_folders(self.root, 'win32')
        self.assertIn(('iCloud Drive', icloud), found)
        self.assertIn(('Google Drive', google), found)
        self.assertNotIn(('Google Drive', google.parent), found)

    def test_menu_setup_and_backup_with_export(self):
        config_path = self.root / 'settings/config.json'
        usb = self.root / 'usb'
        usb.mkdir()
        exported = usb / 'key.json'
        with patch.object(backup, 'detect_cloud_folders', return_value=[('Test cloud', self.cloud)]), patch('builtins.input', side_effect=[str(self.source), '1', '1']):
            config = backup.setup(config_path)
        with patch('builtins.input', side_effect=[str(exported)]):
            backup.export_key(config)
        self.assertEqual(backup.import_key_file(exported), config['key'])
        with patch('builtins.input', side_effect=['1', '1', 'y', '6']):
            backup.menu(config, config_path)
        self.assertEqual(len(list((self.cloud / 'Verwrity Backups').glob('*.vbackup'))), 1)


if __name__ == '__main__':
    unittest.main()

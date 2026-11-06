"""Realistic Verwrity layout fixtures; no real user data is accessed."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

import backup


def project_bytes(identity, title, text):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as project:
        project.writestr('manifest.json', json.dumps({'archive_version': 6, 'project_id': identity, 'title': title}))
        project.writestr('manuscript.md', text)
        project.writestr('wiki.json', '[{"title": "My notes"}]')
        project.writestr('scene_revisions.json', '["earlier text"]')
    return stream.getvalue()


def write_project(root, relative, identity, title, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(project_bytes(identity, title, text))
    return path


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mac = self.root / 'mac/user_files'
        self.pc = self.root / 'pc/user_files'
        self.cloud = self.root / 'cloud'
        self.cloud.mkdir()
        self.key = os.urandom(32)
        self.a, self.b = str(uuid4()), str(uuid4())
        self.draft_a = f'local/drafts/{self.a}'
        for machine in (self.mac, self.pc):
            write_project(machine, 'local/Story A.verwrity.zip', self.a, 'Story A', 'original A')
            write_project(machine, 'local/Story B.verwrity.zip', self.b, 'Story B', 'original B')
            write_project(machine, f'{self.draft_a}/current.verwrity.zip', self.a, 'Story A', 'original draft A')
            (machine / 'local/preferences.json').write_text('preferences')

    def story(self, root, identity=None):
        return next(s for s in backup.local_stories(root) if s.project_id == (identity or self.a))

    def test_exact_mac_single_restore_pc_full_backup_mac_full_restore_workflow(self):
        write_project(self.mac, 'local/Story A.verwrity.zip', self.a, 'Story A', 'Mac saved A')
        latest_a = write_project(self.mac, f'{self.draft_a}/current.verwrity.zip', self.a, 'Story A', 'Mac latest draft A').read_bytes()
        latest_b = write_project(self.pc, 'local/Story B.verwrity.zip', self.b, 'Story B', 'PC latest B').read_bytes()
        draft_b = f'local/drafts/{self.b}/current.verwrity.zip'
        latest_draft_b = write_project(self.pc, draft_b, self.b, 'Story B', 'PC latest draft B').read_bytes()
        (self.pc / 'local/preferences.json').write_text('PC settings')
        mac_full = backup.create_backup(self.mac, self.cloud, self.key)
        scope, stories = backup.inspect_backup(mac_full, self.key)
        self.assertEqual(scope, 'all')
        selected = next(s for s in stories if s.project_id == self.a)
        previous = backup.restore_backup(mac_full, self.pc, self.key, story=selected)
        self.assertEqual(len(previous), 2)
        self.assertEqual((self.pc / self.draft_a / 'current.verwrity.zip').read_bytes(), latest_a)
        self.assertEqual((self.pc / 'local/Story B.verwrity.zip').read_bytes(), latest_b)
        self.assertEqual((self.pc / draft_b).read_bytes(), latest_draft_b)
        self.assertEqual((self.pc / 'local/preferences.json').read_text(), 'PC settings')
        self.assertFalse(self.pc.with_name('user_files.bu1').exists())
        pc_full = backup.create_backup(self.pc, self.cloud, self.key)
        previous_mac = backup.restore_backup(pc_full, self.mac, self.key)
        self.assertEqual(previous_mac.name, 'user_files.bu1')
        self.assertEqual((self.mac / self.draft_a / 'current.verwrity.zip').read_bytes(), latest_a)
        self.assertEqual((self.mac / 'local/Story B.verwrity.zip').read_bytes(), latest_b)
        self.assertEqual((self.mac / draft_b).read_bytes(), latest_draft_b)

    def test_single_story_backup_contains_only_selected_story_and_drafts(self):
        archive = backup.create_backup(self.mac, self.cloud, self.key, story=self.story(self.mac))
        self.assertIn('verwrity-story-', archive.name)
        with archive.open('rb') as source, tempfile.TemporaryFile() as plain:
            backup.decrypt_stream(source, plain, self.key)
            with zipfile.ZipFile(plain) as contents:
                self.assertEqual(backup.archive_scope(contents), 'story')
                names = contents.namelist()
                self.assertIn('local/Story A.verwrity.zip', names)
                self.assertIn(f'{self.draft_a}/current.verwrity.zip', names)
                self.assertNotIn('local/Story B.verwrity.zip', names)
                self.assertNotIn('local/preferences.json', names)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(archive, self.pc, self.key)
        scope, stories = backup.inspect_backup(archive, self.key)
        self.assertEqual(len(stories), 1)
        backup.restore_backup(archive, self.pc, self.key, story=stories[0])
        self.assertTrue((self.pc / 'local/Story B.verwrity.zip').exists())

    def test_repeated_story_restores_keep_numbered_files_and_draft_directories(self):
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        story = backup.inspect_backup(archive, self.key)[1][0]
        backup.restore_backup(archive, self.pc, self.key, story=story)
        backup.restore_backup(archive, self.pc, self.key, story=story)
        for suffix in ('.bu1', '.bu2'):
            self.assertTrue((self.pc / ('local/Story A.verwrity.zip' + suffix)).exists())
            self.assertTrue((self.pc / (self.draft_a + suffix)).is_dir())

    def test_draft_only_story_and_title_prefer_current_recovery(self):
        (self.mac / 'local/Story A.verwrity.zip').unlink()
        latest = write_project(self.mac, f'{self.draft_a}/current.verwrity.zip', self.a, 'Renamed working story', 'latest').read_bytes()
        write_project(self.mac, f'{self.draft_a}/previous-1.verwrity.zip', self.a, 'Old name', 'earlier')
        story = self.story(self.mac)
        self.assertEqual(story.title, 'Renamed working story')
        archive = backup.create_backup(self.mac, self.cloud, self.key, story=story)
        destination = self.root / 'new-computer/user_files'
        selection = backup.inspect_backup(archive, self.key)[1][0]
        backup.restore_backup(archive, destination, self.key, story=selection)
        self.assertEqual((destination / self.draft_a / 'current.verwrity.zip').read_bytes(), latest)
        self.assertEqual(len(backup.local_stories(destination)), 1)

    def test_saved_only_story_retires_stale_target_drafts(self):
        import shutil
        shutil.rmtree(self.mac / self.draft_a)
        archive = backup.create_backup(self.mac, self.cloud, self.key, story=self.story(self.mac))
        selected = backup.inspect_backup(archive, self.key)[1][0]
        backup.restore_backup(archive, self.pc, self.key, story=selected)
        self.assertFalse((self.pc / self.draft_a).exists())
        self.assertTrue((self.pc / (self.draft_a + '.bu1') / 'current.verwrity.zip').exists())

    def test_renamed_story_matched_by_identity(self):
        (self.pc / 'local/Story A.verwrity.zip').rename(self.pc / 'local/Renamed A.verwrity.zip')
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        selected = next(s for s in backup.inspect_backup(archive, self.key)[1] if s.project_id == self.a)
        backup.restore_backup(archive, self.pc, self.key, story=selected)
        self.assertFalse((self.pc / 'local/Renamed A.verwrity.zip').exists())
        self.assertTrue((self.pc / 'local/Renamed A.verwrity.zip.bu1').exists())
        self.assertTrue((self.pc / 'local/Story A.verwrity.zip').exists())

    def test_same_filename_different_identity_keeps_other_story(self):
        other = write_project(self.pc, 'local/Story A.verwrity.zip', str(uuid4()), 'Unrelated Story A', 'PC original').read_bytes()
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        selected = next(s for s in backup.inspect_backup(archive, self.key)[1] if s.project_id == self.a)
        backup.restore_backup(archive, self.pc, self.key, story=selected)
        self.assertEqual((self.pc / 'local/Story A.verwrity.zip').read_bytes(), other)
        self.assertEqual(backup.story_identity(self.pc / 'local/Story A (2).verwrity.zip')[0], self.a)

    def test_partial_story_publish_failure_rolls_back_every_component(self):
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        selected = next(s for s in backup.inspect_backup(archive, self.key)[1] if s.project_id == self.a)
        before = {p.relative_to(self.pc): p.read_bytes() for p in self.pc.rglob('*') if p.is_file()}
        real_rename = Path.rename
        installed = []
        def fail_second_incoming(path, target):
            if '.verwrity-restore-' in str(path):
                installed.append(path)
                if len(installed) == 2:
                    raise OSError('Failed second component')
            return real_rename(path, target)
        with patch.object(Path, 'rename', fail_second_incoming):
            with self.assertRaises(OSError):
                backup.restore_backup(archive, self.pc, self.key, story=selected)
        after = {p.relative_to(self.pc): p.read_bytes() for p in self.pc.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_unauthenticated_story_backup_leaves_new_destination_untouched(self):
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        story = self.story(self.mac)
        new = self.root / 'new/user_files'
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(archive, new, os.urandom(32), story=story)
        self.assertFalse(new.parent.exists())

    def test_story_user_symlink_rejected_before_touching_other_directory(self):
        archive = backup.create_backup(self.mac, self.cloud, self.key)
        selected = next(s for s in backup.inspect_backup(archive, self.key)[1] if s.project_id == self.a)
        new = self.root / 'new/user_files'
        new.mkdir(parents=True)
        (new / 'local').symlink_to(self.pc / 'local', target_is_directory=True)
        before = (self.pc / 'local/Story A.verwrity.zip').read_bytes()
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(archive, new, self.key, story=selected)
        self.assertEqual((self.pc / 'local/Story A.verwrity.zip').read_bytes(), before)

    def test_corrupt_story_manifest_skipped_and_users_kept_separate(self):
        (self.mac / 'local/Broken.verwrity.zip').write_bytes(b'bad zip')
        write_project(self.mac, 'other/Story A.verwrity.zip', self.a, 'Story A', 'other user')
        stories = backup.local_stories(self.mac)
        self.assertEqual(len(stories), 3)
        self.assertEqual({s.user for s in stories if s.project_id == self.a}, {'local', 'other'})

    def test_menu_selective_backup_and_full_backup_selective_restore(self):
        config = {'source': str(self.mac), 'key': base64.b64encode(self.key).decode(), 'destinations': [str(self.cloud)]}
        with patch('builtins.input', side_effect=['1', '2', '1', 'y', '6']):
            backup.menu(config, self.root / 'config.json')
        archive = list(self.cloud.glob('*.vbackup'))[0]
        self.assertEqual(len(backup.inspect_backup(archive, self.key)[1]), 1)
        config['source'] = str(self.pc)
        with patch('builtins.input', side_effect=['2', '1', '1', 'RESTORE', '6']):
            backup.menu(config, self.root / 'config.json')
        self.assertTrue((self.pc / 'local/Story A.verwrity.zip.bu1').exists())
        archive.unlink()
        backup.create_backup(self.mac, self.cloud, self.key)
        with patch('builtins.input', side_effect=['2', '1', '2', '1', 'RESTORE', '6']):
            backup.menu(config, self.root / 'config.json')
        self.assertTrue((self.pc / 'local/Story A.verwrity.zip.bu2').exists())


if __name__ == '__main__':
    unittest.main()

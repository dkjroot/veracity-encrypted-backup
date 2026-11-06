"""Menu-driven encrypted backups. All archive contents stay encrypted in cloud storage."""
from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import string
import sys
import tempfile
import unicodedata
import uuid
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.json"
MAGIC = b"VWRBACK\x01"
CHUNK = 1024 * 1024
SUFFIX = ".vbackup"


class BackupError(Exception):
    """An actionable error that can be shown without a traceback."""


@dataclass(frozen=True)
class Story:
    title: str
    user: str
    project_id: str
    paths: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.title} — {self.user} ({self.project_id[:8]})"


def story_identity(stream) -> tuple[str, str]:
    """Read only the small manifest; don't depend on an installed Verwrity."""
    with zipfile.ZipFile(stream) as project:
        member = project.getinfo("manifest.json")
        if member.file_size > 65536:
            raise ValueError("Oversized story manifest")
        manifest = json.loads(project.read(member))
        if not isinstance(manifest["project_id"], str):
            raise ValueError("Invalid story identity")
        identity = str(uuid.UUID(manifest["project_id"]))
        title = manifest["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Missing story title")
        # Titles are displayed in a terminal, never used as paths.
        title = "".join(c if c.isprintable() else " " for c in title)[:200]
        return identity, title


def collect_stories(names: list[str], read_identity) -> list[Story]:
    """Group a user's named archives and recovery snapshots by project ID."""
    groups = {}
    for name in sorted(names):
        parts = PurePosixPath(name).parts
        saved = len(parts) == 2 and parts[-1].endswith(".verwrity.zip")
        draft = (len(parts) == 4 and parts[1] == "drafts" and
                 parts[-1] in ("current.verwrity.zip", "previous-1.verwrity.zip", "previous-2.verwrity.zip"))
        if not (saved or draft):
            continue
        try:
            identity, title = read_identity(name)
            if draft and parts[2] != identity:
                continue
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
            continue
        key = (parts[0], identity)
        # Recovery current is the working copy; saved archives may be older.
        rank = 0 if saved else {"current.verwrity.zip": 3, "previous-1.verwrity.zip": 2, "previous-2.verwrity.zip": 1}[parts[-1]]
        group = groups.setdefault(key, {"title": title, "rank": -1, "paths": set()})
        if rank > group["rank"]:
            group.update(title=title, rank=rank)
        group["paths"].add(name if saved else "/".join(parts[:3]))
    return sorted((Story(group["title"], user, identity, tuple(sorted(group["paths"])))
                   for (user, identity), group in groups.items()), key=lambda s: (s.title.casefold(), s.user, s.project_id))


def local_stories(source: Path) -> list[Story]:
    if not source.is_dir() or source.is_symlink():
        raise BackupError(f"Can't find a regular project folder at {source}.")
    paths = []
    for user in source.iterdir():
        if not user.is_dir() or user.is_symlink():
            continue
        paths.extend(user.glob("*.verwrity.zip"))
        drafts = user / "drafts"
        if drafts.is_dir() and not drafts.is_symlink():
            for directory in drafts.iterdir():
                if directory.is_dir() and not directory.is_symlink():
                    paths.extend(directory.glob("*.verwrity.zip"))
    names = [p.relative_to(source).as_posix() for p in paths if not p.is_symlink()]
    return collect_stories(names, lambda name: story_identity(source / name))


def archived_stories(archive: zipfile.ZipFile) -> list[Story]:
    def identity(name):
        with archive.open(name) as stream:
            return story_identity(stream)
    return collect_stories(archive.namelist(), identity)


def archive_scope(archive: zipfile.ZipFile) -> str:
    if not archive.comment:
        return "all"  # Original full backups have no metadata.
    try:
        metadata = json.loads(archive.comment)
        if metadata["verwrity_backup"] == 1 and metadata["scope"] in ("all", "story"):
            return metadata["scope"]
    except (ValueError, KeyError, TypeError):
        pass
    raise BackupError("Unrecognised backup metadata. This may need a newer version of the tool.")


def matches_story(name: str, story: Story) -> bool:
    return any(name.rstrip("/") == root or name.startswith(root + "/") for root in story.paths)


def checked_local_path(source: Path, relative: str) -> Path:
    path = source
    for part in PurePosixPath(relative).parts:
        if part in (".", "..") or ":" in part or "\\" in part or part == "/":
            raise BackupError("Unsafe story path.")
        path = path / part
        if path.is_symlink():
            raise BackupError(f"Can't use a link as a story location: {path}")
    return path


def path_from_text(value: str) -> Path:
    return Path(os.path.expandvars(value.strip().strip('"').strip("'"))).expanduser().absolute()


def decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise BackupError("That key isn't valid. Import the original exported key file.") from exc
    if len(key) != 32:
        raise BackupError("The encryption key must contain exactly 32 bytes.")
    return key


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def private_json(path: Path, data: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        # O_EXCL also works on FAT/exFAT USB sticks, which lack hard links.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".settings-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != 1:
            raise ValueError("version")
        decode_key(data["key"])
        if not isinstance(data["source"], str) or not isinstance(data["destinations"], list):
            raise ValueError("paths")
        if not all(isinstance(p, str) for p in data["destinations"]):
            raise ValueError("destinations")
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"Couldn't read settings in {path}. Keep this file safe: it holds your key.") from exc


def detect_cloud_folders(home: Path | None = None, platform: str | None = None) -> list[tuple[str, Path]]:
    home = home or Path.home()
    platform = platform or sys.platform
    candidates: list[tuple[str, Path]] = []
    if platform == "darwin":
        candidates.append(("iCloud Drive", home / "Library/Mobile Documents/com~apple~CloudDocs"))
        candidates.extend(("Google Drive", p / "My Drive") for p in
                          sorted((home / "Library/CloudStorage").glob("GoogleDrive-*")))
        for root in (Path("/Volumes/GoogleDrive"), Path("/Volumes/Google Drive")):
            candidates.append(("Google Drive", root / "My Drive"))
    elif platform == "win32":
        for name in ("iCloudDrive", "iCloud Drive"):
            candidates.append(("iCloud Drive", home / name))
        candidates.append(("iCloud Drive", home / "Apple/CloudDocs"))
        # Explorer's registered sync roots also reveal relocated iCloud folders.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager") as roots:
                for index in range(winreg.QueryInfoKey(roots)[0]):
                    name = winreg.EnumKey(roots, index)
                    if "icloud" not in name.lower():
                        continue
                    with winreg.OpenKey(roots, name + r"\UserSyncRoots") as entries:
                        for j in range(winreg.QueryInfoKey(entries)[1]):
                            _, value, _ = winreg.EnumValue(entries, j)
                            candidates.append(("iCloud Drive", Path(os.path.expandvars(value))))
        except (ImportError, OSError):
            pass
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:/")
            candidates.append(("Google Drive", root / "My Drive"))
    for name in ("Google Drive", "GoogleDrive"):
        candidates.append(("Google Drive", home / name / "My Drive"))
        candidates.append(("Google Drive", home / name))
    found, seen = [], set()
    for label, path in candidates:
        if path.is_dir() and str(path.resolve()) not in seen:
            # Prefer My Drive over its parent when both exist.
            if label == "Google Drive" and (path / "My Drive").is_dir():
                continue
            seen.add(str(path.resolve()))
            found.append((label, path))
    return found


def is_within(child: Path, parent: Path) -> bool:
    return child.resolve().is_relative_to(parent.resolve())


def encrypt_stream(source, output, key: bytes) -> None:
    nonce = os.urandom(12)
    header = MAGIC + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    output.write(header)
    while block := source.read(CHUNK):
        output.write(encryptor.update(block))
    output.write(encryptor.finalize())
    output.write(encryptor.tag)


def decrypt_stream(source, output, key: bytes) -> None:
    source.seek(0, 2)
    size = source.tell()
    if size < len(MAGIC) + 12 + 16:
        raise BackupError("This backup is incomplete or isn't a Verwrity backup.")
    source.seek(0)
    header = source.read(len(MAGIC) + 12)
    if header[:len(MAGIC)] != MAGIC:
        raise BackupError("Unrecognised backup format or version.")
    source.seek(-16, 2)
    tag = source.read(16)
    source.seek(len(header))
    decryptor = Cipher(algorithms.AES(key), modes.GCM(header[-12:], tag)).decryptor()
    decryptor.authenticate_additional_data(header)
    remaining = size - len(header) - 16
    try:
        while remaining:
            block = source.read(min(CHUNK, remaining))
            if not block:
                raise BackupError("The backup download is incomplete. Let your cloud app finish syncing.")
            remaining -= len(block)
            output.write(decryptor.update(block))
        output.write(decryptor.finalize())
    except InvalidTag as exc:
        raise BackupError("Couldn't unlock this backup: wrong key, damaged file, or incomplete cloud sync. Your files haven't been changed.") from exc
    output.seek(0)


def create_backup(source: Path, destination: Path, key: bytes, *, story: Story | None = None) -> Path:
    source, destination = source.absolute(), destination.absolute()
    if source.is_symlink() or not source.is_dir():
        raise BackupError(f"Can't find a regular project folder at {source}.")
    if is_within(destination, source):
        raise BackupError("Choose a backup folder outside your project folder.")
    # Do not silently recreate a disconnected cloud mount as a local folder.
    if not destination.is_dir():
        raise BackupError(f"Backup folder is unavailable: {destination}. Check your cloud app or settings.")
    if story is not None and story not in local_stories(source):
        raise BackupError("That story has changed or is no longer available. Choose it again.")
    kind = "story-" if story is not None else ""
    name = f"verwrity-{kind}{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}{SUFFIX}"
    target = destination / name
    temporary = destination / (name + ".partial")
    try:
        # Plaintext is temporary and local, never in the destination's sync folder.
        with tempfile.TemporaryFile() as plain:
            with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                if story is not None:
                    archive.comment = json.dumps({"verwrity_backup": 1, "scope": "story"}).encode("utf-8")
                def walk_error(error):
                    raise error

                def paths_to_save():
                    roots = [checked_local_path(source, p) for p in story.paths] if story else [source]
                    for root in roots:
                        if root != source:
                            yield root
                        if root.is_dir():
                            for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                                yield from (Path(directory) / name for name in dirs + files)

                for path in paths_to_save():
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise BackupError(f"Can't back up links or special files: {path}")
                    before = path.stat()
                    archive.write(path, path.relative_to(source).as_posix())
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise BackupError("A file changed during backup. Close Verwrity and try again.")
            plain.seek(0)
            with temporary.open("xb") as encrypted:
                encrypt_stream(plain, encrypted, key)
                encrypted.flush()
                os.fsync(encrypted.fileno())
        # Check the actual saved ciphertext before publishing the finished name.
        with temporary.open("rb") as encrypted, tempfile.TemporaryFile() as verified:
            decrypt_stream(encrypted, verified, key)
            with zipfile.ZipFile(verified) as archive:
                validate_members(archive)
                if archive.testzip() is not None:
                    raise BackupError("Backup verification failed. Please try again.")
        os.rename(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def validate_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    seen = set()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
    for member in members:
        name = member.filename.rstrip("/")
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or "\\" in name or any(
            part in (".", "..") or part.endswith((".", " ")) or
            re.search(r'[<>:"|?*\x00-\x1f]', part) or part.split(".")[0].upper() in reserved
            for part in parts
        ) or "//" in name or any(p in (".", "..") for p in name.split("/")):
            raise BackupError(f"This archive has a filename that cannot be safely restored on Mac and Windows: {member.filename!r}")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise BackupError("Archives containing links or special files cannot be restored.")
        normalized = unicodedata.normalize("NFD", name).casefold()
        if normalized in seen:
            raise BackupError("This archive has duplicate filenames (possibly differing only in capitals).")
        seen.add(normalized)
    files = {unicodedata.normalize("NFD", m.filename.rstrip('/')).casefold() for m in members if not m.is_dir()}
    for member in members:
        if any(unicodedata.normalize("NFD", str(parent)).casefold() in files for parent in PurePosixPath(member.filename).parents if str(parent) != '.'):
            raise BackupError("This archive has conflicting file and folder names.")
    return members


def next_previous(path: Path) -> Path:
    index = 1
    while (candidate := path.with_name(f"{path.name}.bu{index}")).exists() or candidate.is_symlink():
        index += 1
    return candidate


def restore_backup(archive_path: Path, destination: Path, key: bytes, *, story: Story | None = None) -> Path | list[Path] | None:
    destination = destination.absolute()
    if destination.is_symlink() or destination == Path(destination.anchor):
        raise BackupError("Choose a normal project folder, not a link or a drive root.")
    # Authentication happens before even creating the destination's parent.
    with tempfile.TemporaryFile() as plain, archive_path.open("rb") as encrypted:
        decrypt_stream(encrypted, plain, key)
        with zipfile.ZipFile(plain) as archive:
            members = validate_members(archive)
            if story is None and archive_scope(archive) == "story":
                raise BackupError("This is a single-story backup. Choose the story to restore; it cannot replace all user data.")
            if story is not None and story not in archived_stories(archive):
                raise BackupError("That story isn't in this backup. Choose it again.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".verwrity-restore-", dir=destination.parent) as staging:
                staged = Path(staging) / "user_files"
                staged.mkdir()
                for member in members:
                    if story is not None and not matches_story(member.filename, story):
                        continue
                    target = staged.joinpath(*PurePosixPath(member.filename).parts)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output, CHUNK)
                if story is not None:
                    return publish_story(staged, destination, story)
                previous = None
                if destination.exists():
                    previous = next_previous(destination)
                    destination.rename(previous)
                try:
                    staged.rename(destination)
                except BaseException:
                    if previous is not None:
                        previous.rename(destination)
                    raise
                return previous


def publish_story(staged: Path, destination: Path, story: Story) -> list[Path]:
    """Replace just this story, rolling all its files back if any move fails."""
    roots = set(story.paths)
    if destination.exists():
        for existing in local_stories(destination):
            if (existing.user, existing.project_id) == (story.user, story.project_id):
                roots.update(existing.paths)
    # Even a saved-only archive must retire newer recovery drafts of this story.
    # Verwrity would otherwise reopen the stale working copy after the restore.
    roots.add(f"{story.user}/drafts/{story.project_id}")
    plans = []
    for relative in sorted(roots):
        target = checked_local_path(destination, relative)
        incoming = staged / relative
        if incoming.is_file() and target.is_file() and target.name.endswith(".verwrity.zip"):
            try:
                same_story = story_identity(target)[0] == story.project_id
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                same_story = False
            if not same_story:
                # A different story may have exactly the same title/filename.
                stem = target.name.removesuffix(".verwrity.zip")
                number = 2
                while (candidate := target.with_name(f"{stem} ({number}).verwrity.zip")).exists() or candidate.is_symlink():
                    number += 1
                target = candidate
        if incoming.exists() or target.exists():
            plans.append((incoming, target))
    # Create/check every parent before moving any existing files.
    for _, target in plans:
        target.parent.mkdir(parents=True, exist_ok=True)
    moved, installed = [], []
    try:
        for incoming, target in plans:
            if target.exists():
                previous = next_previous(target)
                target.rename(previous)
                moved.append((target, previous))
            if incoming.exists():
                incoming.rename(target)
                installed.append(target)
    except BaseException:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        for target, previous in reversed(moved):
            previous.rename(target)
        raise
    return [previous for _, previous in moved]


def inspect_backup(path: Path, key: bytes) -> tuple[str, list[Story]]:
    with path.open("rb") as encrypted, tempfile.TemporaryFile() as plain:
        decrypt_stream(encrypted, plain, key)
        with zipfile.ZipFile(plain) as archive:
            validate_members(archive)
            return archive_scope(archive), archived_stories(archive)


def choose(title: str, options: list[str], *, cancel: bool = True) -> int | None:
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")
    if cancel:
        print("  0. Back")
    while True:
        answer = input("Choose a number: ").strip()
        if cancel and answer == "0":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("Please enter one of the numbers above.")


def ask_path(prompt: str, default: Path | None = None) -> Path:
    value = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    if not value and default is None:
        raise BackupError("No folder selected.")
    return path_from_text(value) if value else default


def pick_destination(source: Path) -> Path | None:
    detected = detect_cloud_folders()
    index = choose("Where should your encrypted backups go?",
                   [f"{label} — {path}" for label, path in detected] + ["Choose another folder (cloud, USB or local)"])
    if index is None:
        return None
    if index < len(detected):
        root = detected[index][1]
        destination = root / "Verwrity Backups"
    else:
        root = ask_path("Existing backup drive or folder (you can paste its path)")
        destination = root / "Verwrity Backups"
    if not root.is_dir():
        raise BackupError("That folder isn't available. Start your cloud app or connect the drive first.")
    if is_within(destination, source):
        raise BackupError("Your backups need to live outside the project folder.")
    destination.mkdir(exist_ok=True)
    return destination


def import_key_file(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data["key"]
        decode_key(key)
        return key
    except (ValueError, TypeError, KeyError) as exc:
        raise BackupError("That isn't a valid exported key or settings file.") from exc


def setup(config_path: Path) -> dict | None:
    print("\nWelcome! Let's give your writing a safe place to live.")
    source = ask_path("Verwrity project folder", APP_DIR.parent / "verwrity/user_files")
    if is_within(config_path, source):
        raise BackupError("Keep this tool and its settings outside your project folder so your key isn't included in backups.")
    if not source.exists():
        print("That folder doesn't exist yet. You can restore a backup into it.")
    destination = pick_destination(source)
    if destination is None:
        return None
    index = choose("Do you already have a backup key?", ["Create a key for my first device", "Import my key from another device or USB stick"])
    if index is None:
        return None
    if index == 0:
        key = base64.b64encode(os.urandom(32)).decode("ascii")
    else:
        key = import_key_file(ask_path("Path to your exported key file (or the other device's config.json)"))
    data = {"version": 1, "key": key, "source": str(source), "destinations": [str(destination)]}
    private_json(config_path, data, exclusive=True)
    print(f"\nSettings saved locally: {config_path}\nKey ID: {fingerprint(decode_key(key))}")
    print("Use 'Export my key' to save it on your USB stick. You'll need the same key on both computers.")
    return data


def export_key(config: dict) -> None:
    print("\nYour exported key unlocks all backups made with it. Keep it on your USB stick, away from the cloud backups.")
    target = ask_path("Save key as", Path.home() / "verwrity-backup.key.json")
    if any(is_within(target, Path(p).parent) for p in config["destinations"]) or is_within(target, Path(config["source"])):
        raise BackupError("Save the key outside your projects and backup drives, for example on your USB stick.")
    private_json(target, {"version": 1, "key": config["key"]}, exclusive=True)
    print(f"Key saved to {target}")


def menu(config: dict, config_path: Path) -> None:
    while True:
        source = Path(config["source"])
        print(f"\nVerwrity encrypted backup\nProjects: {source}\nKey ID: {fingerprint(decode_key(config['key']))}")
        index = choose("What would you like to do?", ["Back up my writing", "Restore a backup", "Check a backup", "Backup folders and settings", "Export my key", "Quit"], cancel=False)
        try:
            if index == 5:
                return
            if index == 0:
                scope = choose("What would you like to back up?", ["All my user data", "One story"])
                if scope is None:
                    continue
                story = None
                if scope == 1:
                    stories = local_stories(source)
                    if not stories:
                        print("No readable Verwrity stories found. You can still back up all your user data.")
                        continue
                    selection = choose("Choose a story (includes its recovery drafts)", [s.label for s in stories])
                    if selection is None:
                        continue
                    story = stories[selection]
                if input("Save your work and close Verwrity first. Ready to back up? [y/N]: ").strip().lower() != "y":
                    continue
                for folder in config["destinations"]:
                    try:
                        print(f"\nBacking up to {folder}…", flush=True)
                        result = create_backup(source, Path(folder), decode_key(config["key"]), story=story)
                        print(f"Saved and verified: {result.name}")
                    except (OSError, BackupError, zipfile.BadZipFile) as exc:
                        print(f"Couldn't finish this destination: {exc}")
                print("Your cloud app handles uploading. Check it has finished syncing before switching computers.")
            elif index in (1, 2):
                paths = []
                for folder in config["destinations"]:
                    directory = Path(folder)
                    if directory.is_dir():
                        paths.extend(directory.glob(f"*{SUFFIX}"))
                    else:
                        print(f"Unavailable: {folder}")
                paths.sort(key=lambda p: (re.search(r"\d{8}T\d{6}Z", p.name).group() if re.search(r"\d{8}T\d{6}Z", p.name) else "", p.name), reverse=True)
                selection = choose("Choose a backup (newest first; 'story' marks a single-story backup)", [f"{p.name} — {p.parent}" for p in paths] + ["Browse to a backup file"])
                if selection is None:
                    continue
                selected = paths[selection] if selection < len(paths) else ask_path("Backup file path")
                if index == 2:
                    with selected.open("rb") as encrypted, tempfile.TemporaryFile() as plain:
                        decrypt_stream(encrypted, plain, decode_key(config["key"]))
                        with zipfile.ZipFile(plain) as archive:
                            validate_members(archive)
                            if archive.testzip() is not None:
                                raise BackupError("The archive contains damaged files.")
                    print("This backup unlocks with your key and all its files passed the integrity check.")
                else:
                    scope, stories = inspect_backup(selected, decode_key(config["key"]))
                    story = None
                    if scope == "all":
                        selection = choose("What would you like to restore?", ["All user data (replaces the whole folder)", "One story (keeps my other stories and settings)"])
                        if selection is None:
                            continue
                        single = selection == 1
                    else:
                        single = True
                    if single:
                        if not stories:
                            raise BackupError("No readable Verwrity stories found in this backup.")
                        selection = choose("Choose the story to restore", [s.label for s in stories])
                        if selection is None:
                            continue
                        story = stories[selection]
                    print(f"\nRestore {selected.name}\nInto: {source}")
                    if story is not None:
                        print(f"Story: {story.label}\nOnly this story will be restored. Your other stories and settings will stay as they are.")
                        print("This replaces the selected story; it doesn't merge edits within it. Existing story files and drafts will be kept as .bu1, .bu2, etc.")
                    else:
                        print("Your existing folder will be kept as user_files.bu1, .bu2, etc.")
                    print("Close Verwrity before continuing.")
                    if input("Type RESTORE to continue (Enter cancels): ").strip() != "RESTORE":
                        continue
                    previous = restore_backup(selected, source, decode_key(config["key"]), story=story)
                    print(f"Restored successfully to {source}")
                    if previous:
                        for path in previous if isinstance(previous, list) else [previous]:
                            print(f"Your previous files are safe at {path}")
            elif index == 3:
                settings(config, config_path)
            elif index == 4:
                export_key(config)
        except (OSError, BackupError, zipfile.BadZipFile, RuntimeError) as exc:
            print(f"\nCouldn't finish: {exc}")


def settings(config: dict, config_path: Path) -> None:
    print("\nBackup folders:")
    for path in config["destinations"]:
        print(f"  {path}")
    selected = choose("Settings", ["Add a backup folder", "Remove a backup folder", "Change project folder", "Show settings file location"])
    if selected == 0:
        destination = pick_destination(Path(config["source"]))
        if destination is not None and str(destination) not in config["destinations"]:
            config["destinations"].append(str(destination))
    elif selected == 1:
        if len(config["destinations"]) <= 1:
            print("Add another backup folder before removing this one.")
            return
        index = choose("Remove which folder? (Backups themselves will be kept.)", config["destinations"])
        if index is not None:
            config["destinations"].pop(index)
    elif selected == 2:
        source = ask_path("Project folder", Path(config["source"]))
        if is_within(config_path, source):
            raise BackupError("Choose a project folder that doesn't contain this tool's settings and key.")
        config["source"] = str(source)
    elif selected == 3:
        print(f"Local settings and key: {config_path}")
    private_json(config_path, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Use a different local settings file")
    args = parser.parse_args()
    try:
        config_path = args.config.expanduser().absolute()
        config = load_config(config_path) if config_path.exists() else setup(config_path)
        if config is not None:
            if is_within(config_path, Path(config["source"])):
                raise BackupError("Your project folder contains the settings and key. Move this tool outside the project folder first.")
            menu(config, config_path)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. See you next time.")
        return 0
    except (OSError, BackupError, zipfile.BadZipFile) as exc:
        print(f"\nCouldn't start: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

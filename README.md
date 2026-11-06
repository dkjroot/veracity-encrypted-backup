# Verwrity encrypted backup

Your writing belongs to you, and keeping a backup shouldn't mean handing somebody else a readable copy of it. This is a small companion to [Verwrity](https://github.com/dkjroot/verwrity) that zips up your project files, encrypts the whole archive on your computer, and puts the encrypted backup in your iCloud Drive or Google Drive folder. You can restore it on a Mac or a Windows PC using the same key.

It's a simple numbered menu. Choose **Back up my writing**, **Restore a backup**, or **Check a backup**, and it'll walk you through it. You can back up everything or just one story, and restore one story from either kind of backup. You can send backups to more than one place, including a USB drive or another local folder.

## Getting started

You'll need Python 3.11 or newer, and your cloud provider's desktop app installed and signed in. For Google Drive, that's **Drive for desktop**; for iCloud on Windows, that's **iCloud for Windows**. This tool uses their synced folders, so you don't need to give it your account details or set up an API. “Google Drive” here means your ordinary synced drive, rather than Google Cloud Storage buckets.

Keep this folder beside your Verwrity folder:

```text
my-projects/
    verwrity/
        user_files/
    verwrity-encrypted-backup/
        run.command
        run.cmd
```

On a **Mac**, double-click `run.command`, or open a terminal in this folder and run `./run.command`. On **Windows**, double-click `run.cmd`. The launcher creates a private Python environment and installs the encryption library the first time, so you'll need an internet connection for that first run. After that, encryption and decryption work locally.

If you'd rather set it up yourself:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python backup.py
```

On Windows, use `py -3 -m venv .venv`, then `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

The first run asks where your writing lives. The default is `../verwrity/user_files`, relative to this tool's folder, so the layout above works straight away. You can choose another folder if yours is somewhere else, or accept a folder that doesn't exist yet when you're setting up a new computer for a restore.

It then looks for iCloud Drive and Google Drive and offers the folders it finds. That includes modern Mac Google Drive folders, multiple Google accounts, Windows Google Drive letters, and the usual iCloud locations. It also checks registered Windows iCloud sync roots for relocated folders. Pick the one you want and it'll create a `Verwrity Backups` folder there. If your setup uses a location it can't find, choose another folder and paste its path. You can copy that path from Finder or Explorer.

Finally, create a new encryption key on your **first** computer. On your other computer, import the key you've already made.

## Keeping your key safe

The key lives in `config.json` beside the program. That file is local and ignored by Git, along with exported files ending in `.key.json`. Don't add it to a commit or put the tool itself in a shared cloud folder. The settings file contains the key in readable form, so protect it as you would any other private file. On a Mac it's created with permissions for your user only; on Windows, keep it in your own account's private folders.

Choose **Export my key** to save a separate copy on your USB stick. Give it a name such as `verwrity-backup.key.json`. Existing files aren't overwritten, and the menu refuses to export into your configured project or backup drive folders. The key ID shown in the menu is a short fingerprint, so you can check that both computers are using the same key without displaying the key itself.

**Keep that USB copy somewhere safe. If every copy of the key is lost, the backups can't be recovered.** Anyone who has both your key and your backups can read them, so keep the USB copy separate from the cloud archives.

You can also import the other computer's `config.json` during setup. Only the key is imported; the new computer gets its own project and cloud folder settings. Avoid creating a fresh key on the second computer, because that won't unlock your existing backups. The app doesn't rotate or silently replace an existing key.

## Backing up and moving between computers

1. Save your work and close Verwrity so its files aren't changing while they're being copied.
2. Run this tool and choose **Back up my writing**, then **All my user data** or **One story**. Each configured destination gets a new dated archive, with a unique name so previous backups are kept.
3. Wait for your cloud app to finish uploading before shutting down or switching computers. “Saved and verified” means the local backup was checked; the cloud app is responsible for uploading it.
4. On the other computer, let the cloud app finish syncing. Start this tool, import the same key during first-time setup, and choose **Restore a backup**.
5. Pick an archive from the list. For a full backup, choose whether to restore all user data or one story; for a single-story backup, pick its story. Check the destination and type `RESTORE` to confirm. You can also browse by entering a backup file's path, for example one copied onto a USB stick.

**All my user data** backs up the whole `user_files` folder, including all your projects and empty folders. **One story** gives you a list of stories by title, with the local user and a short project ID to tell similarly named stories apart. It includes the selected story's saved ZIPs and recovery drafts, which may contain work newer than the last named save. The ZIPs already contain the manuscript, wiki, revisions, and other project material. Other stories and account preferences aren't included in a single-story backup. If a story only exists as a recovery draft, you can back that up too. Unreadable project manifests aren't offered in the picker; a full backup still keeps those files for recovery.

These are complete snapshots of the selected data each time. The tool doesn't silently delete older archives or make you depend on a chain of incremental backups. You can remove old encrypted archives yourself when you're happy you no longer need them. Single-story archive filenames include `story`, but never the story's title; the story list is read locally after unlocking the backup.

Use **Backup folders and settings** to add another destination, remove one from the list without deleting its archives, or change the project folder. If one destination is unavailable, the app reports it and still tries the others.

### Combining work from your Mac and PC

Suppose you've worked on story A on the Mac and story B on the PC. This brings both computers up to date:

1. Back up from the Mac. A full backup is fine; you can pick out just one story when restoring.
2. On the PC, choose **Restore a backup**, select the Mac backup, choose **One story**, and pick story A. Story B and your PC preferences stay as they are.
3. Back up **All my user data** from the PC. That now contains the Mac's story A and the PC's story B.
4. On the Mac, restore **All user data** from that new PC backup.

Let your cloud app finish syncing between steps, and keep Verwrity closed while backing up and restoring. This replaces each selected story with the copy you choose. If you've edited the *same* story on both computers, it won't combine those edits automatically; choose which copy you want and use the numbered backup copies to recover anything else you need.

## Restoring without losing what's there

Restore checks the encryption first, then extracts and checks the ZIP locally before changing your project folder. A wrong key, incomplete download, damaged archive, or unsafe filename leaves your existing folder alone.

For **All user data**, if `user_files` already exists, it's moved to `user_files.bu1` before the restored folder takes its place. Next time it'll use `.bu2`, and so on, skipping names that are already taken. **The previous folder is kept in full**, including files that weren't in the selected backup.

For **One story**, only that story's saved files and recovery draft folder are replaced. Each existing item is kept beside it with a `.bu1`, `.bu2`, etc. suffix. Other stories, preferences, and shared dictionaries stay where they are. Stories are matched within their local user account by their internal project ID, so a changed title doesn't leave an older copy active under its previous filename. If an unrelated story has the same filename, it's left alone and the incoming file gets a numbered name. If the backup has no recovery drafts for the selected story, any existing drafts for it are moved aside too, so an old working copy won't unexpectedly reappear when you open Verwrity.

You can pick one story from existing full backups as well as new backups. A single-story backup is always restored as a story; the menu won't offer to use it to replace your whole `user_files` folder. Use this updated version of the tool on both devices for single-story backups.

You can open those previous folders to recover anything you need. They aren't encrypted, just like your normal working files, and they take up disk space until you remove them yourself. If the final move of the restored folder fails, the app attempts to put the original folder straight back. A power cut during those final moves may need manual recovery from the `.bu` folder.

Keep Verwrity closed during restore. You'll also need enough local free space for the decrypted archive, the restored files, and the previous folder. **Check a backup** lets you verify that an archive unlocks and that its files are intact without restoring anything.

## Encryption and file compatibility

Backups have a `.vbackup` extension. Inside is a ZIP encrypted with AES-256-GCM, using a randomly generated 256-bit key and a fresh random 96-bit nonce for each archive. Authentication detects an incorrect key or modified ciphertext. This uses the [Python cryptography library](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/), rather than ZIP's older password encryption. The archive format starts with the versioned `VWRBACK` header and nonce, followed by ciphertext and a 16-byte authentication tag; the header is authenticated too.

File contents and filenames inside the archive are encrypted. Its external name shows when the backup was made, and your cloud provider can still see the archive's size. Plaintext ZIP data is held in temporary storage and never intentionally written to the cloud backup folder. Decrypted data is only extracted after authentication succeeds. Use your computer's ordinary local temporary storage, rather than setting `TMPDIR`, `TEMP`, or `TMP` to a cloud-synced folder. Temporary storage isn't guaranteed to be securely erased; full-disk encryption protects local working files and temporary data when your computer is off.

To keep restores portable, archives with Windows-incompatible names, names differing only in capitals, or unsafe paths are rejected. Links and special files are rejected too, rather than following a link outside your project and backing up something you didn't mean to include. Ordinary Verwrity project files work on both systems. Backups preserve file contents and folder structure, rather than filesystem permissions, Finder tags, extended attributes, or original modification times.

## Running the checks

From the configured Python environment:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The checks use temporary projects, including the Mac-to-PC reconciliation workflow above, single-story backups, renamed stories, recovery drafts, same-name projects, round trips, damaged archives, wrong keys, numbered restore copies, key import and export, cloud-folder discovery, and recovery from a failed restore move. GitHub Actions is configured to run them on Windows, macOS, and Linux.

"""Publish the current 9J Helper version for the user, in one command.

    .venv\Scripts\python tools\publish_update.py [--notes "what's new"] [--share dad@example.com]

What it does, in this order:
  1. builds dist\9J-Helper.exe (skip with --skip-build) and runs its self-test,
  2. uploads the exe to Google Drive (a new version of the same file, so the link never changes),
  3. checks the upload arrived intact (MD5),
  4. only then writes the new version number (and link / notes) to the "9J Helper updates" sheet.

Nothing is written until you type "yes" (or pass --yes). It refuses to publish a version older than
the one the sheet already shows (override with --force).

Drive access is limited to files this script itself created ("drive.file"), so it cannot see the
rest of your Drive. The first run creates the Drive file and prints its link; send that link to the
user once (or pass --share EMAIL to share it for you). Later runs reuse the same file.

One-time setup: enable the "Google Drive API" in the same Google Cloud project as the app, and
approve the sign-in window the first time (a separate token, publish_token.json, is saved in
%APPDATA%\\9J Helper).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nine_j_helper import config, updates  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/drive.file",
          "https://www.googleapis.com/auth/spreadsheets"]
EXE = os.path.join(ROOT, "dist", "9J-Helper.exe")
_SAMPLE_NAME = os.path.join("Example 9J", "9J_504206_R M KERNER COMPANY 106096.pdf")
# The sample 9J lives in the project folder (or, in older setups, the folder above it).
SAMPLE_PDF = next((p for p in (os.path.join(ROOT, _SAMPLE_NAME),
                               os.path.join(os.path.dirname(ROOT), _SAMPLE_NAME)) if os.path.exists(p)),
                  os.path.join(ROOT, _SAMPLE_NAME))
DRIVE_NAME = "9J-Helper.exe"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"


class PublishError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def local_version() -> str:
    with open(os.path.join(ROOT, "nine_j_helper", "__init__.py"), encoding="utf-8-sig") as f:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise PublishError("Couldn't find __version__ in nine_j_helper/__init__.py")
    return m.group(1)


def file_id_from_link(link: str) -> Optional[str]:
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", link or "")
    return m.group(1) if m else None


def view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- Drive

class Drive:
    """The few Drive calls needed. `session` is a requests-style session that is already signed in."""

    def __init__(self, session):
        self.s = session

    def update_media(self, file_id: str, path: str) -> Optional[Dict]:
        """Upload `path` as the new content of an existing file. None if the file isn't reachable
        (deleted, or not created by this script), so the caller can create a fresh one."""
        with open(path, "rb") as f:
            r = self.s.patch(f"{UPLOAD}/files/{file_id}",
                             params={"uploadType": "media", "fields": "id,md5Checksum"},
                             data=f, headers={"Content-Type": "application/octet-stream"})
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.json()

    def create_empty(self, name: str) -> str:
        r = self.s.post(f"{API}/files", params={"fields": "id"},
                        json={"name": name, "mimeType": "application/octet-stream"})
        r.raise_for_status()
        return r.json()["id"]

    def share(self, file_id: str, email: str) -> None:
        r = self.s.post(f"{API}/files/{file_id}/permissions",
                        params={"sendNotificationEmail": "false"},
                        json={"role": "reader", "type": "user", "emailAddress": email})
        r.raise_for_status()


# ---------------------------------------------------------------- the sheet

def find_rows(rows: List[List[str]]) -> Dict[str, int]:
    """{'version': 1, 'download': 2, 'notes': 3} (1-based sheet rows, by the label in column A)."""
    out: Dict[str, int] = {}
    for i, r in enumerate(rows, start=1):
        if r and str(r[0]).strip().lower() in ("version", "download", "notes"):
            out.setdefault(str(r[0]).strip().lower(), i)
    return out


def publish(drive: Drive, book, version: str, exe_path: str, notes: Optional[str] = None,
            share: Optional[List[str]] = None, force: bool = False, log=print) -> Dict:
    """Upload the exe, verify it, and only then update the sheet. Returns what was done."""
    sheet = book.sheet1
    rows = sheet.get_values("A1:B20")
    pos = find_rows(rows)
    if "version" not in pos or "download" not in pos:
        raise PublishError("The updates sheet needs 'version' and 'download' in column A of its first tab.")
    cell = lambda name: (rows[pos[name] - 1][1].strip() if len(rows[pos[name] - 1]) > 1 else "")
    current, current_link = cell("version"), cell("download")
    if updates.is_newer(current, version) and not force:
        raise PublishError(
            f"The sheet already says version {current}, which is newer than this build ({version}). "
            "Fix the number in the sheet (or bump the version and rebuild), or use --force.")

    file_id = file_id_from_link(current_link)
    result = drive.update_media(file_id, exe_path) if file_id else None
    created = False
    if result is None:
        log("Creating the Drive file (first run, or the old one isn't reachable)...")
        file_id = drive.create_empty(DRIVE_NAME)
        result = drive.update_media(file_id, exe_path)
        created = True
        if result is None:
            raise PublishError("Drive refused the upload to the file it just created.")
    log(f"Uploaded {os.path.getsize(exe_path) / 1e6:.1f} MB to Drive file {file_id}.")

    local, remote = md5_of(exe_path), result.get("md5Checksum")
    if remote != local:
        raise PublishError(f"The uploaded file doesn't match (local {local}, Drive {remote}). "
                           "The sheet was NOT updated.")
    log("Checked: the file on Drive matches the one built here.")

    for email in share or []:
        drive.share(file_id, email)
        log(f"Shared the file with {email} (view).")

    link = view_link(file_id)
    title = sheet.title
    data = [{"range": f"'{title}'!B{pos['version']}", "values": [[version]]},
            {"range": f"'{title}'!B{pos['download']}", "values": [[link]]}]
    if notes is not None:
        if "notes" in pos:
            data.append({"range": f"'{title}'!B{pos['notes']}", "values": [[notes]]})
        else:
            log("The sheet has no 'notes' row, so the notes were not written.")
    book.values_batch_update({"valueInputOption": "RAW", "data": data})
    log(f"Updated the sheet: version {current or '(blank)'} -> {version}.")
    return {"file_id": file_id, "link": link, "created": created, "previous_version": current}


# ---------------------------------------------------------------- build / auth / main

def build(skip: bool) -> None:
    if skip:
        if not os.path.exists(EXE):
            raise PublishError(f"{EXE} doesn't exist; run without --skip-build.")
        return
    started = os.path.getmtime(EXE) if os.path.exists(EXE) else 0
    env = dict(os.environ, PYTHON=sys.executable)
    p = subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", "build_exe.ps1"],
                       cwd=ROOT, env=env)
    for junk in ("build", "9J-Helper.spec"):
        path = os.path.join(ROOT, junk)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else (
            os.remove(path) if os.path.exists(path) else None)
    if p.returncode != 0 or not os.path.exists(EXE) or os.path.getmtime(EXE) <= started:
        raise PublishError("The build didn't produce a new dist\\9J-Helper.exe.")


def selftest(pdf: str) -> None:
    if not os.path.exists(pdf):
        print(f"(Skipping the exe self-test: {pdf} not found.)")
        return
    out = os.path.join(config.app_dir(), "publish_selftest.txt")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run([EXE, "--selftest", pdf, out], timeout=180)
    text = open(out, encoding="utf-8").read().strip() if os.path.exists(out) else ""
    if not text.startswith("OK"):
        raise PublishError(f"The exe's self-test failed: {text or '(no output)'}")
    print(f"Self-test: {text}")


def authorize():
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    import gspread

    token = os.path.join(config.app_dir(), "publish_token.json")
    creds = Credentials.from_authorized_user_file(token, SCOPES) if os.path.exists(token) else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = config.client_secret_path()
            if not secret:
                raise PublishError("client_secret.json wasn't found (see the README's Google setup).")
            creds = InstalledAppFlow.from_client_secrets_file(secret, SCOPES).run_local_server(port=0)
        with open(token, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return gspread.authorize(creds), AuthorizedSession(creds)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build and publish the current 9J Helper version.")
    ap.add_argument("--notes", help="text for the 'notes' cell, shown in the user's update bar")
    ap.add_argument("--share", action="append", default=[], metavar="EMAIL",
                    help="also share the Drive file (view) with this Google account; repeatable")
    ap.add_argument("--skip-build", action="store_true", help="publish the exe already in dist\\")
    ap.add_argument("--force", action="store_true", help="allow publishing a version older than the sheet's")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--selftest-pdf", default=SAMPLE_PDF)
    args = ap.parse_args(argv)
    try:
        if not config.UPDATE_SHEET_ID:
            raise PublishError("config.UPDATE_SHEET_ID is empty.")
        version = local_version()
        build(args.skip_build)
        selftest(args.selftest_pdf)
        client, session = authorize()
        book = client.open_by_key(config.UPDATE_SHEET_ID)
        rows = book.sheet1.get_values("A1:B20")
        pos = find_rows(rows)
        shown = rows[pos["version"] - 1][1] if "version" in pos and len(rows[pos["version"] - 1]) > 1 else "(blank)"
        print(f"\nReady to publish version {version} (the sheet currently says {shown}).")
        if not args.yes and input('Type "yes" to upload to Drive and update the sheet: ').strip().lower() != "yes":
            print("Cancelled; nothing was changed.")
            return 1
        done = publish(Drive(session), book, version, EXE, notes=args.notes,
                       share=args.share, force=args.force)
    except PublishError as e:
        print(f"\nNOT published: {e}")
        return 1
    print(f"\nPublished {version}.")
    if done["created"]:
        print("A new Drive file was created. Send this link to the user ONCE (and share the file "
              f"with their Google account if you haven't):\n  {done['link']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

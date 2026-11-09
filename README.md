# 9J Helper

Reads a Hendrickson **9J Supplier Schedule** PDF and updates the orders Google Sheet
(e.g. "HAUX - RMKCo") with the latest schedule. No Python needed: it's a single Windows program.

## For Dad: using it

### Installing

1. Open the **Google Drive link** you were sent for **9J-Helper.exe** (sign in with your Google
   account if it asks). Click the **download** button at the top right, and save the file on your
   **Desktop**. There is nothing to install: the file *is* the program.
2. Double-click **9J-Helper.exe**. The first time, Windows may say *"Windows protected your PC"*:
   click **More info → Run anyway**.
   The window title shows the version (for example "9J Helper 0.4.1").

### Updating to a newer version

When a new version is ready, a **yellow bar** appears at the top of the window the next time you
pick a 9J: *"A new version of 9J Helper is available…"*. To update:

1. Click **Download** in the yellow bar. Drive opens; click the **download** button and save the
   file (your browser may add "(1)" to the name; that's fine).
2. **Close** 9J Helper.
3. Move the new file onto your Desktop and **replace** the old `9J-Helper.exe` (click **Replace**
   if asked). Your settings and Google sign-in are kept.
4. Double-click it. The window title should show the new version number.

If you never see the bar, you are already on the newest version. (The very first time you switch
from an older version without the bar, use the Drive link you were sent instead.)

### Using it

1. Click **Browse…** and pick the 9J PDF from your email/downloads.
2. The first time only:
   - Paste the link to your Google Sheet (open the sheet in your browser, copy the address bar).
   - A browser window opens to sign in to Google. You'll see *"Google hasn't verified this app"*:
     click **Advanced → Go to 9J Helper (unsafe)** and then **Continue**. (It's our own app;
     it only gets access to Google Sheets.)
3. Look over the list. It follows the order of the 9J, with **one thick box around each part's
   rows** (all the 010545-1 lines together, and so on; boxes in the order the parts appear on the
   9J, rows in date order), so you can check the app's work at a glance:
   - **green "new → row 35"**: will be added at that row
   - **red "REMOVE row 33"**: an old row that hasn't shipped; it is deleted and replaced by the 9J
     (it sits just above the line that replaces it)
   - **grey "skipped"**: that part + delivery date already **shipped**, so it's left alone
   - **red "choose a tab"**: the app didn't know which tab; it asks you, and remembers next time

   Use **Add rows to the sheet in this order** to pick how the new rows are written to the sheet:
   *Delivery date, then part* (default), *Part number, then date*, or *Order on the 9J*. The list
   always follows the 9J and stays grouped by part either way; the row numbers in it update to match. Your choice is remembered.
4. Click **Update Sheet**, then **Yes**.
5. The list then turns into a summary of what was done: **green rows were added, red rows were
   removed**. It stays until you pick another 9J with **Browse…**.

### How it updates the sheet (replace mode)
The 9J is the newest word on what's still owed, so for **every part that is on the 9J**:

1. Its **unshipped rows** are deleted from that part's tab. A row is unshipped when **Shipped Qty,
   Actual Ship Date and P/S are all empty** (anything typed in any of them, even a formula, means
   it has shipped and the row is kept). This includes dates the new 9J no longer lists.
2. **All of that part's 9J lines** are added after the last remaining order (into the "x" rows,
   adding rows if they run out), sorted by date, then part.

Rows for parts that are **not** on the 9J, and every shipped row, are never touched. A 9J line with
the same part and date as a shipped row is skipped.

### What it writes
Each added row gets:

| Sheet column | Value |
|---|---|
| Part Number | **Hendrickson Item #** |
| Delivery Date | Delivery Date |
| Req Qty | **Net Req Qty** |
| Ship to Customer | `=WORKDAY(C<row>,-2)` |
| Balance Due / Net Req Qty | `=D<row>-F<row>` |
| part number (Machined Only, col K) | the part number again |
| Location (when the tab has one) | code from the 9J Ship To: Haux / Dist / Mitc |

Everything else (Shipped Qty, ship dates, P/S, comments) is left for you. Firm and
planned lines are both added.

Formatting on new rows:
- The **Part Number text color** matches that part's most recent row on the tab (e.g. 010545-2
  stays blue). Brand-new parts keep the default color. Background and bold aren't changed.
- Columns **A–J get thin black borders** on all sides, like the rest of the order rows.
- The tab's **sort/filter range grows to include the new rows**, so they sort and filter with
  the rest of the table. (Your existing filter settings are kept; deleting rows shrinks it first, then it is grown back.)

Tabs are matched by part number only (the tab that already has that part). The PO number is
ignored. For a part that isn't on any tab yet, the app asks which tab to use and remembers it.
Other tabs (Keys, packaging, forecasts…) are never touched.

Settings and the Google sign-in are saved in `%APPDATA%\9J Helper\`. Delete that folder to start over.

## One-time Google setup (for whoever maintains the app)

The app needs a Google OAuth "Desktop" client so it can ask Dad's permission to edit his sheets.

1. Go to <https://console.cloud.google.com/>, create a project (e.g. "9J Helper").
2. **APIs & Services → Library →** enable **Google Sheets API**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type **External**, app name "9J Helper", your email as support/developer contact.
   - Scopes: add `.../auth/spreadsheets`.
   - **Audience → Publish app** (switch from *Testing* to *In production*). In Testing mode Google
     signs users out every 7 days. Verification isn't needed for personal use; users just see the
     "unverified app" screen once.
4. **Clients → Create client → Desktop app**. Download the JSON.
5. Make it available to the build:
   - **GitHub:** repo **Settings → Secrets and variables → Actions → New repository secret**,
     name `GOOGLE_CLIENT_SECRET_JSON`, value = the whole JSON file contents.
   - **Local:** save it as `client_secret.json` in the project folder (it's git-ignored).

> A desktop app's client secret can't really be kept secret (Google documents this), but
> keep it out of the repo anyway. Access to a sheet still requires the user to sign in and approve.

## Releasing a new version

**What the user gets** comes from Google Drive plus the updates sheet (next section), not from
GitHub: the repo is private, so the user can't open its Releases page.

**Optional, for your own records:** tagging builds the exe on GitHub and attaches it to a Release
that only people with repo access can see.

```
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions runs the tests, builds `9J-Helper.exe` and attaches it to a Release.
(Every push to a pull request also builds the exe as a downloadable workflow artifact.) Note that
this build uses the OAuth client secret stored in the repo's Actions secrets.

## Telling the user about a new version (private, no GitHub account needed)

The app can show a yellow **"A new version of 9J Helper is available"** bar with a **Download**
button. It reads the latest version from a small Google Sheet shared only with the people who use
the app (the app can already read Google Sheets, so there is no extra sign-in).

**One-time setup**
1. Create a Google Sheet "9J Helper updates". On its first tab put, in columns A and B:

   | A | B |
   |---|---|
   | version | `0.4.0` |
   | download | link to the exe in Google Drive |
   | notes | optional, shown in the bar |

2. Share that sheet (view access is enough) with each person who uses the app.
3. Upload `9J-Helper.exe` to Drive, share it with the same people, and put its link in `download`.
4. Put the sheet's ID (the long text between `/d/` and `/edit` in its address) in
   `UPDATE_SHEET_ID` in `nine_j_helper/config.py`, then build and release that version. Until this
   is filled in, the app does no update check.

**Each new version (one command)**

1. Bump `__version__` in `nine_j_helper/__init__.py`.
2. Run:

   ```
   .venv\Scripts\python tools\publish_update.py --notes "What's new"
   ```

   It builds the exe, runs its self-test, uploads it to Drive, checks the upload arrived intact,
   and only then writes the new version (and link and notes) into the updates sheet. It asks you
   to type `yes` first (or pass `--yes`) and refuses to publish a version older than the sheet's
   (`--force` overrides). Users see the yellow bar the next time they open a 9J. The check never
   blocks anything: if it fails, nothing is shown.

**One-time setup for the script**
- In the same Google Cloud project as the app, enable the **Google Drive API**
  (APIs & Services → Library).
- The first run opens a browser to approve access. The script only gets Google's `drive.file`
  permission, so it can only touch the Drive file it creates itself, not the rest of your Drive.
  Its sign-in is saved as `publish_token.json` in `%APPDATA%\9J Helper\`.
- The first run also **creates** the Drive file and prints its link. Send that link to the user
  once, and share the file with their Google account (or pass `--share their@email`). After that,
  every run uploads a new version of the same file, so the link never changes.

*Doing it by hand instead:* in Drive, open the exe, **Manage versions → Upload new version**, then
change `version` in the sheet. Upload first, then change the number.

**How the user upgrades:** click **Download** in the bar, close the app, replace the old
`9J-Helper.exe` with the new file, and open it. The window title shows the new version. Settings and
Google sign-in are kept.

## Development

```
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python run_9j_helper.py            # run the app
powershell -ExecutionPolicy Bypass -File build_exe.ps1   # build dist\9J-Helper.exe
```

To include the real sample tests (files are kept out of git), point `NINEJ_SAMPLES` at the folder
holding `Example 9J\...pdf` and `Copy of HAUX - RMKCo.xlsx`:
`$env:NINEJ_SAMPLES = "C:\path\to\samples"; .venv\Scripts\python -m pytest`

Dry run against an Excel export of the sheet (never modifies the export):

```
.venv\Scripts\python -m nine_j_helper.cli "path\to\9J.pdf" "Copy of HAUX - RMKCo.xlsx" [--sort date|part|9j] --out preview.xlsx
```

Code layout:

| File | Purpose |
|---|---|
| `nine_j_helper/nine_j.py` | PDF → header, items, schedule lines |
| `nine_j_helper/planner.py` | finds order tabs/columns, routes parts, skips duplicates, picks rows, sort order, groups for the list |
| `nine_j_helper/google_book.py` | Google sign-in, reading tabs, writing the plan |
| `nine_j_helper/xlsx_book.py` | same against a local .xlsx (tests / dry runs) |
| `nine_j_helper/app.py` | the window |
| `nine_j_helper/updates.py` | reads the latest version from the updates sheet |
| `nine_j_helper/group_table.py` | the boxed, color-filled list in the window |
| `nine_j_helper/config.py` | settings in `%APPDATA%\9J Helper` |

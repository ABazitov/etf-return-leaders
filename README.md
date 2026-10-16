# ETF Return Leaders

A web page that runs on your own computer and ranks three groups of people by the return they earned on ETFs over
the last 1, 3 and 5 years:

- **Fund managers** who run actively managed ETFs (badge EXACT: their funds' own returns).
- **Big investors** who file SEC Form 13F (badge APPROX: rebuilt from quarterly holdings reports).
- **Members of Congress**, from their STOCK Act trade reports (badge EST: trades are reported as dollar ranges).

Every number links back to the public document it came from. Anything skipped or doubtful is listed under "Data
issues" with the reason.

Not investment advice. Congressional reports may be used only for lawful, non-commercial purposes (Ethics in
Government Act); this tool is for personal use.

## What you need

- macOS or Windows (Linux should work with `run.sh` but is untested).
- Python 3.11 or newer: [python.org/downloads](https://www.python.org/downloads/). On Windows, tick "Add python.exe
  to PATH" in the installer.
- About 10 GB of free disk space and an internet connection. The first load downloads several GB of public data.
- An email address. The SEC requires a contact email in every request; it is sent only to the SEC.

## Install and start

1. Download the code: click **Code → Download ZIP** on GitHub and unzip it, or run
   `git clone https://github.com/ABazitov/etf-return-leaders.git`.
2. Open a terminal in that folder and start the app:
   - macOS / Linux: `./run.sh`
   - Windows (PowerShell or Command Prompt): `.\run.bat`
3. The first start installs what it needs into a `.venv` folder (a few minutes) and asks three questions:
   - your contact email for SEC requests (required);
   - an [OpenFIGI API key](https://www.openfigi.com/api) (optional, free; press Enter to skip, but the first load is
     much faster with one);
   - whether you accept the Senate site's terms of use (type `yes` to include the Senate, anything else to skip it).

   The answers are saved in `config.toml` in the same folder.
4. Open http://127.0.0.1:8765 in your browser. The page is only reachable from your own computer.

The first load takes a few hours; progress shows at the top of the page, and the boards fill in as data arrives.
Later starts are quick. Leave the terminal open while you use the page.

**Stop:** press Ctrl+C in the terminal. **Start again:** run `./run.sh` (or `.\run.bat`) again.

## Good to know

- **Senate data:** the Senate site refuses automated browsers, so the app opens a Chrome window, minimized, for a
  moment. If the site answers "Access Denied", the app waits 6 hours before trying again. To leave the Senate out,
  set `senate_enabled = false` in `config.toml`.
- **Your data** lives in the `data/` folder: the database, the downloaded originals and a log (`data/app.log`).
  Deleting `data/` starts over from scratch.
- **Settings** are in `config.toml`. Delete it to be asked the setup questions again.
- **Uninstall:** delete the folder. Nothing is installed elsewhere, except the browser Playwright downloads for the
  Senate source (in `~/Library/Caches/ms-playwright` on macOS, `%LOCALAPPDATA%\ms-playwright` on Windows).
- **How the numbers are made:** the Methodology section at the bottom of the page, and the full rules in
  [the design spec](docs/superpowers/specs/2026-10-08-etf-roi-tracker-design.md).

## For developers

```
.venv/bin/python -m pytest -q -o addopts="-m 'not e2e'"           # unit tests
.venv/bin/python -m pytest -q -o addopts="" -m e2e tests/e2e -k "not senate_live"   # browser tests
```

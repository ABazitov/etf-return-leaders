"""First-run questions (spec §15). Nothing is pre-filled."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

SENATE_TERMS = """The Senate financial disclosure site requires you to agree to this before searching:

Title 1 of the Ethics in Government Act of 1978, as amended, 5 U.S.C. app. § 105(c), states that it shall be
unlawful for any person to obtain or use a report: for any unlawful purpose; for any commercial purpose, other than
by news and communications media for dissemination to the general public; for determining or establishing the credit
rating of any individual; or for use, directly or indirectly, in the solicitation of money for any political,
charitable, or other purpose.

This app is for personal, non-commercial use. It will tick the site's agreement box on your behalf each session."""


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_setup(path: Path, ask: Callable[[str], str] = input) -> None:
    while True:
        email = ask("Contact email for SEC requests (the SEC requires one in every request): ").strip()
        if re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
            break
        print("That doesn't look like an email address. Try again.")
    key = ask("OpenFIGI API key (optional, press Enter to skip; makes the first load much faster): ").strip()
    print("\n" + SENATE_TERMS + "\n")
    senate = ask("Type yes to accept and include the Senate, anything else to skip it: ").strip().lower() == "yes"
    Path(path).write_text(
        f"sec_contact_email = {_q(email)}\nopenfigi_api_key = {_q(key)}\n"
        f"senate_enabled = {'true' if senate else 'false'}\nsenate_headed = false\nport = 8765\n")
    print(f"Saved {path}")


if __name__ == "__main__":
    run_setup(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.toml"))

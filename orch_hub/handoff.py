"""Hand the dashboard and its bearer token to the operator on this Mac.

After setup the token exists only in the mode-600 launchd plist, and the
dashboard accepts it only as a typed (or pasted) bearer credential: the design
deliberately has no URL, query, fragment, or cookie path for it. So the
hand-off is local and two-part — the token goes to the clipboard, the browser
opens the plain dashboard URL — and the secret never appears in a URL, in
``open``'s argv, in a log, or in a terminal the operator did not ask to print it
to.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

CLIPBOARD_TIMEOUT = 10.0


class HandoffError(RuntimeError):
    """Raised when the clipboard cannot take the token."""


Clipboard = Callable[[str], None]
Opener = Callable[[str], bool]


def copy_to_clipboard(text: str) -> None:
    """Put ``text`` on the macOS clipboard via ``pbcopy``'s stdin."""
    try:
        subprocess.run(
            ["pbcopy"],
            input=text,
            text=True,
            capture_output=True,
            timeout=CLIPBOARD_TIMEOUT,
            check=True,
        )
    except FileNotFoundError as exc:
        raise HandoffError("pbcopy is not available on this machine") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HandoffError(f"pbcopy failed: {exc}") from exc


def open_in_browser(url: str) -> bool:
    """Open ``url`` with the default browser; ``False`` when nothing could."""
    try:
        result = subprocess.run(
            ["open", url],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=CLIPBOARD_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

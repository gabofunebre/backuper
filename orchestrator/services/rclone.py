import re
import subprocess


def authorize_drive() -> str:
    """Run ``rclone authorize "drive"`` and return the authorization URL.

    The command normally prints a line containing the URL the user must open in
    their browser. This helper captures the output, extracts the first URL and
    terminates the process once found.
    """
    proc = subprocess.Popen(
        ["rclone", "authorize", "drive"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout is None:
        proc.kill()
        raise RuntimeError("failed to capture rclone output")

    url: str | None = None
    try:
        for line in proc.stdout:
            match = re.search(r"https?://\S+", line)
            if match:
                url = match.group(0)
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not url:
        raise RuntimeError("authorization URL not found")
    return url


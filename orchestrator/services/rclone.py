import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import IO


@dataclass
class AuthorizationSession:
    """State for an in-flight ``rclone authorize`` invocation."""

    remote: str
    process: subprocess.Popen[str]
    stdout: IO[str]
    stdin: IO[str]


_AUTH_SESSIONS: dict[str, AuthorizationSession] = {}
_AUTH_LOCK = threading.Lock()
_URL_TIMEOUT = 30.0
_TOKEN_TIMEOUT = 60.0


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _cleanup_session(session_id: str, terminate: bool = False) -> None:
    session: AuthorizationSession | None
    with _AUTH_LOCK:
        session = _AUTH_SESSIONS.pop(session_id, None)
    if not session:
        return
    proc = session.process
    try:
        if proc.poll() is None:
            if terminate:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for stream in (session.stdout, session.stdin):
            try:
                stream.close()
            except Exception:
                pass
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _cleanup_sessions_for_remote(remote: str) -> None:
    pending: list[str] = []
    with _AUTH_LOCK:
        for session_id, session in _AUTH_SESSIONS.items():
            if session.remote == remote:
                pending.append(session_id)
    for session_id in pending:
        _cleanup_session(session_id, terminate=True)


def _wait_for_authorization_url(proc: subprocess.Popen[str]) -> str:
    assert proc.stdout is not None
    start = time.monotonic()
    while True:
        if _URL_TIMEOUT and time.monotonic() - start > _URL_TIMEOUT:
            raise RuntimeError("timed out waiting for authorization URL")
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        match = re.search(r"https?://\S+", line)
        if match:
            return match.group(0)
    raise RuntimeError("authorization URL not found")


def get_authorization_session(session_id: str) -> AuthorizationSession | None:
    """Return the cached session for *session_id* if available."""

    with _AUTH_LOCK:
        return _AUTH_SESSIONS.get(session_id)


def authorize_drive(remote: str) -> tuple[str, str]:
    """Run ``rclone authorize drive`` and return ``(session_id, url)``.

    The command normally prints a line containing the URL the user must open in
    their browser. This helper captures the output, extracts the first URL and
    keeps the process alive so that the caller can later provide the
    verification code and capture the resulting token JSON.
    """
    try:
        proc = subprocess.Popen(
            [
                "rclone",
                "authorize",
                "drive",
                "--auth-no-open-browser",
                "--manual",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("rclone is not installed") from exc

    if proc.stdout is None or proc.stdin is None:
        _stop_process(proc)
        raise RuntimeError("failed to capture rclone output")

    try:
        url = _wait_for_authorization_url(proc)
    except Exception:
        _stop_process(proc)
        raise

    session_id = uuid.uuid4().hex
    _cleanup_sessions_for_remote(remote)
    with _AUTH_LOCK:
        _AUTH_SESSIONS[session_id] = AuthorizationSession(
            remote=remote, process=proc, stdout=proc.stdout, stdin=proc.stdin
        )
    return session_id, url


def _wait_for_token(session: AuthorizationSession) -> str:
    proc = session.process
    stdout = session.stdout
    start = time.monotonic()
    collecting = False
    buffer = ""
    while True:
        if _TOKEN_TIMEOUT and time.monotonic() - start > _TOKEN_TIMEOUT:
            raise RuntimeError("timed out waiting for authorization token")
        line = stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if not collecting and "{" not in stripped:
            continue
        collecting = True
        buffer += line
        candidate = buffer.strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    raise RuntimeError("failed to read authorization token from rclone")


def complete_drive_authorization(session_id: str, code: str) -> str:
    """Submit *code* to the pending session and return the token JSON."""

    with _AUTH_LOCK:
        session = _AUTH_SESSIONS.get(session_id)
    if not session:
        raise RuntimeError("authorization session not found")

    proc = session.process
    if proc.poll() is not None:
        _cleanup_session(session_id)
        raise RuntimeError("authorization session is no longer active")

    submission = code.rstrip("\n") + "\n"
    try:
        session.stdin.write(submission)
        session.stdin.flush()
    except Exception as exc:  # pragma: no cover - defensive
        _cleanup_session(session_id, terminate=True)
        raise RuntimeError("failed to submit verification code") from exc

    try:
        token = _wait_for_token(session)
    except Exception:
        _cleanup_session(session_id, terminate=True)
        raise

    _cleanup_session(session_id)
    return token


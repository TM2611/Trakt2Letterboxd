#!/usr/bin/env python3
"""Refresh Letterboxd cookies through a temporary mobile-accessible browser.

The workflow runs this script under Xvfb. It exposes the runner's Chromium
window through x11vnc/websockify/noVNC and a short-lived Cloudflare quick
tunnel. The user logs in through that remote browser; cookies are then read
from Playwright's browser context (which can read httpOnly cookies) and sent to
GitHub with ``gh secret set`` via stdin.

No cookie value is printed, passed as a command-line argument, or written to a
repository artifact.
"""

from __future__ import annotations

import os
import re
import select
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


LETTERBOXD_URL = "https://letterboxd.com"
LOGIN_URL = f"{LETTERBOXD_URL}/login/"
SYNC_WORKFLOW = "trakt-sync.yml"
DEFAULT_TIMEOUT_SECONDS = 20 * 60
POLL_SECONDS = 3
VNC_PORT = 5900
NOVNC_PORT = 6080
CLOUDFLARED_START_TIMEOUT_SECONDS = 90
TUNNEL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

COOKIE_NAMES = {
    "LETTERBOXD_SESSION_COOKIE": "letterboxd.user.CURRENT",
    "LETTERBOXD_CSRF_COOKIE": "com.xk72.webparts.csrf",
    "LETTERBOXD_CF_CLEARANCE": "cf_clearance",
}


def _integer_environment(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum}")
    return value


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required for cookie refresh")
    return value


def _append_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(markdown.rstrip() + "\n")


class RefreshBrowser:
    """Owns the temporary VNC, tunnel, and Playwright browser processes."""

    def __init__(self):
        self.processes: list[subprocess.Popen[str]] = []
        self.token_file: Path | None = None

    def _start_process(
        self,
        command: list[str],
        *,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        self.processes.append(process)
        return process

    def start_vnc(self) -> str:
        display = os.environ.get("DISPLAY", "").strip()
        if not display:
            raise RuntimeError("DISPLAY is not set; run this helper under Xvfb")
        if not shutil.which("x11vnc"):
            raise RuntimeError("x11vnc is not installed")
        if not shutil.which("websockify"):
            raise RuntimeError("websockify is not installed")

        novnc_dir = Path(os.environ.get("NOVNC_WEB_DIR", "/usr/share/novnc"))
        if not (novnc_dir / "vnc.html").is_file():
            raise RuntimeError(f"noVNC was not found at {novnc_dir}")

        token = secrets.token_urlsafe(24)
        token_handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="letterboxd-vnc-", delete=False, encoding="utf-8"
        )
        self.token_file = Path(token_handle.name)
        try:
            # websockify TokenFile maps the URL token to the local VNC target.
            token_handle.write(f"{token}:127.0.0.1:{VNC_PORT}\n")
        finally:
            token_handle.close()
        self.token_file.chmod(0o600)

        self._start_process(
            [
                "x11vnc",
                "-display",
                display,
                "-rfbport",
                str(VNC_PORT),
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-quiet",
            ]
        )
        time.sleep(1)
        self._start_process(
            [
                "websockify",
                "--web",
                str(novnc_dir),
                "--token-plugin=TokenFile",
                f"--token-source={self.token_file}",
                str(NOVNC_PORT),
            ]
        )
        time.sleep(1)
        return token

    def start_tunnel(self) -> str:
        if not shutil.which("cloudflared"):
            raise RuntimeError("cloudflared is not installed")

        tunnel = subprocess.Popen(
            [
                "cloudflared",
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{NOVNC_PORT}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.processes.append(tunnel)
        deadline = time.monotonic() + CLOUDFLARED_START_TIMEOUT_SECONDS
        output: list[str] = []
        assert tunnel.stdout is not None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([tunnel.stdout], [], [], 0.5)
            if not ready:
                if tunnel.poll() is not None:
                    break
                continue
            line = tunnel.stdout.readline()
            if line:
                output.append(line.strip())
                match = TUNNEL_PATTERN.search(line)
                if match:
                    return match.group(0)
            elif tunnel.poll() is not None:
                break
        details = " ".join(output[-3:])
        raise RuntimeError(f"cloudflared did not provide a tunnel URL. {details}")

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.token_file is not None:
            self.token_file.unlink(missing_ok=True)


def _cookies_by_name(context) -> dict[str, str]:
    cookies = context.cookies([LETTERBOXD_URL])
    return {cookie["name"]: cookie["value"] for cookie in cookies}


def _logged_in(page, context) -> bool:
    cookies = _cookies_by_name(context)
    if not cookies.get("letterboxd.user.CURRENT"):
        return False
    if not cookies.get("com.xk72.webparts.csrf"):
        return False
    try:
        return page.locator("#field-username").count() == 0 and page.locator(
            "#field-password"
        ).count() == 0
    except Exception:
        return False


def _write_secret(repo: str, name: str, value: str) -> None:
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        # Do not put secret values in process arguments or logs.
        input=value + "\n",
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh secret set failed for {name}: {result.stderr.strip()}")


def _delete_secret_if_present(repo: str, name: str) -> None:
    result = subprocess.run(
        ["gh", "secret", "delete", name, "--repo", repo, "--yes"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"gh secret delete failed for {name}")


def _update_secrets_and_dispatch(repo: str, values: dict[str, str]) -> None:
    for secret_name in (
        "LETTERBOXD_SESSION_COOKIE",
        "LETTERBOXD_CSRF_COOKIE",
    ):
        _write_secret(repo, secret_name, values[secret_name])

    if values["LETTERBOXD_CF_CLEARANCE"]:
        _write_secret(repo, "LETTERBOXD_CF_CLEARANCE", values["LETTERBOXD_CF_CLEARANCE"])
    else:
        _delete_secret_if_present(repo, "LETTERBOXD_CF_CLEARANCE")

    dispatch = subprocess.run(
        ["gh", "workflow", "run", SYNC_WORKFLOW, "--repo", repo],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
        check=False,
    )
    if dispatch.returncode != 0:
        raise RuntimeError(f"sync workflow dispatch failed: {dispatch.stderr.strip()}")


def run() -> int:
    repo = _required_environment("REPO")
    _required_environment("GH_TOKEN")
    timeout_seconds = _integer_environment(
        "REFRESH_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 60, 3600
    )
    debug_dir = Path(os.environ.get("DEBUG_DIR", "debug"))
    debug_dir.mkdir(parents=True, exist_ok=True)

    refresh_browser = RefreshBrowser()
    browser = None
    try:
        vnc_token = refresh_browser.start_vnc()
        tunnel_url = refresh_browser.start_tunnel()
        remote_url = (
            f"{tunnel_url}/vnc.html?autoconnect=true&resize=remote"
            f"&path=websockify&token={vnc_token}"
        )
        message = (
            "## Letterboxd cookie refresh\n\n"
            "Open the temporary browser on your phone, then log in to Letterboxd:\n\n"
            f"[**Open remote Letterboxd browser**]({remote_url})\n\n"
            "The session is captured automatically after login. This link expires "
            f"when this workflow ends (within {timeout_seconds // 60} minutes)."
        )
        print(message, flush=True)
        _append_summary(message)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="Europe/London",
            )
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15_000)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if _logged_in(page, context):
                    break
                page.wait_for_timeout(POLL_SECONDS * 1000)
            else:
                page.screenshot(path=str(debug_dir / "refresh_timeout.png"), full_page=True)
                raise RuntimeError("Timed out waiting for a Letterboxd login")

            cookies = _cookies_by_name(context)
            values = {
                secret_name: cookies.get(cookie_name, "")
                for secret_name, cookie_name in COOKIE_NAMES.items()
            }
            if not values["LETTERBOXD_SESSION_COOKIE"] or not values["LETTERBOXD_CSRF_COOKIE"]:
                raise RuntimeError("Letterboxd login completed without required cookies")
            _update_secrets_and_dispatch(repo, values)

        completion = (
            "## Letterboxd cookie refresh complete\n\n"
            "Secrets updated and the sync workflow was re-triggered."
        )
        print(completion, flush=True)
        _append_summary(completion)
        return 0
    except Exception as exc:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        error = f"Cookie refresh failed: {exc}"
        print(error, file=sys.stderr, flush=True)
        _append_summary(f"## Letterboxd cookie refresh failed\n\n{error}")
        return 1
    finally:
        refresh_browser.close()


if __name__ == "__main__":
    raise SystemExit(run())

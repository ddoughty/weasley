"""
Weasley authentication via Playwright persistent browser context.

First run:  interactive_login() — launches visible browser, you log in
            manually (including YubiKey if prompted), session is saved.

Subsequent runs: ensure_session() restores the saved session and verifies
            it's still valid. Falls back to interactive_login() if not.
"""

import json
import logging
import os
import uuid
from typing import Optional

from playwright.sync_api import sync_playwright

from config import Config

log = logging.getLogger("weasley.auth")

ICLOUD_URL = "https://www.icloud.com"
VALIDATE_URL = "https://setup.icloud.com/setup/ws/1/validate"


class WeasleyAuth:
    def __init__(self, config: Config):
        self.config = config
        self._cookies: Optional[list] = None   # raw Playwright cookie list
        self._fmip_base_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_session(self) -> bool:
        """
        Verify we have a usable session. If the saved session is valid,
        load it and return True. If not, attempt interactive login.
        """
        if self._load_saved_session():
            log.info("Restored saved session.")
            return True

        log.warning("No valid saved session found. Falling back to interactive login.")
        return self.interactive_login()

    def interactive_login(self) -> bool:
        """
        Launch a visible browser and wait for the user to log in to iCloud.
        Saves the session when done and validates it immediately.
        """
        log.info("Launching browser for interactive login.")
        log.info("Please log in to iCloud in the browser window that opens.")
        log.info("When you can see the iCloud home screen, press Enter here.")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self.config.session_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
            page.goto(ICLOUD_URL)

            # Wait for the user to complete login manually
            input("\n>>> Press Enter once you are logged in to iCloud... ")

            # Grab cookies and save
            self._cookies = context.cookies()
            self._save_session(self._cookies)

            # Validate immediately and persist any refreshed session cookies.
            if not self._validate_session():
                log.warning(
                    "Could not validate authenticated session after login. "
                    "Re-run `python main.py auth` if `once` still returns 450."
                )
                context.close()
                return False

            context.close()

        log.info("Interactive login complete. Session saved.")
        return True

    def get_cookies_for_requests(self) -> list[dict]:
        """Return cookies with metadata suitable for constructing a requests jar."""
        if not self._cookies:
            raise RuntimeError("No session loaded. Call ensure_session() first.")
        return self._cookies

    @property
    def fmip_base_url(self) -> str:
        if not self._fmip_base_url:
            raise RuntimeError("FMIP URL not known yet. Call ensure_session() first.")
        return self._fmip_base_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_file(self) -> str:
        return os.path.join(self.config.session_dir, "weasley_session.json")

    def _save_session(self, cookies: list):
        os.makedirs(self.config.session_dir, exist_ok=True)
        with open(self._session_file(), "w") as f:
            json.dump({"cookies": cookies}, f, indent=2)
        log.info(f"Session saved to {self._session_file()}")

    def _load_saved_session(self) -> bool:
        path = self._session_file()
        if not os.path.exists(path):
            log.info("No saved session file found.")
            return False

        with open(path) as f:
            data = json.load(f)

        self._cookies = data.get("cookies", [])

        # Quick sanity check: do we have any icloud cookies at all?
        icloud_cookies = [c for c in self._cookies if "icloud.com" in c.get("domain", "")]
        if not icloud_cookies:
            log.warning("Saved session has no iCloud cookies.")
            return False

        # Try to validate the session by hitting the validate endpoint
        return self._validate_session()

    def _validate_session(self) -> bool:
        """
        Hit the validate endpoint with saved cookies to confirm the session
        is still alive and extract current account info (dsid, fmip URL).
        """
        import requests

        jar = _cookies_to_jar(self._cookies)

        session = requests.Session()
        session.cookies = jar
        session.headers.update(_browser_headers())

        params = {
            "clientBuildNumber": self.config.client_build_number,
            "clientMasteringNumber": self.config.client_mastering_number,
            "clientId": self.config.client_id,
            "requestId": str(uuid.uuid4()),
        }
        if self.config.dsid:
            params["dsid"] = self.config.dsid

        try:
            resp = session.post(VALIDATE_URL, params=params, timeout=15)
        except Exception as e:
            log.warning(f"validate request failed: {e}")
            return False

        if resp.status_code != 200:
            log.warning(f"validate returned {resp.status_code} — session likely expired.")
            return False

        self._update_cookies_from_session(session.cookies)

        account = resp.json()
        dsid = account.get("dsInfo", {}).get("dsid", "")
        if dsid and str(dsid) != self.config.dsid:
            self.config.set_secret("dsid", str(dsid))
            log.info(f"Captured dsid: {dsid}")
        apple_id = account.get("dsInfo", {}).get("primaryEmail", "")
        if apple_id and apple_id != self.config.apple_id:
            self.config.set_secret("apple_id", apple_id)
            log.info(f"Captured apple_id: {apple_id}")
        self._extract_fmip_url(account)
        return bool(self._fmip_base_url)

    def _update_cookies_from_session(self, jar):
        refreshed = _cookiejar_to_dicts(jar)
        if not refreshed:
            return
        if _cookies_fingerprint(self._cookies) == _cookies_fingerprint(refreshed):
            return
        self._cookies = refreshed
        self._save_session(self._cookies)

    def _extract_fmip_url(self, account: dict):
        """Pull the FMIP base URL out of a validate response blob."""
        try:
            url = account["webservices"]["findme"]["url"]
            # Strip trailing port if present — requests handles it fine either way
            self._fmip_base_url = url
            log.info(f"FMIP base URL: {url}")
        except KeyError:
            log.warning("Could not find FMIP URL in validate response.")


def _browser_headers() -> dict:
    return {
        "Origin": "https://www.icloud.com",
        "Referer": "https://www.icloud.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/26.3.1 Safari/605.1.15"
        ),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def _cookies_to_jar(cookies: Optional[list[dict]]):
    from requests.cookies import RequestsCookieJar

    jar = RequestsCookieJar()
    for cookie in cookies or []:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        jar.set(
            name,
            value,
            domain=cookie.get("domain", ".icloud.com"),
            path=cookie.get("path", "/"),
        )
    return jar


def _cookiejar_to_dicts(jar) -> list[dict]:
    cookies = []
    for cookie in jar:
        cookies.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".icloud.com",
            "path": cookie.path or "/",
        })
    return cookies


def _cookies_fingerprint(cookies: Optional[list[dict]]) -> list[tuple[str, str, str, str]]:
    rows = []
    for cookie in cookies or []:
        rows.append((
            cookie.get("domain", ""),
            cookie.get("path", ""),
            cookie.get("name", ""),
            cookie.get("value", ""),
        ))
    rows.sort()
    return rows

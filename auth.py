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
import re
import time
from urllib.parse import urlparse
import uuid
from typing import Optional

from playwright.sync_api import sync_playwright

from config import Config

log = logging.getLogger("weasley.auth")

ICLOUD_URL = "https://www.icloud.com"
ICLOUD_FIND_URL = "https://www.icloud.com/find"
VALIDATE_URL = "https://setup.icloud.com/setup/ws/1/validate"


class WeasleyAuth:
    def __init__(self, config: Config):
        self.config = config
        self._cookies: Optional[list] = None  # raw Playwright cookie list
        self._fmip_base_url: Optional[str] = None
        self._fmf_base_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_session(self) -> bool:
        """
        Verify we have a usable session. If the saved session is valid,
        load it and return True. If not, attempt interactive login.
        """
        if self._load_saved_session():
            self._log_cookie_inventory("restored")
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

            # Prime Find My in the same browser context so FMIP session cookies
            # are created before we persist cookies.
            self._prime_findmy_cookie_in_context(context, interactive=True)

            # Grab cookies and save
            self._cookies = context.cookies()
            self._log_cookie_inventory("post-interactive-login")
            self._save_session(self._cookies)

            # Validate immediately and persist any refreshed session cookies.
            if not self._validate_session():
                if self._salvage_session_without_validate():
                    context.close()
                    log.info("Interactive login complete. Session saved.")
                    return True
                log.warning(
                    "Could not validate authenticated session after login. "
                    "Re-run `python main.py auth` if `once` still returns 450."
                )
                context.close()
                return False

            context.close()

        log.info("Interactive login complete. Session saved.")
        return True

    def refresh_session(self, reprime_fmip: bool = False) -> bool:
        """
        Refresh session metadata (cookies + FMIP URL) using validate.
        Useful when FMIP returns 450 but cookies still look usable.
        """
        log.info("[refresh] starting (reprime_fmip=%s)", reprime_fmip)
        if not self._cookies:
            if not self._load_cookies_from_disk():
                log.warning("[refresh] no cookies on disk — cannot refresh")
                return False
        self._log_cookie_inventory("pre-refresh")
        validated = self._validate_session()
        log.info("[refresh] validate result: %s", validated)
        if validated and (not reprime_fmip or self._has_cookie("X-APPLE-WEBAUTH-FMIP")):
            self._log_cookie_inventory("post-refresh-ok")
            return True

        if reprime_fmip:
            log.info(
                "[refresh] FMIP cookie missing or stale after validate; "
                "refreshing from iCloud Find."
            )
            if self._refresh_cookies_from_browser():
                self._log_cookie_inventory("post-browser-reprime")
                return self._validate_session()
            else:
                log.warning("[refresh] browser re-prime failed")
        self._log_cookie_inventory("post-refresh-fallback")
        return validated

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

    @property
    def fmf_base_url(self) -> Optional[str]:
        return self._fmf_base_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_file(self) -> str:
        return os.path.join(self.config.session_dir, "weasley_session.json")

    def _save_session(self, cookies: list):
        os.makedirs(self.config.session_dir, exist_ok=True)
        payload = {"cookies": cookies}
        if self._fmip_base_url:
            payload["fmip_base_url"] = self._fmip_base_url
        if self._fmf_base_url:
            payload["fmf_base_url"] = self._fmf_base_url
        with open(self._session_file(), "w") as f:
            json.dump(payload, f, indent=2)
        log.info(f"Session saved to {self._session_file()}")

    def _load_saved_session(self) -> bool:
        if not self._load_cookies_from_disk():
            return False

        # Fast-path: avoid validate on every run if we already have FMIP cookie
        # + host metadata. This reduces auth churn/challenges from Apple.
        if self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
            if self._fmip_base_url:
                log.info("Loaded session from disk (cached FMIP metadata).")
                return True
            if self._extract_fmip_url_from_cookies():
                log.info("Loaded session from disk (cookie-derived FMIP host).")
                return True

        if self._validate_session():
            return True
        if self._salvage_session_without_validate():
            log.warning("Using saved session despite validate failure.")
            return True
        return False

    def _load_cookies_from_disk(self) -> bool:
        path = self._session_file()
        if not os.path.exists(path):
            log.info("No saved session file found.")
            return False

        with open(path) as f:
            data = json.load(f)

        self._cookies = data.get("cookies", [])
        fmip_url = data.get("fmip_base_url")
        if isinstance(fmip_url, str) and fmip_url:
            self._fmip_base_url = fmip_url
        fmf_url = data.get("fmf_base_url")
        if isinstance(fmf_url, str) and fmf_url:
            self._fmf_base_url = fmf_url

        # Quick sanity check: do we have any icloud cookies at all?
        icloud_cookies = [
            c for c in self._cookies if "icloud.com" in c.get("domain", "")
        ]
        if not icloud_cookies:
            log.warning("Saved session has no iCloud cookies.")
            return False

        return True

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

        base_params = {
            "clientBuildNumber": self.config.client_build_number,
            "clientMasteringNumber": self.config.client_mastering_number,
            "requestId": str(uuid.uuid4()),
        }
        variants = []
        if self.config.client_id and self.config.dsid:
            variants.append(
                {
                    **base_params,
                    "clientId": self.config.client_id,
                    "dsid": self.config.dsid,
                }
            )
        if self.config.client_id:
            variants.append({**base_params, "clientId": self.config.client_id})
        variants.append(base_params)

        resp = None
        for idx, params in enumerate(variants, start=1):
            try:
                resp = session.post(VALIDATE_URL, params=params, timeout=15)
            except Exception as e:
                log.warning(f"validate request failed (attempt {idx}): {e}")
                continue

            if resp.status_code == 200:
                break

            body = (resp.text or "").replace("\n", " ")
            if len(body) > 200:
                body = body[:200] + "..."
            log.warning(
                "validate attempt %s returned %s (params: %s). Body: %s",
                idx,
                resp.status_code,
                sorted(params.keys()),
                body or "<empty>",
            )

        if not resp or resp.status_code != 200:
            # In some accounts, validate can intermittently return 421 even when
            # FMIP cookies/host are valid. Fall back to FMIP host from cookies.
            if (
                resp
                and resp.status_code == 421
                and self._extract_fmip_url_from_cookies()
            ):
                log.warning(
                    "validate returned 421, falling back to FMIP host from cookies."
                )
                return True
            log.warning(
                "validate did not return 200 — Apple rejected trust validation "
                "(session may still be valid)."
            )
            return False

        self._update_cookies_from_session(session.cookies)
        log.info(
            "Validate succeeded; FMIP cookie present: %s",
            "yes" if self._has_cookie("X-APPLE-WEBAUTH-FMIP") else "no",
        )

        account = resp.json()
        dsid = account.get("dsInfo", {}).get("dsid", "")
        if dsid and str(dsid) != self.config.dsid:
            self.config.set_secret("dsid", str(dsid))
            log.info(f"Captured dsid: {dsid}")
        apple_id = account.get("dsInfo", {}).get("primaryEmail", "")
        if apple_id and apple_id != self.config.apple_id:
            self.config.set_secret("apple_id", apple_id)
            log.info(f"Captured apple_id: {apple_id}")
        self._extract_service_urls(account)
        return bool(self._fmip_base_url or self._fmf_base_url)

    def _update_cookies_from_session(self, jar):
        refreshed = _cookiejar_to_dicts(jar)
        if not refreshed:
            return
        if _cookies_fingerprint(self._cookies) == _cookies_fingerprint(refreshed):
            return
        self._cookies = refreshed
        self._save_session(self._cookies)

    def _extract_service_urls(self, account: dict):
        """Pull FMIP/FMF base URLs out of a validate response blob."""
        webservices = account.get("webservices", {})
        if not isinstance(webservices, dict):
            log.warning("Could not find webservices in validate response.")
            return

        fmip_url = _extract_url_from_webservices(webservices, "findme")
        if fmip_url:
            self._fmip_base_url = fmip_url
            log.info(f"FMIP base URL: {fmip_url}")
        else:
            log.warning("Could not find FMIP URL in validate response.")

        fmf_url = _extract_fmf_url_from_webservices(webservices)
        if fmf_url:
            self._fmf_base_url = fmf_url
            log.info(f"FMF base URL: {fmf_url}")

    def _has_cookie(self, name: str) -> bool:
        return any(cookie.get("name") == name for cookie in (self._cookies or []))

    def _log_cookie_inventory(self, label: str = "current"):
        """Log which auth-critical cookies are present and session file age."""
        key_cookies = [
            "X-APPLE-WEBAUTH-FMIP",
            "X-APPLE-DS-WEB-SESSION-TOKEN",
            "X-APPLE-WEBAUTH-LOGIN",
            "X-APPLE-WEBAUTH-USER",
            "X-APPLE-WEBAUTH-HSA-TRUST",
        ]
        present = {name for name in key_cookies if self._has_cookie(name)}
        missing = sorted(set(key_cookies) - present)

        session_age_str = "unknown"
        path = self._session_file()
        if os.path.exists(path):
            age_secs = time.time() - os.path.getmtime(path)
            if age_secs < 120:
                session_age_str = f"{age_secs:.0f}s"
            elif age_secs < 7200:
                session_age_str = f"{age_secs / 60:.0f}m"
            else:
                session_age_str = f"{age_secs / 3600:.1f}h"

        total_icloud = sum(
            1 for c in (self._cookies or []) if "icloud.com" in c.get("domain", "")
        )
        log.info(
            "[session:%s] cookies=%d icloud=%d | present: %s | missing: %s | "
            "session_file_age=%s | fmip_url=%s | fmf_url=%s",
            label,
            len(self._cookies or []),
            total_icloud,
            sorted(present) if present else "none",
            missing if missing else "none",
            session_age_str,
            self._fmip_base_url or "unset",
            self._fmf_base_url or "unset",
        )

    def _refresh_cookies_from_browser(self, max_attempts: int = 3) -> bool:
        """
        Reopen the persisted browser profile and load iCloud Find to mint
        FMIP auth cookies without forcing a full re-auth.

        Retries up to *max_attempts* times if the FMIP cookie is not minted.
        """
        headless = not os.environ.get("WEASLEY_DEBUG_BROWSER")
        for attempt in range(1, max_attempts + 1):
            log.info(
                "[refresh-browser] attempt %d/%d (headless=%s)",
                attempt,
                max_attempts,
                headless,
            )
            try:
                with sync_playwright() as p:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=self.config.session_dir,
                        headless=headless,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    self._prime_findmy_cookie_in_context(context, interactive=False)
                    self._cookies = context.cookies()
                    self._extract_fmip_url_from_cookies()
                    self._save_session(self._cookies)
                    context.close()
            except Exception as e:
                log.warning("[refresh-browser] attempt %d failed: %s", attempt, e)
                continue

            if self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
                log.info(
                    "[refresh-browser] FMIP cookie obtained on attempt %d", attempt
                )
                return True
            log.warning(
                "[refresh-browser] attempt %d completed but FMIP cookie still absent",
                attempt,
            )
        log.warning("[refresh-browser] all %d attempts exhausted", max_attempts)
        return False

    def _prime_findmy_cookie_in_context(self, context, interactive: bool):
        # Snapshot cookies before priming to detect changes
        cookies_before = context.cookies()
        fmip_before = any(
            c.get("name") == "X-APPLE-WEBAUTH-FMIP" for c in cookies_before
        )
        log.info(
            "[prime] starting (interactive=%s) — FMIP cookie before: %s, total cookies: %d",
            interactive,
            fmip_before,
            len(cookies_before),
        )

        page = context.new_page()
        try:
            log.info("[prime] navigating to iCloud Find...")
            page.goto(ICLOUD_FIND_URL, wait_until="domcontentloaded", timeout=60000)
            log.info("[prime] page loaded — url: %s", page.url)
            self._extract_fmip_url_from_page_url(page.url)
            if interactive:
                log.info("If prompted, re-enter your Apple password in the Find page.")
                input(
                    ">>> Press Enter once iCloud Find is fully loaded (map/devices visible)... "
                )
            else:
                # Poll for FMIP cookie instead of a fixed wait — Apple's JS
                # may take varying time to mint the cookie.
                poll_interval_ms = 1000
                max_wait_ms = 15000
                elapsed = 0
                while elapsed < max_wait_ms:
                    page.wait_for_timeout(poll_interval_ms)
                    elapsed += poll_interval_ms
                    if any(
                        c.get("name") == "X-APPLE-WEBAUTH-FMIP"
                        for c in context.cookies()
                    ):
                        log.info("[prime] FMIP cookie appeared after %dms", elapsed)
                        break
                else:
                    log.warning(
                        "[prime] FMIP cookie did not appear within %dms",
                        max_wait_ms,
                    )
            self._extract_fmip_url_from_page_url(page.url)
            self._extract_fmip_url_from_page_resources(page)
            current_url = page.url.lower()
            if "signin" in current_url or "appleauth" in current_url:
                log.warning(
                    "[prime] landed on sign-in page (%s) — browser profile "
                    "is not authenticated for Find.",
                    page.url,
                )
        except Exception as e:
            log.warning(f"[prime] could not open iCloud Find in browser context: {e}")
        finally:
            page.close()

        # Report what changed
        cookies_after = context.cookies()
        fmip_after = any(c.get("name") == "X-APPLE-WEBAUTH-FMIP" for c in cookies_after)
        new_count = len(cookies_after) - len(cookies_before)
        log.info(
            "[prime] finished — FMIP cookie after: %s (was %s), "
            "cookies: %d (delta %+d)",
            fmip_after,
            fmip_before,
            len(cookies_after),
            new_count,
        )

    def _extract_fmip_url_from_cookies(self) -> bool:
        domains = []
        for cookie in self._cookies or []:
            domain = cookie.get("domain", "")
            if isinstance(domain, str):
                domains.append(domain.lstrip("."))

        for domain in sorted(set(domains)):
            if re.search(r"^p\d+-fmipweb\.icloud\.com$", domain):
                self._fmip_base_url = f"https://{domain}:443"
                log.info(f"FMIP base URL (cookie-derived): {self._fmip_base_url}")
                return True
        return False

    def _extract_fmip_url_from_page_url(self, page_url: str) -> bool:
        if not page_url:
            return False
        parsed = urlparse(page_url)
        host = (parsed.hostname or "").lower()
        if re.search(r"^p\d+-fmipweb\.icloud\.com$", host):
            self._fmip_base_url = f"https://{host}:443"
            log.info(f"FMIP base URL (page-derived): {self._fmip_base_url}")
            return True
        return False

    def _extract_fmip_url_from_page_resources(self, page) -> bool:
        try:
            resource_url = page.evaluate("""
                () => {
                    const items = performance.getEntriesByType('resource').map(e => e.name || '');
                    const hit = items.find((u) => /https:\\/\\/p\\d+-fmipweb\\.icloud\\.com/i.test(u));
                    return hit || null;
                }
                """)
        except Exception:
            return False

        if not isinstance(resource_url, str) or not resource_url:
            return False
        return self._extract_fmip_url_from_page_url(resource_url)

    def _salvage_session_without_validate(self) -> bool:
        """
        Allow progress when validate is blocked (e.g., 421 trust-token flow) but
        Find/FMIP cookies are present and we can determine an FMIP host.
        """
        if not self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
            return False
        if self._fmip_base_url:
            self._save_session(self._cookies or [])
            log.warning(
                "Validate failed; proceeding with existing FMIP session metadata."
            )
            return True
        if self._extract_fmip_url_from_cookies():
            self._save_session(self._cookies or [])
            log.warning("Validate failed; proceeding with cookie-derived FMIP host.")
            return True
        return False


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


def _extract_url_from_webservices(webservices: dict, key: str) -> Optional[str]:
    service = webservices.get(key, {})
    if isinstance(service, dict):
        url = service.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _extract_fmf_url_from_webservices(webservices: dict) -> Optional[str]:
    for key in ("fmf", "findfriends", "findmyfriends"):
        url = _extract_url_from_webservices(webservices, key)
        if url:
            return url

    for key, service in webservices.items():
        if not isinstance(service, dict):
            continue
        url = service.get("url")
        if not isinstance(url, str):
            continue
        key_norm = key.lower()
        if "friend" in key_norm or "fmf" in key_norm or "/fmf" in url:
            return url
    return None


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
        domain = cookie.get("domain", "")
        if (
            isinstance(domain, str)
            and "icloud.com" in domain
            and domain != ".icloud.com"
        ):
            # FMIP endpoints are subdomains of icloud.com; keep a superdomain
            # variant so auth cookies are available across setup/fmip hosts.
            jar.set(
                name,
                value,
                domain=".icloud.com",
                path=cookie.get("path", "/"),
            )
    return jar


def _cookiejar_to_dicts(jar) -> list[dict]:
    cookies = []
    for cookie in jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or ".icloud.com",
                "path": cookie.path or "/",
            }
        )
    return cookies


def _cookies_fingerprint(
    cookies: Optional[list[dict]],
) -> list[tuple[str, str, str, str]]:
    rows = []
    for cookie in cookies or []:
        rows.append(
            (
                cookie.get("domain", ""),
                cookie.get("path", ""),
                cookie.get("name", ""),
                cookie.get("value", ""),
            )
        )
    rows.sort()
    return rows

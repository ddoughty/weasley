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


FMIP_COOKIE_MAX_AGE_SECS = 25 * 60  # 25 minutes — re-prime before likely expiry


class WeasleyAuth:
    def __init__(self, config: Config):
        self.config = config
        self._cookies: Optional[list] = None  # raw Playwright cookie list
        self._fmip_base_url: Optional[str] = None
        self._fmf_base_url: Optional[str] = None
        self._fmip_cookie_ts: Optional[float] = None  # epoch when FMIP cookie last set

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
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-site-isolation-trials",
                ],
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
        Multi-tier session refresh.  Each tier escalates only when the
        previous tier fails:

          Tier 1 — validate endpoint (re-confirm session, refresh cookies)
          Tier 2 — headless browser re-prime of FMIP cookie
          Tier 3 — automated re-login with stored credentials
          Tier 4 — give up; caller should fall back to interactive auth
        """
        log.info("[refresh] starting (reprime_fmip=%s)", reprime_fmip)
        if not self._cookies:
            if not self._load_cookies_from_disk():
                log.warning("[refresh] no cookies on disk — cannot refresh")
                return False
        self._log_cookie_inventory("pre-refresh")

        # --- Tier 1: validate ---
        validated = self._validate_session()
        log.info("[refresh] tier-1 validate result: %s", validated)
        if validated and (not reprime_fmip or self._has_cookie("X-APPLE-WEBAUTH-FMIP")):
            self._log_cookie_inventory("post-tier1-ok")
            return True

        # --- Tier 2: headless browser re-prime ---
        if reprime_fmip:
            log.info(
                "[refresh] tier-2: FMIP cookie missing or stale after validate; "
                "re-priming from iCloud Find."
            )
            if self._refresh_cookies_from_browser():
                self._log_cookie_inventory("post-tier2-reprime")
                revalidated = self._validate_session()
                if revalidated:
                    return True
                # validate may fail but FMIP cookie is fresh — allow it
                if self._has_cookie("X-APPLE-WEBAUTH-FMIP") and self._fmip_base_url:
                    log.warning(
                        "[refresh] tier-2: validate failed after re-prime "
                        "but FMIP cookie + URL present — proceeding"
                    )
                    return True
            else:
                log.warning("[refresh] tier-2: browser re-prime failed")

        # --- Tier 3: automated re-login with stored credentials ---
        if self._attempt_automated_login():
            self._log_cookie_inventory("post-tier3-login")
            return True

        # --- Tier 4: give up ---
        self._log_cookie_inventory("post-refresh-exhausted")
        log.warning("[refresh] all tiers exhausted")
        return validated

    def ensure_fresh_fmip(self) -> bool:
        """
        Proactively re-prime the FMIP cookie if it is older than the
        configured max age, avoiding 450 errors on the next API call.
        Returns True if the FMIP cookie is (now) present.
        """
        if not self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
            log.info("[fresh-fmip] no FMIP cookie — attempting re-prime")
            return self._refresh_cookies_from_browser()

        age = self._fmip_cookie_age_secs()
        if age is not None and age > FMIP_COOKIE_MAX_AGE_SECS:
            log.info(
                "[fresh-fmip] FMIP cookie is %.0fs old (max %ds) — re-priming",
                age,
                FMIP_COOKIE_MAX_AGE_SECS,
            )
            return self._refresh_cookies_from_browser()

        if age is not None:
            log.info("[fresh-fmip] FMIP cookie age %.0fs — still fresh", age)
        else:
            log.info("[fresh-fmip] FMIP cookie present (age unknown)")
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
        # Record when FMIP cookie was last obtained
        if self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
            if self._fmip_cookie_ts is None:
                self._fmip_cookie_ts = time.time()
        payload = {"cookies": cookies}
        if self._fmip_base_url:
            payload["fmip_base_url"] = self._fmip_base_url
        if self._fmf_base_url:
            payload["fmf_base_url"] = self._fmf_base_url
        if self._fmip_cookie_ts:
            payload["fmip_cookie_ts"] = self._fmip_cookie_ts
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
        fmip_ts = data.get("fmip_cookie_ts")
        if isinstance(fmip_ts, (int, float)) and fmip_ts > 0:
            self._fmip_cookie_ts = float(fmip_ts)

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

    def _fmip_cookie_age_secs(self) -> Optional[float]:
        """Seconds since FMIP cookie was last saved, or None if unknown."""
        if self._fmip_cookie_ts is None:
            return None
        return time.time() - self._fmip_cookie_ts

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

    def _attempt_automated_login(self) -> bool:
        """
        Tier 3: programmatically sign in to iCloud using stored credentials,
        then prime FMIP cookies.  Requires credentials in macOS Keychain
        (set up via ``python main.py store-credentials``).

        Returns True if a usable session was established.
        """
        from credentials import get_credentials, has_credentials

        if not has_credentials():
            log.info("[tier-3] no stored credentials — skipping automated login")
            return False

        creds = get_credentials()
        if creds is None:
            return False
        email, password = creds

        log.info("[tier-3] attempting automated re-login for %s", email)
        headless = not os.environ.get("WEASLEY_DEBUG_BROWSER")
        success = False
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.config.session_dir,
                    headless=headless,
                    args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-site-isolation-trials",
                ],
                )
                page = context.new_page()
                page.goto(ICLOUD_URL, wait_until="domcontentloaded", timeout=60000)

                # Detect whether we're on a sign-in page or already logged in.
                # Check both URL and DOM — iCloud may embed sign-in in an
                # iframe without changing the URL.
                page.wait_for_timeout(3000)
                current_url = page.url.lower()

                if (
                    "signin" in current_url
                    or "appleauth" in current_url
                    or self._page_has_sign_in_form(page)
                ):
                    log.info("[tier-3] sign-in page detected — filling credentials")
                    success = self._fill_sign_in_form(page, email, password)
                else:
                    # Already logged in — just need to re-prime FMIP
                    log.info("[tier-3] already authenticated — re-priming FMIP cookies")
                    success = True

                if success:
                    self._prime_findmy_cookie_in_context(context, interactive=False)
                    self._cookies = context.cookies()
                    self._fmip_cookie_ts = time.time()
                    self._extract_fmip_url_from_cookies()
                    self._save_session(self._cookies)

                    if self._has_cookie("X-APPLE-WEBAUTH-FMIP"):
                        log.info("[tier-3] automated login succeeded")
                    else:
                        log.warning(
                            "[tier-3] login completed but FMIP cookie not obtained"
                        )
                        success = False

                context.close()
        except Exception as e:
            log.warning("[tier-3] automated login failed: %s", e)
            success = False
        finally:
            # Ensure password is cleared from local scope
            del password
            del creds

        return success

    def _page_has_sign_in_form(self, page) -> bool:
        """
        Detect whether the current page contains a sign-in form, either
        directly in the page or embedded in the Apple auth iframe.
        Works even when the URL doesn't change (e.g. iCloud Find embeds
        the sign-in in an iframe at the same /find URL).
        """
        try:
            # Primary check: the auth widget iframe or its container exist
            # in the outer DOM.  The iframe itself is cross-origin
            # (idmsa.apple.com) so we cannot inspect its inner DOM, but
            # its mere presence on the page means Apple is asking for
            # credentials.
            auth_iframe = page.locator(
                'iframe#aid-auth-widget-iFrame, iframe[name="aid-auth-widget"]'
            )
            if auth_iframe.count() > 0:
                return True

            # Secondary: outer-page markers Apple wraps around the iframe
            auth_widget = page.locator(
                "div.auth-widget, div.sign-in-label, "
                "div.auth-widget-container"
            )
            if auth_widget.count() > 0:
                return True

            # Tertiary: fully logged-out landing page with a standalone
            # "Sign In" CTA (may be a <button>, <a>, or other element)
            sign_in_cta = page.locator(
                'button:has-text("Sign In"), a:has-text("Sign In"), '
                '[role="button"]:has-text("Sign In")'
            )
            if sign_in_cta.count() > 0:
                return True

            # Quaternary: check for inline email/password inputs (no iframe)
            email_input = page.locator(
                'input[type="email"], input[name="appleId"], '
                "input#account_name_text_field"
            )
            password_input = page.locator(
                'input[type="password"], input[name="password"]'
            )
            if email_input.count() > 0 or password_input.count() > 0:
                return True
        except Exception:
            pass
        return False

    def _fill_sign_in_form(self, page, email: str, password: str) -> bool:
        """
        Fill in the iCloud sign-in form.  Handles the common two-step flow
        (email → password) and single-page forms.

        Returns True if we appear to have reached the iCloud home/app screen.
        """
        try:
            # iCloud sign-in may use an iframe; try multiple ways to find it.
            # The iframe name/ID can vary between flow states.
            frame = page.frame(name="aid-auth-widget")
            if frame is None:
                # Try finding the iframe element and getting its frame
                iframe_el = page.locator(
                    'iframe#aid-auth-widget-iFrame, '
                    'iframe[name="aid-auth-widget"], '
                    'iframe[src*="idmsa.apple.com"]'
                )
                if iframe_el.count() > 0:
                    iframe_name = iframe_el.first.get_attribute("name")
                    iframe_id = iframe_el.first.get_attribute("id")
                    log.info(
                        "[tier-3] found iframe element: name=%s id=%s",
                        iframe_name, iframe_id,
                    )
                    if iframe_name:
                        frame = page.frame(name=iframe_name)
                    if frame is None and iframe_id:
                        frame = page.frame(name=iframe_id)
                    if frame is None:
                        # Try by URL pattern
                        for f in page.frames:
                            if "idmsa.apple.com" in (f.url or ""):
                                frame = f
                                break
            # The iframe DOM element may appear before its content has loaded.
            # Poll until a frame with the idmsa.apple.com URL appears.
            if frame is None:
                max_frame_wait = 15000
                elapsed = 0
                poll_ms = 500
                while elapsed < max_frame_wait:
                    for f in page.frames:
                        if "idmsa.apple.com" in (f.url or ""):
                            frame = f
                            log.info(
                                "[tier-3] found auth frame after %dms: %s",
                                elapsed, f.url,
                            )
                            break
                    if frame is not None:
                        break
                    page.wait_for_timeout(poll_ms)
                    elapsed += poll_ms

            if frame is None:
                log.warning("[tier-3] auth frame never loaded")
                log.info("[tier-3] page has %d frame(s):", len(page.frames))
                for i, f in enumerate(page.frames):
                    log.info("[tier-3]   frame[%d]: name=%r url=%s", i, f.name, f.url)
                frame = page

            # Wait for the iframe content to actually render its inputs
            # (the iframe is cross-origin so its DOM loads asynchronously)
            email_sel = (
                'input[type="email"], input[name="appleId"], '
                "input#account_name_text_field, "
                'input[autocomplete="username"]'
            )
            password_sel = (
                'input[type="password"], input[name="password"], '
                "input#password_text_field"
            )
            log.info(
                "[tier-3] using frame: %s (url: %s)",
                "iframe" if frame != page else "page",
                frame.url if hasattr(frame, "url") else "n/a",
            )

            try:
                frame.wait_for_selector(email_sel, timeout=20000)
            except Exception:
                # Email field may not appear if Apple already knows the account
                log.info("[tier-3] email field did not appear, checking for password field")

            # Some sign-in pages show email first, then password.
            # Apple may pre-fill and mark the email field readonly when
            # it already knows the account — skip filling in that case.
            email_input = frame.locator(email_sel)
            if email_input.count() > 0:
                is_readonly = email_input.first.get_attribute("readonly") is not None
                if is_readonly:
                    log.info(
                        "[tier-3] email field is readonly (pre-filled) — skipping to password"
                    )
                else:
                    email_input.first.fill(email)
                    # Look for a "Continue" / "Sign In" / arrow button
                    submit_btn = frame.locator(
                        'button[type="submit"], #sign-in, .si-button'
                    )
                    if submit_btn.count() > 0:
                        submit_btn.first.click()
                        page.wait_for_timeout(3000)

            # Apple's sign-in iframe has overlapping elements that intercept
            # pointer events, so we must use force=True for all interactions.

            # Now fill password — wait for it to appear after email submission
            try:
                frame.wait_for_selector(password_sel, timeout=20000)
            except Exception:
                log.warning("[tier-3] password field did not appear within 10s")
                return False

            password_input = frame.locator(password_sel)
            if password_input.count() > 0:
                # Click the password field to focus it, type the password
                # character by character (fires proper JS input events).
                password_input.first.click(force=True)
                page.wait_for_timeout(500)
                password_input.first.press_sequentially(password, delay=50)
                log.info("[tier-3] password typed")
                # Apple's JS moves focus to the passkey button after typing.
                # Press Enter (which goes nowhere), wait, re-focus the
                # password field, then press Enter again to actually submit.
                # If the first Enter succeeds and the iframe detaches, the
                # second attempt will throw — treat that as success.
                password_input.first.press("Enter")
                page.wait_for_timeout(1000)
                try:
                    password_input.first.click(force=True)
                    page.wait_for_timeout(500)
                    password_input.first.press("Enter")
                    log.info("[tier-3] submitted password form")
                except Exception as e:
                    if "detached" in str(e).lower():
                        log.info("[tier-3] auth iframe detached — sign-in succeeded")
                        return True
                    raise
            else:
                log.warning("[tier-3] could not find password field")
                return False

            # Wait for the auth widget iframe to disappear (sign-in complete)
            # or for an error/2FA state.  The page URL stays at /find/ the
            # whole time since sign-in happens inside a cross-origin iframe.
            auth_iframe_sel = 'iframe#aid-auth-widget-iFrame, iframe[name="aid-auth-widget"]'
            try:
                page.wait_for_selector(
                    auth_iframe_sel, state="detached", timeout=15000
                )
                log.info("[tier-3] auth iframe disappeared — sign-in succeeded")
                return True
            except Exception:
                pass

            # Iframe still present — check if it's a 2FA challenge or failure
            if self._page_has_sign_in_form(page):
                log.warning(
                    "[tier-3] sign-in form still present after submitting — "
                    "credentials may be wrong or 2FA required"
                )
                return False

            log.info("[tier-3] sign-in form submitted, now at: %s", page.url)
            return True

        except Exception as e:
            log.warning("[tier-3] error filling sign-in form: %s", e)
            return False

    def _fill_credentials_on_find_page(self, page, context) -> bool:
        """
        When the Find page redirects to a sign-in form, attempt to fill
        credentials from Keychain and re-authenticate.  This handles the
        case where the main iCloud session is valid but the Find service
        specifically requires re-login.

        Returns True if credentials were filled and FMIP cookie obtained.
        """
        from credentials import get_credentials, has_credentials

        if not has_credentials():
            log.info(
                "[prime-login] no stored credentials — cannot auto-login on Find page"
            )
            return False

        creds = get_credentials()
        if creds is None:
            return False
        email, password = creds

        log.info("[prime-login] attempting credential fill on Find sign-in page")
        try:
            # Fully logged-out landing page: no iframe yet, just a "Sign In"
            # button.  Click it to trigger the auth widget / redirect.
            auth_iframe = page.locator(
                'iframe#aid-auth-widget-iFrame, iframe[name="aid-auth-widget"]'
            )
            if auth_iframe.count() == 0:
                landing_btn = page.locator(
                    'button:has-text("Sign In"), a:has-text("Sign In"), '
                    '[role="button"]:has-text("Sign In")'
                )
                if landing_btn.count() > 0:
                    log.info("[prime-login] clicking landing-page Sign In button")
                    landing_btn.first.click()
                    # Wait for the auth iframe or a redirect to appear
                    try:
                        page.wait_for_selector(
                            'iframe#aid-auth-widget-iFrame, '
                            'iframe[name="aid-auth-widget"]',
                            timeout=10000,
                        )
                        log.info("[prime-login] auth iframe appeared after clicking Sign In")
                    except Exception:
                        log.warning("[prime-login] auth iframe did not appear after clicking Sign In")
                        # May have redirected to idmsa.apple.com — continue anyway

            success = self._fill_sign_in_form(page, email, password)
            if not success:
                log.warning("[prime-login] credential fill failed")
                return False

            # After successful sign-in, poll for FMIP cookie
            poll_interval_ms = 1000
            max_wait_ms = 15000
            elapsed = 0
            while elapsed < max_wait_ms:
                page.wait_for_timeout(poll_interval_ms)
                elapsed += poll_interval_ms
                if any(
                    c.get("name") == "X-APPLE-WEBAUTH-FMIP" for c in context.cookies()
                ):
                    log.info(
                        "[prime-login] FMIP cookie appeared after sign-in (%dms)",
                        elapsed,
                    )
                    return True
            log.warning(
                "[prime-login] signed in but FMIP cookie did not appear within %dms",
                max_wait_ms,
            )
            # Still return True if we navigated away from sign-in — cookies
            # may have been set even if FMIP specifically wasn't detected
            final_url = page.url.lower()
            return "signin" not in final_url and "appleauth" not in final_url
        except Exception as e:
            log.warning("[prime-login] error during credential fill: %s", e)
            return False
        finally:
            del password
            del creds

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
                        args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-site-isolation-trials",
                ],
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
                self._fmip_cookie_ts = time.time()
                self._save_session(self._cookies)
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

            # Wait briefly for JS to render, then check for sign-in form
            page.wait_for_timeout(2000)

            # Detect sign-in by checking the DOM, not just the URL.
            # iCloud Find often embeds sign-in in an iframe without
            # changing the URL from /find.
            current_url = page.url.lower()
            sign_in_detected = (
                "signin" in current_url
                or "appleauth" in current_url
                or self._page_has_sign_in_form(page)
            )

            if sign_in_detected:
                log.warning(
                    "[prime] sign-in form detected on Find page (%s)",
                    page.url,
                )
                if not interactive:
                    if self._fill_credentials_on_find_page(page, context):
                        self._extract_fmip_url_from_page_url(page.url)
                        self._extract_fmip_url_from_page_resources(page)
                else:
                    log.info(
                        "If prompted, re-enter your Apple password in the Find page."
                    )
                    input(
                        ">>> Press Enter once iCloud Find is fully loaded (map/devices visible)... "
                    )
            elif interactive:
                log.info("If prompted, re-enter your Apple password in the Find page.")
                input(
                    ">>> Press Enter once iCloud Find is fully loaded (map/devices visible)... "
                )
            else:
                # No sign-in form — poll for FMIP cookie instead of a fixed
                # wait; Apple's JS may take varying time to mint the cookie.
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

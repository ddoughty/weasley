"""
Secure credential storage for Weasley via macOS Keychain.

This is the ONLY module that should access iCloud credentials.
No other module should import keyring or handle raw passwords.
"""

import getpass
import logging
from typing import Optional

import keyring

log = logging.getLogger("weasley.credentials")

SERVICE_NAME = "weasley-icloud"
_USERNAME_KEY = "icloud_email"
_PASSWORD_KEY = "icloud_password"


def store_credentials() -> bool:
    """
    Interactively prompt for iCloud credentials and store them in the
    macOS Keychain.  Returns True on success.
    """
    email = input("iCloud email: ").strip()
    if not email:
        log.error("Email cannot be empty.")
        return False

    password = getpass.getpass("iCloud password: ")
    if not password:
        log.error("Password cannot be empty.")
        return False

    keyring.set_password(SERVICE_NAME, _USERNAME_KEY, email)
    keyring.set_password(SERVICE_NAME, _PASSWORD_KEY, password)
    # Clear from local scope immediately
    del password

    log.info("Credentials stored in macOS Keychain under service %r.", SERVICE_NAME)
    return True


def get_credentials() -> Optional[tuple[str, str]]:
    """
    Retrieve iCloud credentials from the macOS Keychain.
    Returns (email, password) or None if not stored.

    The caller MUST delete the returned password from its scope as soon
    as it is no longer needed.
    """
    email = keyring.get_password(SERVICE_NAME, _USERNAME_KEY)
    password = keyring.get_password(SERVICE_NAME, _PASSWORD_KEY)
    if not email or not password:
        return None
    return email, password


def has_credentials() -> bool:
    """Check whether credentials are stored without retrieving the password."""
    email = keyring.get_password(SERVICE_NAME, _USERNAME_KEY)
    return email is not None


def delete_credentials() -> None:
    """Remove stored credentials from the Keychain."""
    for key in (_USERNAME_KEY, _PASSWORD_KEY):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass
    log.info("Credentials removed from macOS Keychain.")

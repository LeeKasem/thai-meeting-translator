"""Windows microphone privacy switches.

When "Microphone access" or "Let desktop apps access your microphone" is off,
WASAPI still opens the mic but delivers silence, so the app would look like
it is listening while hearing nothing. Checking the consent store first lets
the UI explain what to change instead.
"""
import os
import sys

_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
SETTINGS_URI = "ms-settings:privacy-microphone"


def _value(hive, path) -> str:
    import winreg
    try:
        with winreg.OpenKey(hive, path) as k:
            return str(winreg.QueryValueEx(k, "Value")[0])
    except OSError:
        return ""


def mic_blocked() -> bool:
    """True if Windows privacy settings stop this (desktop) app using the mic."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        if _value(hive, _KEY).lower() == "deny":
            return True
    return _value(winreg.HKEY_CURRENT_USER, _KEY + r"\NonPackaged").lower() == "deny"


def open_settings() -> None:
    try:
        os.startfile(SETTINGS_URI)  # noqa: S606 - fixed Windows settings URI
    except OSError:
        pass

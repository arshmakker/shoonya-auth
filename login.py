"""
Shoonya OAuth login — standalone.

Reads OAuth config from ~/.shoonya/cred.yml, performs login, writes fresh
Access_token back to the same file.

Run once pre-market:
    python /Users/arshdeep/git/shoonya-auth/login.py

Also imported by broker_proxy.py for auto-login on stale token.
"""

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import time as _time
import urllib.parse

import yaml

# api_helper.py and shoonya_selenium_auth.py live in the same directory (symlinks).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_helper import ShoonyaApiPy
import shoonya_selenium_auth  # symlink to regimetrader/trading_system/auth/shoonya_selenium_auth.py

SHARED_CRED = os.path.expanduser("~/.shoonya/cred.yml")
DEFAULT_AUTH_CODE_SCRIPT = "/Users/arshdeep/git/Shoonya_oAuth_API.py/tests/getAuthCode.py"


def _load_creds(path=SHARED_CRED):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_creds(creds, path=SHARED_CRED):
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(creds, f, sort_keys=False)
    os.chmod(path, 0o600)


def _mask_secret(value):
    s = str(value or "")
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _is_oauth_configured(creds):
    required = ("oauth_url", "client_id", "Secret_Code", "UID")
    return all(str(creds.get(k, "")).strip() for k in required)


def _extract_auth_code(text):
    raw = str(text or "")
    if not raw:
        return ""
    m = re.search(r"Auth\s*Code\s*:\s*([A-Za-z0-9._-]+)", raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"[?&]code=([^&\s]+)", raw)
    if m:
        return urllib.parse.unquote(m.group(1).strip())
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and re.fullmatch(r"[A-Za-z0-9._-]{8,}", lines[0]):
        return lines[0]
    return ""


def _resolve_auth_code_cmd(creds):
    cmd = os.environ.get("SHOONYA_AUTH_CODE_CMD", "").strip() or str(creds.get("auth_code_cmd", "")).strip()
    if cmd:
        return cmd
    if os.path.exists(DEFAULT_AUTH_CODE_SCRIPT):
        return f'python3 "{DEFAULT_AUTH_CODE_SCRIPT}"'
    return ""


def _fetch_auth_code_from_command(creds, log):
    cmd = _resolve_auth_code_cmd(creds)
    if not cmd:
        return ""
    timeout_raw = (
        os.environ.get("SHOONYA_AUTH_CODE_TIMEOUT", "").strip()
        or str(creds.get("auth_code_timeout", "180")).strip()
    )
    try:
        timeout = max(30, int(timeout_raw))
    except ValueError:
        timeout = 180

    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        log.warning("Auth code command unparseable (%s): %s", exc, cmd)
        return ""
    if not argv:
        return ""

    shell_tokens = {"&&", "||", ";", "|", "&", ">", "<", ">>", "<<", ">&", "<&"}
    leaked = [a for a in argv if a in shell_tokens]
    if leaked:
        log.warning("Auth code command contains shell control token(s) %s; use a single binary invocation.", leaked)
        return ""

    log.info("Attempting auth code via command: %s", cmd)
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        log.warning("Auth code command failed to execute: %s", exc)
        return ""

    merged_lines = []
    code = ""
    start_ts = _time.time()
    try:
        while True:
            if process.stdout is None:
                break
            line = process.stdout.readline()
            if line:
                merged_lines.append(line)
                line_for_log = line.strip()
                if "Auth Code" in line_for_log:
                    line_for_log = "Auth code captured by external script."
                if line_for_log:
                    log.info("auth_code_cmd: %s", line_for_log)
                code = _extract_auth_code(line)
                if code:
                    process.terminate()
                    break
            elif process.poll() is not None:
                break
            if (_time.time() - start_ts) > timeout:
                process.terminate()
                log.warning("Auth code command timed out after %ss.", timeout)
                break
    finally:
        try:
            remaining, _ = process.communicate(timeout=3)
        except Exception:
            remaining = ""
        if remaining:
            merged_lines.append(remaining)

    merged_output = "".join(merged_lines)
    if not code:
        code = _extract_auth_code(merged_output)
    if code:
        log.info("Auth code captured from command output.")
        return code

    return_code = process.returncode
    if return_code and return_code != 0:
        log.warning("Auth code command exited non-zero (%s).", return_code)
    else:
        log.warning("Auth code command completed but no auth code found in output.")
    return ""


def _validate_oauth_creds(creds, log):
    required = ("UID", "client_id", "Secret_Code", "oauth_url")
    missing = [k for k in required if not str(creds.get(k, "")).strip()]
    if missing:
        log.warning("OAuth pre-flight: cred.yml missing required fields: %s", missing)

    secret_code = str(creds.get("Secret_Code", "")).strip()
    if secret_code and len(secret_code) < 50:
        log.warning(
            "OAuth pre-flight: Secret_Code is %d chars; working value is 64 chars. "
            "Likely the dummy/old value — exchange will return INVALID_VERIFIER.",
            len(secret_code),
        )

    token_url = os.environ.get("SHOONYA_TOKEN_URL", "").strip() or str(creds.get("token_url", "")).strip()
    if token_url and "api.shoonya.com" not in token_url:
        if "trade.shoonya.com" in token_url:
            log.warning(
                "OAuth pre-flight: token_url uses trade.shoonya.com which enforces "
                "static-IP whitelist. Switch to api.shoonya.com to bypass. Current: %s",
                token_url,
            )
        else:
            log.warning("OAuth pre-flight: token_url on unrecognized host: %s", token_url)


def _initialize_api_oauth(api, creds, log, alerts=None, cred_path=SHARED_CRED):
    _validate_oauth_creds(creds, log)
    uid = str(creds.get("UID", "")).strip()
    client_id = str(creds.get("client_id", "")).strip()
    secret_code = str(creds.get("Secret_Code", "")).strip()
    oauth_url = str(creds.get("oauth_url", "")).strip()
    token_url = (
        os.environ.get("SHOONYA_TOKEN_URL", "").strip()
        or str(creds.get("token_url", "")).strip()
    )
    oauth_api_host = (
        os.environ.get("SHOONYA_OAUTH_API_HOST", "").strip()
        or str(creds.get("oauth_api_host", "")).strip()
        or "https://api.shoonya.com/NorenWClientAPI/"
    )
    oauth_ws_endpoint = (
        os.environ.get("SHOONYA_OAUTH_WS", "").strip()
        or str(creds.get("oauth_ws_endpoint", "")).strip()
        or "wss://api.shoonya.com/NorenWS/"
    )
    account_id = str(creds.get("Account_ID", "")).strip() or uid
    access_token = str(creds.get("Access_token", "")).strip()
    retry_raw = (
        os.environ.get("SHOONYA_OAUTH_REAUTH_ATTEMPTS", "").strip()
        or str(creds.get("oauth_reauth_attempts", "2")).strip()
    )
    try:
        oauth_reauth_attempts = max(1, int(retry_raw))
    except ValueError:
        oauth_reauth_attempts = 2

    api.configure_oauth_service_host(oauth_api_host, oauth_ws_endpoint)

    if access_token:
        api.inject_oauth_header(access_token, uid, account_id)
        if api.validate_oauth_session():
            log.info("OAuth login: using cached access token (%s).", _mask_secret(access_token))
            return api
        detail = api.get_last_broker_error()
        log.warning(
            "OAuth login: cached token invalid/expired%s, re-auth required.",
            f", broker says: {detail}" if detail else "",
        )

    oauth_login_url = api.get_oauth_url(oauth_url, client_id)
    if not oauth_login_url:
        raise RuntimeError("Unable to generate OAuth login URL")

    for attempt in range(1, oauth_reauth_attempts + 1):
        auth_code = os.environ.get("SHOONYA_AUTH_CODE", "").strip()
        if not auth_code and shoonya_selenium_auth.is_configured(creds):
            log.info("OAuth re-auth attempt %s/%s: capturing auth code via in-process Selenium.",
                     attempt, oauth_reauth_attempts)
            auth_code = shoonya_selenium_auth.fetch_auth_code(creds)
        if not auth_code:
            auth_code = _fetch_auth_code_from_command(creds, log)
        if not auth_code:
            log.warning("OAuth re-auth attempt %s/%s: no auth code captured.", attempt, oauth_reauth_attempts)
            continue

        token_data = api.exchange_auth_code(auth_code, secret_code, client_id, uid, token_url=token_url)
        if not token_data:
            detail = api.get_last_broker_error() or "Unknown token exchange failure"
            log.warning("OAuth re-auth attempt %s/%s failed at token exchange: %s",
                        attempt, oauth_reauth_attempts, detail)
            continue

        new_access_token, user_id, _refresh_token, new_account_id = token_data
        api.inject_oauth_header(new_access_token, user_id, new_account_id)
        if api.validate_oauth_session():
            creds["Access_token"] = new_access_token
            creds["Account_ID"] = new_account_id
            creds["UID"] = user_id
            _save_creds(creds, path=cred_path)
            log.info("OAuth login successful; access token cached to %s (%s).",
                     cred_path, _mask_secret(new_access_token))
            return api

        detail = api.get_last_broker_error() or "Unknown validation failure"
        log.warning("OAuth re-auth attempt %s/%s failed at session validation: %s",
                    attempt, oauth_reauth_attempts, detail)

    # Manual fallback
    log.info("Open this URL, complete login, then paste the auth code:\n%s", oauth_login_url)
    auth_code = input("Enter Shoonya auth code: ").strip()
    token_data = api.exchange_auth_code(auth_code, secret_code, client_id, uid, token_url=token_url)
    if not token_data:
        detail = api.get_last_broker_error() or "Unknown token exchange failure"
        raise RuntimeError(f"OAuth token exchange failed: {detail}")
    new_access_token, user_id, _refresh_token, new_account_id = token_data
    api.inject_oauth_header(new_access_token, user_id, new_account_id)
    if not api.validate_oauth_session():
        detail = api.get_last_broker_error() or "Unknown validation failure"
        raise RuntimeError(f"OAuth session validation failed after token exchange: {detail}")
    creds["Access_token"] = new_access_token
    creds["Account_ID"] = new_account_id
    creds["UID"] = user_id
    _save_creds(creds, path=cred_path)
    log.info("OAuth login successful (manual fallback); access token cached to %s (%s).",
             cred_path, _mask_secret(new_access_token))
    return api


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shoonya OAuth login — writes token to ~/.shoonya/cred.yml")
    parser.add_argument("--cred-file", default=SHARED_CRED, help="Path to cred.yml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("shoonya-auth")

    creds = _load_creds(args.cred_file)
    if not _is_oauth_configured(creds):
        log.error("cred.yml at %s missing required OAuth fields. See cred.yml.template.", args.cred_file)
        sys.exit(1)

    api = ShoonyaApiPy()
    _initialize_api_oauth(api, creds, log, cred_path=args.cred_file)
    log.info("Login complete. Token written to %s", args.cred_file)

"""AWS credential handling for Bedrock, including short-lived profiles written by
gimme-aws-creds / aws sso / STS. Checks expiry before a sweep and optionally refreshes."""

from __future__ import annotations

import configparser
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nbrain.config.schema import LLMConfig

log = logging.getLogger(__name__)


@dataclass
class AwsCredState:
    ok: bool
    identity: str | None = None
    expires_at: datetime | None = None
    detail: str = ""

    @property
    def minutes_left(self) -> float | None:
        if not self.expires_at:
            return None
        return (self.expires_at - datetime.now(UTC)).total_seconds() / 60


def _profile_expiry(profile: str | None) -> datetime | None:
    """gimme-aws-creds and similar tools write x_security_token_expires / aws_expiration."""
    path = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")).expanduser()
    if not path.exists():
        return None
    cp = configparser.RawConfigParser()
    try:
        cp.read(path)
    except configparser.Error:
        return None
    section = profile or os.environ.get("AWS_PROFILE") or "default"
    if not cp.has_section(section):
        return None
    for key in ("x_security_token_expires", "aws_expiration", "expiration", "aws_session_expiration"):
        if cp.has_option(section, key):
            raw = cp.get(section, key).strip()
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def check_credentials(cfg: LLMConfig, *, verify_identity: bool = True) -> AwsCredState:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    expiry = _profile_expiry(cfg.profile)
    if expiry and expiry <= datetime.now(UTC):
        return AwsCredState(False, expires_at=expiry, detail=f"profile '{cfg.profile or 'default'}' expired at {expiry.isoformat()}")
    if not verify_identity:
        return AwsCredState(True, expires_at=expiry, detail="expiry read from credentials file")
    try:
        session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
        creds = session.get_credentials()
        if creds is None:
            return AwsCredState(False, detail="no AWS credentials found in the boto3 chain")
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        arn = ident.get("Arn", "?")
        frozen = creds.get_frozen_credentials()
        # botocore exposes expiry on refreshable creds; fall back to the file
        exp = getattr(creds, "_expiry_time", None) or expiry
        return AwsCredState(True, identity=arn, expires_at=exp, detail=f"token present ({'temporary' if frozen.token else 'long-lived'})")
    except NoCredentialsError:
        return AwsCredState(False, detail="no AWS credentials found (set AWS_PROFILE or run your credential tool)")
    except (ClientError, BotoCoreError) as e:
        return AwsCredState(False, expires_at=expiry, detail=f"sts get-caller-identity failed: {e}")


def refresh_credentials(cfg: LLMConfig, *, timeout: int = 180) -> tuple[bool, str]:
    if not cfg.refresh_command:
        return False, "no refresh_command configured"
    log.info("Refreshing AWS credentials with: %s", cfg.refresh_command)
    try:
        proc = subprocess.run(
            shlex.split(cfg.refresh_command),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,  # never block a daemon on an MFA prompt
        )
    except FileNotFoundError:
        return False, f"refresh command not found: {cfg.refresh_command.split()[0]}"
    except subprocess.TimeoutExpired:
        return False, "refresh command timed out (interactive MFA prompt?) — run it in a terminal"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        return False, "refresh command failed: " + " | ".join(tail)
    return True, "credentials refreshed"


def ensure_credentials(cfg: LLMConfig) -> AwsCredState:
    """Called before a sweep when the provider is bedrock. Refreshes when missing/expiring."""
    state = check_credentials(cfg)
    threshold = timedelta(minutes=cfg.refresh_when_expiring_within_minutes)
    expiring = state.expires_at is not None and state.expires_at - datetime.now(UTC) < threshold
    if state.ok and not expiring:
        return state
    if cfg.refresh_command:
        ok, msg = refresh_credentials(cfg)
        if ok:
            state = check_credentials(cfg)
            state.detail = msg + "; " + state.detail
            return state
        state.ok = False
        state.detail = f"{state.detail}; {msg}".strip("; ")
        return state
    if expiring and state.ok:
        state.detail += f"; expires in {state.minutes_left:.0f} min and no refresh_command is set"
    return state

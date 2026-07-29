"""Authenticated Garmin Connect client with token caching and retry handling."""
import logging
import time

from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garth.exc import GarthException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from garmin_tracker import config

logger = logging.getLogger(__name__)

RETRYABLE_ERRORS = (
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarthException,
    ConnectionError,
    TimeoutError,
)


def connect() -> Garmin:
    """Return an authenticated Garmin client, reusing a cached token session
    when possible so we don't have to log in on every run."""
    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        raise RuntimeError(
            "GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env (see .env.example)"
        )

    config.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_dir = str(config.TOKEN_DIR)

    client = Garmin(
        email=config.GARMIN_EMAIL,
        password=config.GARMIN_PASSWORD,
        prompt_mfa=lambda: input("Enter the Garmin Connect MFA code sent to you: "),
    )

    # Garmin.login(tokenstore) resumes a cached session from token_dir when one
    # exists and is still valid, otherwise transparently falls back to a fresh
    # credential login and writes new tokens back to token_dir - so a single
    # call handles both the cached and first-run cases.
    client.login(token_dir)
    logger.info("Authenticated with Garmin Connect (session cached at %s)", token_dir)

    return client


def call_with_retry(func, *args, **kwargs):
    """Invoke a Garmin API method with exponential-backoff retry on
    transient errors (network hiccups, rate limiting)."""

    @retry(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def _call():
        return func(*args, **kwargs)

    return _call()


def pace():
    """Small delay between requests to stay rate-limit friendly."""
    time.sleep(config.REQUEST_DELAY_SECONDS)

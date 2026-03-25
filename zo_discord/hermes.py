"""
Hermes backend support for zo-discord.

Encapsulates all Hermes-specific configuration and request/response
handling so the core zo-discord modules stay backend-agnostic.

zo-hermes service: http://127.0.0.1:8788
See the zo-hermes README for setup and troubleshooting details.
"""

import aiohttp
import logging

logger = logging.getLogger(__name__)

# zo-hermes endpoint (localhost only, no auth)
HERMES_URL = "http://127.0.0.1:8788"
HERMES_DEFAULT_MODEL = "gpt-5.4"


async def _hermes_get_json(
    path: str,
    *,
    params: dict | None = None,
    timeout_seconds: int,
) -> tuple[int | None, dict | None]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HERMES_URL}{path}",
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if resp.status != 200:
                    return resp.status, None
                return resp.status, await resp.json()
    except Exception:
        return None, None


async def check_hermes_status(session_id: str) -> dict | None:
    """Check zo-hermes agent status for a session.

    Returns dict with 'state' ('running'|'idle'), 'iterations_used', etc.
    Returns None if hermes is unreachable or session not found.
    """
    status, body = await _hermes_get_json(
        "/status",
        params={"session_id": session_id},
        timeout_seconds=5,
    )
    if body is not None:
        return body
    if status is not None:
        logger.warning("Hermes /status returned %s for session %s", status, session_id)
    else:
        logger.warning("Hermes /status unreachable for session %s", session_id)
    return None


async def check_hermes_health() -> bool:
    """Basic liveness check — is zo-hermes responding?"""
    status, _body = await _hermes_get_json("/health", timeout_seconds=2)
    return status == 200


def is_hermes(backend: str | None, default_backend: str = "zo") -> bool:
    """Check if the effective backend is Hermes."""
    return (backend or default_backend) == "hermes"


def get_request_config(api_key: str, backend: str | None, default_backend: str = "zo") -> tuple[str, dict]:
    """
    Return (api_url, headers) for the given backend.

    Args:
        api_key: Zo API key (used for Zo backend only)
        backend: Per-request backend override
        default_backend: Global default backend from config

    Returns:
        (url, headers) tuple ready for aiohttp.post()
    """
    if is_hermes(backend, default_backend):
        return (
            f"{HERMES_URL}/ask",
            {"Content-Type": "application/json"},
        )
    return (
        "https://api.zo.computer/zo/ask",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )


def get_backend_label(backend: str | None, default_backend: str = "zo") -> str:
    """Human-readable label for logging."""
    return "Hermes" if is_hermes(backend, default_backend) else "Zo"


def get_model_fallback_notice(requested_model: str | None, backend: str | None, default_backend: str = "zo") -> str | None:
    """Explain when Hermes must ignore a Zo-style BYOK model ID."""
    if not is_hermes(backend, default_backend):
        return None
    if not requested_model or not requested_model.startswith("byok:"):
        return None
    return (
        f"*Hermes can't use Zo BYOK model IDs (`{requested_model}`), "
        f"so this turn is falling back to `{HERMES_DEFAULT_MODEL}`.*"
    )


def get_persona_ignored_notice(persona_id: str | None, backend: str | None, default_backend: str = "zo") -> str | None:
    """Explain when Hermes must ignore a Zo persona override."""
    if not is_hermes(backend, default_backend):
        return None
    if not persona_id:
        return None
    return (
        f"*Hermes can't use Zo persona IDs (`{persona_id}`), so this conversation is continuing "
        "with the current Hermes personality/prompt setup.*"
    )


def handle_session_id_change(event_data: dict, current_conv_id: str) -> str | None:
    """
    Check if Hermes changed the session ID (due to context compression).

    After compression, Hermes creates a new session linked via parent_session_id.
    zo-hermes propagates the new ID in the End event's conversation_id field.

    Args:
        event_data: The parsed 'data' dict from an SSE End event
        current_conv_id: The conversation ID we sent in the request

    Returns:
        New conversation ID if changed, None otherwise
    """
    new_conv = event_data.get("conversation_id")
    if new_conv and new_conv != current_conv_id:
        logger.info("Session ID changed (compression): %s -> %s", current_conv_id, new_conv)
        return new_conv
    return None

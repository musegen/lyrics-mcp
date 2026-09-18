#!/usr/bin/env python3
"""MCP server that writes original song lyrics with Claude.

Bring your own Anthropic API key: this server talks to the Anthropic API as you,
with your credentials, and nothing else. There is no hosted backend and no
telemetry -- every request is billed to the key in your own environment.

Transport is stdio, so an MCP client starts and stops the process for you.
See README.md for client configuration.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from importlib import metadata
from typing import Annotated

import anthropic
from mcp.server.mcpserver import MCPServer
from pydantic import Field

try:
    __version__ = metadata.version("lyrics-mcp")
except metadata.PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.0.0+dev"

# Claude Opus 5 is the default: lyrics are a creative task where model quality is
# the whole product. Override with LYRICS_MCP_MODEL if you want to trade quality
# for cost -- that is your call, not this server's.
DEFAULT_MODEL = "claude-opus-5"

# Safety classifiers can decline a request outright (stop_reason "refusal"), which
# is a real failure mode for lyrics about grief, violence or addiction. Server-side
# fallbacks re-run the declined request on this model inside the same API call.
# Set LYRICS_MCP_FALLBACK_MODEL="" to disable.
DEFAULT_FALLBACK_MODEL = "claude-opus-4-8"
FALLBACK_BETA = "server-side-fallback-2026-06-01"

# Generous enough for a long song plus the model's own reasoning, low enough to
# stay well inside the SDK's default HTTP timeout without streaming.
MAX_OUTPUT_TOKENS = 16000

# A runaway agent loop on this tool spends real money. Ten calls a minute is
# plenty for a human songwriting session and caps the damage from a stuck loop.
DEFAULT_RPM = 10


def _log(msg: str) -> None:
    """Log to stderr -- stdout belongs to the JSON-RPC stream under stdio."""
    print(f"[lyrics-mcp] {msg}", file=sys.stderr, flush=True)


class RateLimiter:
    """Token bucket over this process's calls to the Anthropic API.

    This protects the key holder's own bill from a runaway agent loop. It is
    per-process and therefore not a security control: anyone running their own
    copy of this server gets their own bucket, spending their own money.
    """

    def __init__(self, per_minute: int) -> None:
        self.capacity = max(1, per_minute)
        self._tokens = float(self.capacity)
        self._refill_per_second = self.capacity / 60.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Take one token, or raise with the wait time if the bucket is empty."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_per_second)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._refill_per_second
                raise RuntimeError(
                    f"Rate limit reached ({self.capacity} requests/minute). "
                    f"Try again in about {wait:.0f}s, or raise LYRICS_MCP_RPM."
                )
            self._tokens -= 1.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log(f"{name}={raw!r} is not an integer, using {default}")
        return default


MODEL = os.environ.get("LYRICS_MCP_MODEL", "").strip() or DEFAULT_MODEL
FALLBACK_MODEL = os.environ.get("LYRICS_MCP_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip()
RATE_LIMITER = RateLimiter(_env_int("LYRICS_MCP_RPM", DEFAULT_RPM))

_client: anthropic.Anthropic | None = None
_client_lock = threading.Lock()
# Flipped off if the endpoint rejects the fallback beta, which is what a proxy or
# a gateway that has not adopted it will do.
_fallbacks_supported = True


def get_client() -> anthropic.Anthropic:
    """Build the client lazily so importing this module never needs credentials.

    The zero-argument constructor resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
    and ANTHROPIC_BASE_URL from the environment, which is what lets this server
    point at a gateway without any code change.
    """
    global _client
    with _client_lock:
        if _client is None:
            try:
                _client = anthropic.Anthropic()
            except Exception as exc:
                raise RuntimeError(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY in the "
                    f"environment your MCP client launches this server in ({exc})."
                ) from None
        return _client


def build_prompt(brief: str, language: str) -> str:
    """Assemble the songwriting instruction.

    The craft guidance is deliberately framed as guidelines rather than rules --
    stated as hard constraints, models tend to echo the checklist back into the
    lyrics instead of writing from it.
    """
    brief = brief.strip() or "(no specific brief, use your creativity)"
    language = language.strip()

    parts = [
        "You are a professional songwriter. Write original, emotionally resonant "
        "song lyrics based on the brief below.\n\n",
        f"Brief:\n{brief}\n\n",
        "Songwriting craft (guidelines, not rules -- never echo these into the output):\n",
        "- Structure: if the brief specifies a structure, follow it; otherwise pick a fitting "
        "skeleton (e.g. ABABCB for pop/rock, AABA for ballads, AAA for narrative folk). Build "
        "from Verse / Pre-Chorus / Chorus / Bridge sections, and let the structure serve the "
        "emotion.\n",
        "- Emotional arc: treat the song as a journey, not a flat line. Build energy toward the "
        "chorus, peak near the final chorus, and use contrast (quiet vs. loud, sparse vs. full) "
        "-- restraint is also a tool.\n",
        "- Hook: give the chorus a memorable, repeatable hook where lyric and emotion align.\n",
        "- Writing: show, do not tell (concrete images over abstract statements); vary rhyme "
        "(mix perfect, slant and internal rhyme); keep every line singable when read aloud; "
        "avoid cliches and forced word order.\n",
        "- Prosody: match steady emotion with steady phrasing, and unstable emotion with "
        "restless, wandering phrasing.\n\n",
        "Requirements:\n",
    ]

    if language:
        parts.append(f"- Write ALL lyrics strictly in this language: {language}.\n")
    else:
        parts.append(
            "- Write the lyrics in the language implied by the brief; if unclear, use English.\n"
        )

    parts += [
        "- Mark every section with English structure tags in square brackets, e.g. [Verse], "
        "[Pre-Chorus], [Chorus], [Bridge], [Outro] -- keep these tags in ENGLISH even when the "
        "lyrics are written in another language.\n",
        "- Do NOT use real artist, band, or brand names; describe a sound or style instead.\n",
        "- Keep it coherent and singable.\n",
        "- Do NOT add explanations or commentary.\n",
        "- Return STRICTLY in this format and nothing else:\n",
        "TITLE: <a short song title>\n",
        "LYRICS:\n",
        "<the full lyrics>\n",
    ]
    return "".join(parts)


_TITLE_RE = re.compile(r"^[ \t]*TITLE[ \t]*[:：][ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)
_SECTION_TAG_RE = re.compile(r"\[[^\]]*\]")


def parse_result(raw: str) -> dict[str, str]:
    """Split the model output into title and body.

    Models occasionally drop one of the two markers, so both are optional: with no
    LYRICS marker the remainder becomes the body, and with no TITLE the first
    non-empty line is promoted to the title.
    """
    text = raw.strip()
    title = ""

    match = _TITLE_RE.search(text)
    if match:
        title = match.group(1).strip()

    upper = text.upper()
    index = upper.find("LYRICS:")
    if index < 0:
        index = upper.find("LYRICS：")
    if index >= 0:
        text = text[index + len("LYRICS:") :].strip()
    elif match:
        text = _TITLE_RE.sub("", text, count=1).strip()

    if not title:
        # Fall back to the first line of real lyric, skipping section tags -- a song
        # titled "Verse" is what you get if you just take line one.
        for line in text.split("\n"):
            candidate = line.strip()
            if not candidate or _SECTION_TAG_RE.fullmatch(candidate):
                continue
            title = candidate[:60]
            break

    return {"title": title, "lyrics": text}


def _extract_text(response) -> str:
    """Join every text block.

    The first block may be a thinking block with no prose, so this iterates rather
    than reading content[0].
    """
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()


def _create_message(prompt: str):
    """One Anthropic API call, with server-side refusal fallbacks when available.

    `thinking` is deliberately not set: Claude Opus 5 runs adaptive thinking by
    default, while models that still take `budget_tokens` would reject an explicit
    adaptive block -- omitting it keeps every LYRICS_MCP_MODEL value working.
    """
    global _fallbacks_supported
    client = get_client()
    kwargs = {
        "model": MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    if FALLBACK_MODEL and _fallbacks_supported:
        try:
            return client.beta.messages.create(
                betas=[FALLBACK_BETA],
                fallbacks=[{"model": FALLBACK_MODEL}],
                **kwargs,
            )
        except anthropic.BadRequestError as exc:
            if "fallback" not in str(exc).lower():
                raise
            _fallbacks_supported = False
            _log(
                "endpoint rejected the refusal-fallback beta; continuing without it "
                "(a declined request will now surface as an error)"
            )

    return client.messages.create(**kwargs)


def _call_model(prompt: str) -> str:
    """Call the API and return the prose, translating SDK errors into MCP-visible ones."""
    try:
        response = _create_message(prompt)
    except anthropic.AuthenticationError:
        raise RuntimeError(
            "Anthropic rejected the credentials. Check ANTHROPIC_API_KEY."
        ) from None
    except anthropic.PermissionDeniedError:
        raise RuntimeError("This API key is not allowed to call the Messages API.") from None
    except anthropic.NotFoundError:
        raise RuntimeError(f"Model {MODEL!r} not found. Check LYRICS_MCP_MODEL.") from None
    except anthropic.RateLimitError as exc:
        retry_after = exc.response.headers.get("retry-after", "60")
        raise RuntimeError(f"Anthropic rate limit hit. Retry after {retry_after}s.") from None
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise RuntimeError(
                f"Anthropic server error ({exc.status_code}). Retry later."
            ) from None
        raise RuntimeError(f"Anthropic API error ({exc.status_code}): {exc.message}") from None
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Could not reach the Anthropic API: {exc}") from None

    if response.stop_reason == "refusal":
        detail = ""
        if response.stop_details:
            detail = f" (category: {response.stop_details.category})"
        raise RuntimeError(
            f"Claude declined to write these lyrics{detail}. Rewriting the brief in less "
            "graphic terms usually gets a result."
        )

    return _extract_text(response)


mcp = MCPServer(
    name="lyrics",
    version=__version__,
    instructions=(
        "Writes original song lyrics with Claude. Section tags come back in English "
        "([Verse], [Chorus], ...) whatever language the lyrics are in, so the output "
        "drops straight into a music-generation model."
    ),
)


@mcp.tool(
    description=(
        "Write a complete set of original song lyrics from a creative brief. Returns a "
        "title and a body marked up with English section tags ([Verse], [Pre-Chorus], "
        "[Chorus], [Bridge], [Outro]) that stay English whatever language the lyrics "
        "are in, ready to paste into a music-generation model."
    )
)
def write_lyrics(
    brief: Annotated[
        str,
        Field(
            description=(
                "What the song is about. The more concrete the imagery, the better the "
                "result -- 'walking home alone through a quiet city after a late shift' "
                "beats 'a sad song'. Narrative point of view and specific details belong "
                "here too."
            )
        ),
    ],
    language: Annotated[
        str,
        Field(
            description=(
                "Language for the lyrics, e.g. 'English', 'Chinese', 'Japanese'. Leave "
                "empty to follow the brief's own language. Section tags stay English "
                "regardless."
            )
        ),
    ] = "",
    genre: Annotated[
        str,
        Field(description="Musical genre, e.g. 'indie folk', 'synth pop', 'trap'."),
    ] = "",
    mood: Annotated[
        str,
        Field(description="Emotional register, e.g. 'wistful but hopeful', 'defiant'."),
    ] = "",
    structure: Annotated[
        str,
        Field(
            description=(
                "Song structure, e.g. 'ABABCB' or 'Verse/Chorus/Verse/Chorus/Bridge/Chorus'. "
                "Leave empty to let the model pick one that fits the emotion."
            )
        ),
    ] = "",
) -> dict[str, str]:
    """Write lyrics and return the title and body."""
    if not brief or not brief.strip():
        raise ValueError("brief is required: describe what the song should be about.")

    RATE_LIMITER.acquire()

    # Only non-empty fields go into the brief; empty labels read as noise to the model.
    labelled = [f"Brief: {brief.strip()}"]
    for label, value in (("Genre", genre), ("Mood", mood), ("Structure", structure)):
        if value and value.strip():
            labelled.append(f"{label}: {value.strip()}")

    prompt = build_prompt("\n".join(labelled), language)

    # An empty response means the model returned nothing usable; one retry clears
    # the transient version of that.
    raw = _call_model(prompt)
    if not raw:
        _log("empty response, retrying once")
        raw = _call_model(prompt)
    if not raw:
        raise RuntimeError("Lyrics generation failed: the model returned no text twice.")

    result = parse_result(raw)
    _log(f"wrote {result['title']!r} ({len(result['lyrics'])} chars) with {MODEL}")
    return result


def main() -> None:
    _log(f"starting: model={MODEL} rpm={RATE_LIMITER.capacity}")
    mcp.run()


if __name__ == "__main__":
    main()

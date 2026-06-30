import argparse
import asyncio
import json
import logging
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx
import uvicorn
from fastmcp import FastMCP
from mcp.server.fastmcp import Context
from starlette.middleware.cors import CORSMiddleware

global TRANSLATE_API_URL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("translategemma_mcp")

# Optional: Rate limiter for safety (you can remove or relax if not needed)
class RateLimiter:
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.requests: list[datetime] = []

    async def acquire(self):
        now = datetime.now()
        self.requests = [t for t in self.requests if now - t < timedelta(minutes=1)]

        if len(self.requests) >= self.requests_per_minute:
            oldest = min(self.requests)
            wait_time = 60 - (now - oldest).total_seconds()
            if wait_time > 0:
                await asyncio.sleep(wait_time)

        self.requests.append(now)


# Initialize services
RATE_LIMITER = RateLimiter(requests_per_minute=30)

TRANSLATE_API_URL = "http://127.0.0.1:8080/v1/chat/completions"


# Initialize FastMCP server
mcp = FastMCP("translategemma")
logger.info("✅ FastMCP instance created")


@mcp.tool()
async def translate(
    text: str,
    source_lang_code: str,
    target_lang_code: str,
    ctx: Context,
    max_retries: int = 2,
) -> str:
    """
    Translate text using TranslateGemma via local API.

    Args:
        text: The text to translate (required)
        source_lang_code: Source language code (e.g., "en", "auto") (required)
        target_lang_code: Target language code (e.g., "de-DE", "fr-FR", "ja-JP") (required)
        ctx: MCP context for logging (injected automatically)
        max_retries: Max retry attempts on transient failures (default: 2)

    Returns:
        Translated text or error message.
    """
    try:
        await RATE_LIMITER.acquire()
        logger.info(f"Translating: '{text[:50]}...' → {target_lang_code} (from {source_lang_code})")

        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps({
                        "type": "text",
                        "source_lang_code": source_lang_code,
                        "target_lang_code": target_lang_code,
                        "text": text,
                    })
                }
            ]
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={"Content-Type": "application/json"}
        ) as client:
            for attempt in range(1, max_retries + 2):
                try:
                    response = await client.post(TRANSLATE_API_URL, json=payload)
                    response.raise_for_status()
                    break
                except httpx.TransportError as e:
                    if attempt > max_retries:
                        raise
                    logger.warning(f"⚠️ Transport error (attempt {attempt}/{max_retries + 1}): {e}")
                    await asyncio.sleep(1 * attempt)  # exponential backoff-ish

        result = response.json()
        logger.debug(f"Raw API response: {result}")

        # Extract translation from response
        choices = result.get("choices", [])
        if not choices:
            raise ValueError("No 'choices' in response")

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if not content:
            raise ValueError("Empty 'content' in response message")

        # --- LOGIC TO HANDLE NESTED JSON VS PLAIN STRING ---
        # 1. Try to strip whitespace
        content = content.strip()

        # 2. Check if the content starts with a curly brace (indicating JSON)
        if content.startswith('{'):
            try:
                # Attempt to parse the content as JSON
                nested_data = json.loads(content)

                # If it has a 'text' field, that is our translation (the nested format)
                if "text" in nested_data:
                    final_translation = nested_data["text"]
                # If it has a 'content' field but no 'text', use that (less common)
                elif "content" in nested_data:
                    final_translation = nested_data["content"]
                else:
                    # If JSON parsed but no standard field found, return stringified JSON
                    logger.warning("Parsed JSON but found no 'text' or 'content' field. Returning raw JSON.")
                    final_translation = content
            except json.JSONDecodeError:
                # Fallback: If it looks like JSON but fails to parse, return raw string
                final_translation = content
        else:
            # 3. It is a plain string (the standard format)
            final_translation = content

        # --- END LOGIC ---

        logger.info(f"Translation successful: '{final_translation[:50]}...'")
        return final_translation.strip()

    except httpx.TimeoutException:
        error_msg = "❌ Translation request timed out. Please try again."
        logger.error(error_msg)
        return error_msg
    except httpx.HTTPStatusError as e:
        error_msg = f"❌ HTTP error {e.response.status_code}: {e.response.text[:500]}"
        logger.error(error_msg)
        return error_msg
    except json.JSONDecodeError as e:
        error_msg = f"❌ Invalid JSON in response: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except ValueError as e:
        error_msg = f"❌ Translation failed: {str(e)}"
        logger.warning(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"❌ Unexpected error: {str(e)}"
        logger.exception(error_msg)
        return error_msg


# Create ASGI app
app = mcp.http_app(path="/mcp")
print(f"✅ Created ASGI app: {type(app)}")

# CORS middleware (critical for browser clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "mcp-protocol-version",
        "mcp-session-id",
    ],
    expose_headers=["mcp-session-id"],
)
print("✅ CORS middleware added")


# CLI parsing
def parse_args():
    parser = argparse.ArgumentParser(
        description="TranslateGemma MCP Server - Text Translation Tool"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="Server port (default: 3000)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=TRANSLATE_API_URL,
        help=f"TranslateGemma API URL (default: {TRANSLATE_API_URL})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Allow overriding API URL via CLI (useful for dev/testing)

    TRANSLATE_API_URL = args.api_url

    print(f"🚀 Starting MCP server on {args.host}:{args.port}/mcp")
    print(f"📡 Using TranslateGemma API at {TRANSLATE_API_URL}")
    uvicorn.run(app, host=args.host, port=args.port)

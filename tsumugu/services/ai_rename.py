"""
AI-powered file rename module.
Uses LLM to analyze filenames and suggest clean "Song-Artist" format names.
"""
import json
import os
import re
import subprocess
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class RenameSuggestion:
    original_path: str
    original_name: str
    suggested_name: str
    confidence: str  # "high", "medium", "low", "skip"
    reason: str


def get_opencode_config() -> dict:
    """Read opencode.json to get API configuration."""
    config_path = os.path.expanduser("~/.config/opencode/opencode.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


# Known provider base URLs (when config has empty baseUrl)
_KNOWN_PROVIDER_URLS = {
    "volcengine": "https://ark.cn-beijing.volces.com/api/coding/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
    "deepseek": "https://api.deepseek.com",
    "mimo": "https://api.xiaomi.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}

_ANTHROPIC_KNOWN_URLS = {
    "mimo-anthropic": "https://api.anthropic.com",
}

# Default models per provider (used when config doesn't specify one)
_KNOWN_MODELS = {
    "volcengine": "doubao-pro-32k",
    "zhipu": "glm-4-flash",
    "kimi": "moonshot-v1-8k",
    "deepseek": "deepseek-chat",
    "mimo": "MiMo-7B-RL",
    "mimo-anthropic": "claude-3-haiku-20240307",
    "google": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
}


# Provider priority: try these first (known working providers)
_PROVIDER_PRIORITY = ["deepseek", "mimo", "kimi", "zhipu", "volcengine", "google"]


def get_api_config() -> tuple:
    """Get API endpoint, key, and model from config. Returns (api_type, api_key, base_url, model)."""
    config = get_opencode_config()
    providers = config.get("provider", {})

    # Sort providers by priority (known working first), then alphabetically
    def _sort_key(name):
        try:
            return (_PROVIDER_PRIORITY.index(name), name)
        except ValueError:
            return (len(_PROVIDER_PRIORITY), name)

    sorted_providers = sorted(providers.keys(), key=_sort_key)

    for provider_key in sorted_providers:
        provider_data = providers[provider_key]
        options = provider_data.get("options", {})
        api_key = options.get("apiKey", "")
        # Config may use "baseUrl" or "baseURL" (camelCase)
        base_url = options.get("baseUrl", "") or options.get("baseURL", "")

        if not api_key:
            continue

        api_type = provider_data.get("api", "")
        model = provider_data.get("model", "")
        # Also check the "models" dict (opencode.json format)
        if not model:
            models_dict = provider_data.get("models", {})
            if models_dict:
                model = next(iter(models_dict.keys()), "")
        if not model:
            model = _KNOWN_MODELS.get(provider_key, "")

        # Anthropic-type providers
        if "anthropic" in api_type.lower() or "claude" in provider_key.lower():
            if not base_url:
                base_url = _ANTHROPIC_KNOWN_URLS.get(provider_key, "https://api.anthropic.com")
            if not model:
                model = "claude-3-haiku-20240307"
            return ("anthropic", api_key, base_url, model)

        # OpenAI-compatible providers
        if "openai" in api_type.lower() or "openai" in provider_key.lower():
            if not base_url:
                base_url = _KNOWN_PROVIDER_URLS.get(provider_key, "https://api.openai.com/v1")
            if not model:
                model = "gpt-4o-mini"
            return ("openai", api_key, base_url, model)

        # Generic: needs explicit base URL
        if base_url:
            if not model:
                model = "gpt-4o-mini"
            return ("openai", api_key, base_url, model)

    return ("", "", "", "")


def call_llm(prompt: str, system_prompt: str = "") -> str:
    """Call LLM API to get response."""
    api_type, api_key, base_url, model = get_api_config()

    if not api_key:
        raise Exception("No API key configured. Please set up AI in Settings.")

    if api_type == "anthropic":
        return call_anthropic(api_key, base_url, model, prompt, system_prompt)
    else:
        return call_openai(api_key, base_url, model, prompt, system_prompt)


def call_openai(api_key: str, base_url: str, model: str, prompt: str, system_prompt: str) -> str:
    """Call OpenAI-compatible API."""
    import urllib.request
    import urllib.error

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    data = json.dumps({
        "model": model or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2000
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        raise Exception(f"API error {e.code}: {error_body}")


def call_anthropic(api_key: str, base_url: str, model: str, prompt: str, system_prompt: str) -> str:
    """Call Anthropic Claude API."""
    import urllib.request
    import urllib.error

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }

    data = json.dumps({
        "model": model or "claude-3-haiku-20240307",
        "max_tokens": 2000,
        "system": system_prompt if system_prompt else "You are a helpful assistant.",
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=data,
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            content = result.get("content", [])
            if content and len(content) > 0:
                return content[0].get("text", "")
            return ""
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else str(e)
        raise Exception(f"API error {e.code}: {error_body}")


def analyze_filenames(files: List[Dict]) -> List[RenameSuggestion]:
    """Analyze a list of files and suggest clean names using AI."""
    if not files:
        return []

    # Build file list for AI
    file_descriptions = []
    for i, file_info in enumerate(files):
        name = file_info.get("name", "")
        path = file_info.get("path", "")
        file_type = file_info.get("type", "")
        file_descriptions.append(f"{i+1}. {name}")

    file_list = "\n".join(file_descriptions)

    system_prompt = """You are a music file renaming assistant. Your job is to analyze music file names and suggest clean, organized names in "Song-Artist" format.

RULES:
1. ONLY rename if you are VERY CONFIDENT about the song title and artist
2. If you are NOT SURE about a file, set confidence to "skip" - do NOT guess
3. Clean up the filename: remove extra text like "[新歌速递]", numbers, emojis, special characters
4. Extract the actual song title and artist name
5. Format as "Song Title - Artist Name"
6. If a file is not a music file or you can't determine the name, skip it
7. Be precise - do not make up or guess information

OUTPUT FORMAT:
Return a JSON array with objects containing:
- "index": the file number (1-based)
- "suggested_name": the new filename with extension (e.g., "Song Title - Artist.mp3")
- "confidence": "high" or "skip" (only "high" if absolutely sure)
- "reason": brief explanation

IMPORTANT: Return ONLY valid JSON, no other text."""

    prompt = f"""Analyze these music files and suggest clean "Song-Artist" names:

{file_list}

Remember:
- ONLY suggest names you are 100% confident about
- If unsure about any file, set confidence to "skip"
- Format: "Song Title - Artist.ext"
- Return as JSON array"""

    try:
        response = call_llm(prompt, system_prompt)
        print(f"[AI-Rename] Raw response ({len(response)} chars): {response[:300]}")

        # Strip markdown code blocks if present
        cleaned = response.strip()
        cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
        cleaned = re.sub(r'\n?```\s*$', '', cleaned)
        cleaned = cleaned.strip()

        # Try to parse as JSON array directly first
        suggestions_data = None
        try:
            suggestions_data = json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Fallback: extract JSON array with regex
        if not isinstance(suggestions_data, list):
            json_match = re.search(r'\[[\s\S]*\]', cleaned)
            if json_match:
                suggestions_data = json.loads(json_match.group())

        if not isinstance(suggestions_data, list):
            print(f"[AI-Rename] Failed to parse response: {cleaned[:500]}")
            raise Exception("Invalid response format from AI")

        # Convert to RenameSuggestion objects
        suggestions = []
        for i, file_info in enumerate(files):
            suggestion_data = next(
                (s for s in suggestions_data if s.get("index") == i + 1),
                None
            )

            if suggestion_data and suggestion_data.get("confidence") == "high":
                suggestions.append(RenameSuggestion(
                    original_path=file_info.get("path", ""),
                    original_name=file_info.get("name", ""),
                    suggested_name=suggestion_data.get("suggested_name", file_info.get("name", "")),
                    confidence="high",
                    reason=suggestion_data.get("reason", "")
                ))
            else:
                suggestions.append(RenameSuggestion(
                    original_path=file_info.get("path", ""),
                    original_name=file_info.get("name", ""),
                    suggested_name=file_info.get("name", ""),
                    confidence="skip",
                    reason="AI not confident about this file"
                ))

        return suggestions

    except json.JSONDecodeError as e:
        raise Exception(f"Failed to parse AI response: {str(e)}")
    except Exception as e:
        raise Exception(f"AI analysis failed: {str(e)}")

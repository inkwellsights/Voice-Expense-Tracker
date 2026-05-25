"""Gemini Flash expense parser.

Handles both plain text and receipt images. Free tier: 15 RPM / 1M tokens per day.
Input may be Bengali, English, or Banglish — extract the numeric amount regardless.

Context tags: each entry also carries a 'context' (e.g. 'personal',
'MHUBEXP') derived from what the speaker mentions. The synonym table
is configured via TAG_SYNONYMS in .env; parser.configure_context()
sets the module-level defaults at startup.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

from ..config import CATEGORIES

# Set at startup by main.py via configure_context(...). Defaults below
# keep tests / imports working with no env configuration.
_CONTEXT_SYNONYMS: dict[str, list[str]] = {}
_CONTEXT_DEFAULT: str = "personal"
_CONTEXT_BLOCK: str = ""

logger = logging.getLogger(__name__)

# Cascade through Gemini models on transient failure. Free tier and capacity
# vary independently per model, so a 503/429 on the budget tier shouldn't kill
# the bot when the regular tier is healthy. Order = preferred → fallback.
MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

SYSTEM_PROMPT = f"""You are a cash-flow parser. Extract money-movement entries from the user's input (which may be text, a speech-to-text transcript, or raw audio).
Input may be English, Bengali (Bangla), or Banglish (mixed). Transcripts may have spelling errors.

Each entry has a TYPE — either "expense" (money out) or "income" (money in).

INCOME signal words (English): got, received, earned, refund, paid me, gave me, salary, sent me
INCOME signal words (Bangla/Banglish): paisi, pailam, paaisi, dise (gave to me), diyeche (gave me), beton (salary), tk pailam, taka pailam, gift pailam
EXPENSE signal words (English): spent, paid, bought, gave, cost, bill, fare
EXPENSE signal words (Bangla/Banglish): khoroch, kharcha, disi (I gave), dilam, kinlam, lagse, bhara (fare)

When the speaker says "I gave X taka as gift" → expense (they paid out).
When the speaker says "Someone gave me X" / "I received X" / "I got X" → income.
If unclear, default to "expense".

Convert Bangla number words to digits BEFORE extracting:
- eksho=100, duisho=200, tinsho=300, charsho=400, paansho=500, choisho=600, satsho=700, atsho=800, noisho=900
- hajar=1000, dui hajar=2000, paach hajar=5000, dosh hajar=10000, lakh=100000
- Half/fraction: der sho=150, adai sho=250, saare X sho=(X*100)+50 (saare tin sho=350, saare paach sho=550), saare hajar=1500, adai hajar=2500
- Bangla digits (০১২৩৪৫৬৭৮৯) and "taka"/"টাকা" → normalise to integers.

Be tolerant of Whisper mistranscriptions:
- "share"/"shaare"/"shar" = saare (+50 prefix)
- "share patch show"/"share patcho" = saare paach sho = 550
- "share tin show"/"sharatin" = saare tin sho = 350
- "share hajar"/"shar hazar" = saare hajar = 1500
- "do is show"/"dui show"/"doish" = duisho = 200
- "shura ka"/"shuruka" = taka
- "bargar"=burger, "raisten"=rice

Return ONLY a JSON array, no other text. Each entry MUST have:
- "name": short description ("lunch", "uber", "groceries", "salary", "gift received")
- "amount": positive integer, no currency symbols, no decimals unless explicit
- "category": one of {CATEGORIES}
- "type": "expense" or "income"
- "context": short lowercase bucket the money belongs to. If the speaker mentions a specific company, project, person, fund, or label, extract it as a short lowercase string (e.g. "masnoonhub", "wedding fund", "office"). Otherwise omit the field — the bot will default it.
- "flow": one of "regular" (default), "loan_taken", "loan_repaid", "loan_given", or "loan_received_back". This is BIDIRECTIONAL — distinguish carefully who borrowed from whom.
   - LOAN_TAKEN — I borrowed; cash came IN; I now owe them. Signals (EN): "borrowed", "took loan", "loan from <X>", "got a loan". Signals (Banglish): "ar dhaar nilam", "loan nilam", "dhaar ane", "loan paisi", "X theke loan nilam"
   - LOAN_REPAID — I paid back what I owed; cash went OUT; my debt shrinks. Signals (EN): "paid back", "loan repayment", "loan emi", "settled my loan", "paid off loan". Signals (Banglish): "loan shod korlam", "dhaar shod korlam", "loan shodh dilam"
   - LOAN_GIVEN — I lent money; cash went OUT; they now owe me. Signals (EN): "lent", "gave loan", "loaned to <X>", "advanced to <X>". Signals (Banglish): "loan dilam", "dhaar dilam", "X ke loan dilam", "ar loan dilam"
   - LOAN_RECEIVED_BACK — Someone paid me back what they owed me; cash came IN; their debt to me shrinks. Signals (EN): "paid me back", "got my loan back", "they returned my loan". Signals (Banglish): "loan ferot pailam", "amake loan ferot dise", "dhaar ferot pailam"
   - Pair the flow with the right type:
       loan_taken → type "income" (cash in)
       loan_repaid → type "expense" (cash out)
       loan_given → type "expense" (cash out — you gave money away)
       loan_received_back → type "income" (cash in — you got money back)
   - "X dilam" is AMBIGUOUS in Banglish — "loan dilam" = I LENT (loan_given); "loan shod dilam" / "shodh dilam" = I PAID BACK (loan_repaid). Use the surrounding context.
- "loan_name": STRONGLY PREFERRED whenever flow is loan_taken or loan_repaid. Extract a short lowercase identifier (one or two words, hyphens not spaces) from:
   * The lender / borrower's first name ("from rahim" → "rahim", "to selim" → "selim")
   * The loan's purpose ("bike loan" → "bike", "home loan" → "home", "office advance" → "office")
   * The project or context the loan is for ("loan for masnoonhub" → "masnoonhub")
   * A relationship word if no name ("bro" → "bro", "abbu" → "abbu", "supplier" → "supplier")
   Only omit loan_name if the speaker says literally nothing identifying the loan party or purpose. When in doubt, pick the most specific noun in the sentence.

Loan examples:

Input: "borrowed 5000 from rahim"
Output: [{{"name":"loan from rahim","amount":5000,"category":"Other","type":"income","flow":"loan_taken","loan_name":"rahim"}}]

Input: "paid back 1000 loan emi"
Output: [{{"name":"loan emi","amount":1000,"category":"Bills","type":"expense","flow":"loan_repaid"}}]

Input: "rahim ke 2000 loan shod korlam"
Output: [{{"name":"loan repayment to rahim","amount":2000,"category":"Bills","type":"expense","flow":"loan_repaid","loan_name":"rahim"}}]

Input: "bike loan emi 5000"
Output: [{{"name":"bike loan emi","amount":5000,"category":"Bills","type":"expense","flow":"loan_repaid","loan_name":"bike"}}]

Input: "dosh hajar loan nilam masnoonhub er jonno"
Output: [{{"name":"loan for masnoonhub","amount":10000,"category":"Other","type":"income","flow":"loan_taken","loan_name":"masnoonhub","context":"masnoonhub"}}]

Input: "lent 1000 to bashar"
Output: [{{"name":"lent to bashar","amount":1000,"category":"Other","type":"expense","flow":"loan_given","loan_name":"bashar"}}]

Input: "bashar ke 2000 loan dilam"
Output: [{{"name":"loan to bashar","amount":2000,"category":"Other","type":"expense","flow":"loan_given","loan_name":"bashar"}}]

Input: "bashar paid me back 1000"
Output: [{{"name":"loan return from bashar","amount":1000,"category":"Other","type":"income","flow":"loan_received_back","loan_name":"bashar"}}]

Input: "rahim theke loan ferot pailam 3000"
Output: [{{"name":"loan return from rahim","amount":3000,"category":"Other","type":"income","flow":"loan_received_back","loan_name":"rahim"}}]

Examples:

Input: "spent 350 on lunch and 280 for uber"
Output: [{{"name":"lunch","amount":350,"category":"Food","type":"expense"}},{{"name":"uber","amount":280,"category":"Transport","type":"expense"}}]

Input: "got 200 today and spent 100 on rickshaw"
Output: [{{"name":"received","amount":200,"category":"Other","type":"income"}},{{"name":"rickshaw","amount":100,"category":"Transport","type":"expense"}}]

Input: "i got dui sho taka today"
Output: [{{"name":"received","amount":200,"category":"Other","type":"income"}}]

Input: "amake two hundred taka dise"
Output: [{{"name":"received","amount":200,"category":"Other","type":"income"}}]

Input: "ami duisho taka gift korechi"
Output: [{{"name":"gift","amount":200,"category":"Other","type":"expense"}}]

Input: "Burger saare tin sho taka"
Output: [{{"name":"Burger","amount":350,"category":"Food","type":"expense"}}]

Input: "salary 30000 paisi"
Output: [{{"name":"salary","amount":30000,"category":"Other","type":"income"}}]

If the input contains no amount you can confidently extract, return []
"""

RECEIPT_PROMPT = (
    "OCR this receipt image. Extract the total amount paid, the merchant name, "
    "and suggest a category. Use the merchant name as 'name'. The 'type' is "
    "'expense' (receipts are always outflows). Return the JSON array shape "
    "described in the system instructions."
)

AUDIO_PROMPT = (
    "Listen to this voice note and extract any money movements as described "
    "in the system instructions. The audio may be English, Bengali, or "
    "Banglish (mixed). Be careful to detect whether each entry is an "
    "expense (money out) or income (money in) based on the verbs used.\n\n"
    "CRITICAL: If the audio is silent, contains no speech, contains speech "
    "but no clear numeric amount, or you cannot confidently understand the "
    "speaker, return an empty array []. Do NOT invent entries based on the "
    "examples in the system instructions — those are reference patterns, "
    "not content to copy. Only emit entries you can hear in THIS audio.\n\n"
    "Return ONLY the JSON array."
)


class ParseError(RuntimeError):
    pass


def _extract_json_array(raw: str) -> list[dict[str, Any]]:
    """Pull a JSON array out of Gemini's response, even if wrapped in code fences."""
    text = raw.strip()
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    # If the model returned an object or noise, find the first JSON array.
    if not text.startswith("["):
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            text = match.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Gemini returned non-JSON: {raw[:300]}") from exc
    if not isinstance(parsed, list):
        raise ParseError(f"Gemini did not return a JSON array: {raw[:300]}")
    return parsed


def configure_context(synonyms: dict[str, list[str]], default: str) -> None:
    """Wire context-tag config into the parser at startup.

    Called once from main.py with values from Settings. After this call,
    _validate() will normalize each entry's `context` field against the
    synonym table, and the system prompt will include a hint block listing
    the canonical names + their accepted variations.
    """
    global _CONTEXT_SYNONYMS, _CONTEXT_DEFAULT, _CONTEXT_BLOCK
    _CONTEXT_SYNONYMS = synonyms or {}
    _CONTEXT_DEFAULT = (default or "personal").strip() or "personal"
    _CONTEXT_BLOCK = _build_context_block(_CONTEXT_SYNONYMS, _CONTEXT_DEFAULT)
    logger.info(
        "Context tags configured: canonicals=%s default=%s",
        list(_CONTEXT_SYNONYMS.keys()) or "(none)",
        _CONTEXT_DEFAULT,
    )


def _build_context_block(synonyms: dict[str, list[str]], default: str) -> str:
    """System-prompt addendum that teaches Gemini the canonical context list."""
    if not synonyms:
        return ""
    lines = [
        "CONTEXT NORMALIZATION:",
        "If the speaker mentions any of these terms, use the canonical name "
        "as the entry's `context` field. The bot will collapse synonyms to "
        "the canonical anyway, so don't overthink it — just be specific.",
    ]
    for canon, syns in synonyms.items():
        lines.append(f'  - "{canon}" ← if you hear any of: {", ".join(syns)}')
    lines.append(f'If no specific context is mentioned, default to "{default}".')
    return "\n".join(lines)


def _normalize_context(raw: Any, synonyms: dict[str, list[str]], default: str) -> str:
    """Map a free-form `context` value to one of the canonical names."""
    if raw is None:
        return default
    needle = str(raw).strip().lower()
    if not needle:
        return default
    # Exact canonical match first
    for canon in synonyms:
        if canon.lower() == needle:
            return canon
    # Synonym exact match
    for canon, syns in synonyms.items():
        if needle in syns:
            return canon
    # Loose: needle contains a known synonym (helps when Gemini emits
    # "masnoonhub express" with a space, or qualifies with "for")
    for canon, syns in synonyms.items():
        for syn in syns:
            if len(syn) >= 4 and syn in needle:
                return canon
    return default


def _validate(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        category = str(entry.get("category") or "Other").strip()
        amount_raw = entry.get("amount")
        kind = str(entry.get("type") or "expense").strip().lower()
        if kind not in ("expense", "income"):
            kind = "expense"
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            continue
        if amount <= 0 or not name:
            continue
        if category not in CATEGORIES:
            category = "Other"
        context = _normalize_context(
            entry.get("context"), _CONTEXT_SYNONYMS, _CONTEXT_DEFAULT
        )
        flow_raw = str(entry.get("flow") or "regular").strip().lower()
        if flow_raw not in (
            "regular", "loan_taken", "loan_repaid", "loan_given", "loan_received_back"
        ):
            flow_raw = "regular"
        # Coerce type to match flow — direction is non-negotiable so the
        # tracker stays internally consistent even if Gemini emits a
        # mismatched (flow, type) pair.
        if flow_raw in ("loan_taken", "loan_received_back"):
            kind = "income"  # cash IN
        elif flow_raw in ("loan_repaid", "loan_given"):
            kind = "expense"  # cash OUT
        # Loan name — only meaningful when flow != regular. Normalize to
        # lowercase ascii-ish slug so two phrasings of the same name
        # ("Rahim", "rahim ") collapse to one bucket.
        loan_name = _normalize_loan_name(entry.get("loan_name")) if flow_raw != "regular" else ""
        cleaned.append(
            {
                "name": name,
                "amount": amount,
                "category": category,
                "type": kind,
                "context": context,
                "flow": flow_raw,
                "loan_name": loan_name,
            }
        )
    return cleaned


_LOAN_NAME_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def _normalize_loan_name(raw: Any) -> str:
    """Slugify Gemini's loan_name field. Empty string if unusable."""
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    # Whitespace → single hyphen so multi-word names collapse cleanly.
    s = "-".join(s.split())
    # Drop anything outside the keep set so the value is safe to embed
    # in a tag and to compare across entries.
    s = "".join(c for c in s if c in _LOAN_NAME_KEEP)
    s = s.strip("-")
    return s


# Retry transient errors so a single 503/429 doesn't force the user to re-send.
# Permanent errors (400 bad request, 401 bad key) fail fast — no point retrying.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_BACKOFFS_S = (1.5, 4.0)  # two retries → max 3 total attempts


async def _call_gemini(parts: list[dict[str, Any]], *, api_key: str) -> str:
    """Call Gemini, cascading through MODELS and retrying transient failures.

    For each model in turn:
      - try up to (1 + len(RETRY_BACKOFFS_S)) times
      - on a transient status (429/5xx) or network error, retry with backoff
      - on a permanent status (400/401/403), fail fast — switching models won't help
      - on a transient failure that exhausts retries, move to the next model
    Surfaces ParseError only when every model+retry combination has failed.
    """
    import asyncio  # local import keeps top of file untouched

    prompt = SYSTEM_PROMPT
    if _CONTEXT_BLOCK:
        prompt = prompt + "\n\n" + _CONTEXT_BLOCK
    body = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    params = {"key": api_key}

    last_status: int | None = None
    last_body: str = ""

    for model in MODELS:
        url = GEMINI_URL_TEMPLATE.format(model=model)
        for attempt, backoff in enumerate([0.0, *RETRY_BACKOFFS_S]):
            if backoff:
                logger.info("Gemini %s retry in %.1fs (last=%s)", model, backoff, last_status)
                await asyncio.sleep(backoff)
            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    response = await client.post(url, params=params, json=body)
            except httpx.HTTPError as exc:
                logger.warning("Gemini %s transport error on attempt %d: %s", model, attempt + 1, exc)
                last_status = None
                last_body = str(exc)
                continue

            if response.status_code == 200:
                if model != MODELS[0]:
                    logger.info("Gemini served by fallback model %s", model)
                data = response.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    feedback = data.get("promptFeedback") or {}
                    raise ParseError(f"Gemini returned no candidates. Feedback: {feedback}")
                parts_out = candidates[0].get("content", {}).get("parts", [])
                text_chunks = [p.get("text", "") for p in parts_out if "text" in p]
                text = "".join(text_chunks).strip()
                if not text:
                    raise ParseError("Gemini returned an empty response.")
                logger.info("Gemini raw: %s", text)
                return text

            last_status = response.status_code
            last_body = response.text

            if response.status_code in RETRYABLE_STATUS:
                logger.warning(
                    "Gemini %s returned %s on attempt %d", model, last_status, attempt + 1
                )
                continue

            # 4xx (other than 429) is a permanent error — don't try other models.
            logger.error("Gemini %s error %s: %s", model, response.status_code, response.text)
            raise ParseError(
                f"Gemini returned {response.status_code}: {response.text[:300]}"
            )
        logger.warning("Gemini model %s exhausted retries, trying next", model)

    raise ParseError(
        f"Every Gemini model failed (last status {last_status}): {last_body[:300]}"
    )


async def parse_text(message: str, *, api_key: str) -> list[dict[str, Any]]:
    """Parse an expense (or many) from a text/transcript string."""
    raw = await _call_gemini([{"text": message}], api_key=api_key)
    return _validate(_extract_json_array(raw))


async def parse_image(image_bytes: bytes, mime_type: str, *, api_key: str) -> list[dict[str, Any]]:
    """Parse expenses from a receipt photo."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    parts = [
        {"inline_data": {"mime_type": mime_type, "data": encoded}},
        {"text": RECEIPT_PROMPT},
    ]
    raw = await _call_gemini(parts, api_key=api_key)
    return _validate(_extract_json_array(raw))


async def parse_audio(
    audio_bytes: bytes, mime_type: str, *, api_key: str
) -> list[dict[str, Any]]:
    """Parse expenses (and income) directly from a voice note.

    Skips Whisper — Gemini's multimodal models accept audio inline. One API
    call replaces the Whisper+Gemini-text pair, and Gemini reasons over the
    raw audio (better for code-switching and ambiguous Banglish).
    """
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    parts = [
        {"inline_data": {"mime_type": mime_type, "data": encoded}},
        {"text": AUDIO_PROMPT},
    ]
    raw = await _call_gemini(parts, api_key=api_key)
    return _validate(_extract_json_array(raw))

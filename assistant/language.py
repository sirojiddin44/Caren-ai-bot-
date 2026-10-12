"""Cheap language hint (uz / ru / en) for the prompt. Claude makes the final call."""

from __future__ import annotations

import re
from typing import Optional

APOSTROPHES = "'ʻʼ’‘`"

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_UZ_CYRILLIC_LETTERS_RE = re.compile(r"[ўқғҳЎҚҒҲ]")
_CYRILLIC_WORD_RE = re.compile(r"[\u0400-\u04FF]+")
_LATIN_WORD_RE = re.compile(rf"[A-Za-z{APOSTROPHES}]+")
_APOSTROPHE_RE = re.compile(rf"[{APOSTROPHES}]")
# o' / g' followed by a letter, but not an English possessive like "dog's".
_UZ_OG_RE = re.compile(r"[og]'(?!s$)[a-z]")

# Uzbek stems written in Cyrillic, matched as word prefixes ("яхшимисиз" -> "яхши").
# No common Russian word starts with any of them.
UZ_CYRILLIC_STEMS = (
    "ассалому", "салом", "рахмат", "раҳмат", "яхши", "нима", "қалесан", "калесан",
    "йўқ", "бугун", "эртага", "керак", "бопти", "зўр", "хозир", "ҳозир", "кейин",
    "қаерда", "каерда", "қалайсиз", "калайсиз",
)
# Short Uzbek words that are prefixes of Russian words ("мен" -> "менеджер"): exact match only.
UZ_CYRILLIC_WORDS = {"мен", "сен", "сиз", "биз", "хоп", "йук", "ака", "опа"}

UZ_LATIN_WORDS = {
    "assalomu", "alaykum", "salom", "rahmat", "raxmat", "yaxshi", "yaxshimisiz", "nima",
    "nimaga", "qalesan", "qalaysan", "qalaysiz", "qayerda", "qayerdasan", "bugun", "ertaga",
    "kerak", "xop", "xo'p", "bopti", "zor", "hozir", "keyin", "men", "sen", "siz", "biz",
    "yoq", "yo'q", "opa", "uka", "qachon", "nega", "qanday", "mayli",
    "lekin", "yana", "uyda", "ishda", "qilyapman", "boraman", "kelaman",
}

UZ_LATIN_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz", "asizmi", "ganman", "dingmi",
    "sanmi", "sizmi",
)


def _normalize_apostrophes(text: str) -> str:
    return _APOSTROPHE_RE.sub("'", text)


def detect_language(text: str) -> Optional[str]:
    """Return "uz", "ru", "en", or None when the text has no letters (emoji, numbers)."""
    if not text:
        return None
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cyrillic == 0 and latin == 0:
        return None

    if cyrillic > latin:
        if _UZ_CYRILLIC_LETTERS_RE.search(text):
            return "uz"
        words = _CYRILLIC_WORD_RE.findall(text.lower())
        if any(word in UZ_CYRILLIC_WORDS or word.startswith(UZ_CYRILLIC_STEMS) for word in words):
            return "uz"
        return "ru"

    lowered = _normalize_apostrophes(text.lower())
    tokens = _LATIN_WORD_RE.findall(lowered)
    for token in tokens:
        token = token.strip("'")
        if token != "o'clock" and _UZ_OG_RE.search(token):
            return "uz"
    for token in tokens:
        word = token.strip("'")
        if word in UZ_LATIN_WORDS:
            return "uz"
        bare = word.replace("'", "")
        if bare in UZ_LATIN_WORDS:
            return "uz"
        if any(bare.endswith(suffix) and len(bare) > len(suffix) for suffix in UZ_LATIN_SUFFIXES):
            return "uz"
    return "en"

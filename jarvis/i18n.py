"""Lingua dell'interfaccia: messaggi, vocabolario dei comandi Telegram e prompt dei modelli.

Un locale è una cartella `locales/<codice>/` con `messages.yaml` (testi, chiave -> stringa con segnaposto
`{nome}`) e `vocab.yaml` (parole dei comandi), più `prompts/<codice>/*.md` per i prompt. Il predefinito è `en`;
`owner.locale` in config/projects.yaml sceglie il locale del processo (`config.load` lo imposta).

I testi sono sostituiti con `str.format` solo se la chiamata passa dei parametri: una stringa senza parametri
va usata com'è e può contenere graffe. Se manca una chiave nel locale scelto si ripiega sull'inglese.
"""

import re
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT = "en"
ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"
PROMPTS = ROOT / "prompts"

_current = DEFAULT


class LocaleError(ValueError):
    pass


def available() -> list[str]:
    """Codici dei locali installati (cartelle con messages.yaml), in ordine alfabetico."""
    return sorted(p.parent.name for p in LOCALES.glob("*/messages.yaml"))


def check(code: str) -> str:
    if code not in available():
        raise LocaleError(f"locale sconosciuto: {code} (disponibili: {', '.join(available())})")
    return code


def set_locale(code: str) -> None:
    global _current
    _current = check(code)


def get_locale() -> str:
    return _current


@contextmanager
def using(code: str):
    """Locale temporaneo (test, runner): ripristina il precedente all'uscita."""
    previous = _current
    set_locale(code)
    try:
        yield
    finally:
        set_locale(previous)


@lru_cache(maxsize=None)
def _load(code: str, name: str) -> dict:
    data = yaml.safe_load((LOCALES / code / f"{name}.yaml").read_text(encoding="utf-8")) or {}
    return data


def messages(code: str) -> dict:
    return _load(code, "messages")


def vocabulary(code: str | None = None) -> dict:
    return _load(code or _current, "vocab")


def t(key: str, /, **params) -> str:
    """Testo del locale corrente (o dell'inglese se manca); con `params` sostituisce i segnaposto."""
    text = messages(_current).get(key)
    if text is None:
        text = messages(DEFAULT)[key]
    return text.format(**params) if params else text


def words(key: str) -> list[str]:
    """Elenco di parole del vocabolario dei comandi del locale corrente."""
    return list(vocabulary()[key])


def alternation(key: str) -> str:
    """Le parole di `key` come alternativa di regex: `vai|lancia|parti`."""
    return "|".join(re.escape(w) for w in words(key))


def every(key: str) -> tuple[str, ...]:
    """Il testo di `key` in tutti i locali installati (per riconoscere messaggi scritti in qualunque lingua)."""
    return tuple(dict.fromkeys(messages(code)[key] for code in available() if key in messages(code)))


def prompt(name: str, code: str | None = None) -> str:
    """Testo grezzo di `prompts/<locale>/<name>.md`."""
    return (PROMPTS / (code or _current) / f"{name}.md").read_text(encoding="utf-8")

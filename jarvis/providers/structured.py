"""Risposta JSON strutturata da qualunque provider: schema nativo se la CLI lo ha, altrimenti nel prompt."""

import json
import time

from jarvis.i18n import t
from jarvis.providers.base import Provider, ProviderError, run_structured
from jarvis.providers.schema import validate

MIN_RETRY_SECONDS = 20  # sotto questo budget residuo il retry non parte
# Il testo da aggiungere al messaggio (schema nel prompt, richiesta di correggere) sta nel catalogo del locale:
# `prov.schema_prompt` ({schema}) e `prov.retry_prompt` ({errors}).


def call_structured(provider: Provider, system: str, message: str, schema: dict, model: str, timeout: int) -> dict:
    """Con la capability `plan_structured` lo schema lo applica la CLI (la risposta viene comunque validata, senza
    retry). Senza, va nel prompt: se la risposta non è conforme, la si richiede una volta con l'elenco degli errori;
    poi errore chiaro. `timeout` è il budget totale, retry compreso."""
    if "plan_structured" in provider.capabilities:
        output = run_structured(provider, system, message, schema, model, timeout)
        if errors := validate(output, schema):
            raise ProviderError(t("prov.not_conforming", name=provider.name, retry="", errors="; ".join(errors[:5])))
        return output
    deadline = time.monotonic() + timeout
    base = message + t("prov.schema_prompt", schema=json.dumps(schema, ensure_ascii=False))
    prompt, errors, tried = base, [], 0
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if attempt and remaining < MIN_RETRY_SECONDS:
            break
        tried += 1
        output = run_structured(provider, system, prompt, schema, model, max(1, int(remaining)))  # ProviderError: non si riprova
        errors = validate(output, schema)
        if not errors:
            return output
        prompt = base + t("prov.retry_prompt", errors="\n".join(f"- {e}" for e in errors[:8]))
    raise ProviderError(t("prov.not_conforming", name=provider.name, retry=t("prov.after_retry") if tried > 1 else "",
                          errors="; ".join(errors[:5])))

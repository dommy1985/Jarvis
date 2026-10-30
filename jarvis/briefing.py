"""Briefing su Telegram: testo del messaggio e risposte dell'owner.

Risposte riconosciute (più comandi anche sulla stessa riga: "1 sonnet 2 io ok"):
  ok              conferma le voci proposte
  2 haiku         cambia l'esecutore della voce 2 (nomi o alias da projects.yaml)
  2 io            la voce 2 la fa l'owner
  2 salta         toglie la voce 2: al suo posto arriva la prima riserva del planner e il task non viene
                  riproposto per planner.skip_snooze_days giorni
  2 mai           come salta, ma il task non viene più proposto (finché non lo aggiungi a mano)
  ore 4           rigenera il briefing con 4 ore disponibili (da solo; tiene il focus di oggi, se c'è)
  ore 3 su other-app e my-app
                  rigenera concentrato su quei progetti (una volta tanto: domani si torna al normale);
                  le ore vanno anche in coda: "briefing su other-app ore 3"
  briefing su other-app
                  come sopra, con le ore di oggi; "briefing su tutti" torna a tutti i progetti
  <link> agy      aggiunge al briefing un task importato (link GitHub/Jira, PROJ-412,
                  my-app#936) con quell'esecutore; senza esecutore usa il routing
  salta tutto     toglie tutte le voci proposte (es. "salta tutto, <link> agy, ok")
Se una parte non è riconosciuta non viene applicato niente.
"""

import functools
import html
import json
import re
import sqlite3
from datetime import date as Date
from datetime import datetime, timedelta, timezone

from jarvis.config import Config
from jarvis.i18n import alternation, get_locale, t, words

MAX_TEXT = 4000  # limite Telegram 4096
MAX_RATIONALE = 220
# Le parole dei comandi (conferma, salta, "io", filler, ...) stanno nel vocabolario del locale: locales/<codice>/vocab.yaml.
REFS = [  # (pattern sul testo minuscolo, funzione -> riferimento normalizzato)
    (re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)\S*"),
     lambda m: ("github", f"{m.group(1)}#{m.group(2)}")),
    (re.compile(r"https?://\S*?(?:/browse/|selectedissue=)([a-z][a-z0-9]+-\d+)\S*"),
     lambda m: ("jira", m.group(1).upper())),
    (re.compile(r"(?<![\w/#-])([\w.-]+(?:/[\w.-]+)?)#(\d+)\b"), lambda m: ("github", f"{m.group(1)}#{m.group(2)}")),
    (re.compile(r"(?<![\w/-])([a-z][a-z0-9]+-\d+)\b"), lambda m: ("jira", m.group(1).upper())),
]
NUMBER = r"\d+(?:[.,]\d+)?"


@functools.lru_cache(maxsize=None)
def _patterns(locale: str) -> dict:
    """Regex dei comandi del briefing per un locale (le parole vengono da vocab.yaml)."""
    hours, on = alternation("hours_word"), alternation("focus_preps")
    return {
        "FOCUS": re.compile(rf"(?:{alternation('briefing_word')}|(?:{hours})\s+({NUMBER}))\s+(?:{on})\s+(.+)"),
        # "... su X ore 5" / "5 ore"
        "FOCUS_HOURS": re.compile(rf"(?:^|[\s,;])(?:(?:{hours})\s+({NUMBER})|({NUMBER})\s*(?:{hours}))\s*$"),
        "HOURS": re.compile(rf"(?:{hours})\s+({NUMBER})"),
        "FOCUS_SPLIT": re.compile(rf"\s*[,;+&]\s*|\s+(?:{alternation('and_word')})\s+"),
    }


def _rx(name: str) -> re.Pattern:
    return _patterns(get_locale())[name]


def help_text() -> str:
    """"Comandi: ok · 2 haiku · ..." sotto il briefing non ancora confermato."""
    return t("briefing.help_line", commands=t("briefing.help_commands"))


class ReplyError(ValueError):
    pass


def aliases(cfg: Config) -> dict[str, str]:
    """Nomi accettati per ogni esecutore: chiave, chiave senza 'claude-', cli e prefisso modello se univoci."""
    executors = cfg.raw["executors"]
    result = {name: name for name in executors}
    for name, e in executors.items():
        if e.get("human"):
            result.update({h: name for h in words("human")})
            continue
        if name.startswith("claude-"):
            result.setdefault(name.removeprefix("claude-"), name)
        for alias in (e["cli"], e["model"].split("-")[0]):
            owners = [n for n, x in executors.items() if not x.get("human") and alias in (x["cli"], x["model"].split("-")[0])]
            if len(owners) == 1:
                result.setdefault(alias, name)
    return result


def _parts(text: str) -> list[str]:
    """Separa i comandi: virgole e a capo, ma anche sulla stessa riga ("1 sonnet 2 io ok")."""
    parts = []
    for chunk in re.split(r"[,;\n]+", text.strip().lower()):
        chunk = " ".join(chunk.split()).rstrip(".!")
        confirm = None
        for word in sorted(words("confirm"), key=len, reverse=True):  # "... ok" in coda
            if chunk != word and chunk.endswith(" " + word):
                chunk, confirm = chunk[: -len(word) - 1], word
                break
        # Un nuovo comando comincia a ogni "<numero> " che non è il primo token.
        parts += [p for p in re.split(r"\s+(?=\d+\s)", chunk) if p]
        if confirm:
            parts.append(confirm)
    return parts


def added_task(part: str, names: dict[str, str]) -> dict | None:
    """"<link o id> [esecutore]" (anche "fai <link> con agy") -> comando add, None se non c'è un riferimento."""
    for pattern, normalize in REFS:
        if m := pattern.search(part):
            source, ref = normalize(m)
            filler = set(words("filler"))
            rest = " ".join(w for w in (part[: m.start()] + " " + part[m.end():]).split() if w not in filler)
            executor = None
            if rest:
                executor = names.get(rest) or names.get(rest.replace(" ", "-"))
                if executor is None:
                    raise ReplyError(t("err.unknown_executor", name=rest))
            return {"cmd": "add", "source": source, "ref": ref, "executor": executor}
    return None


def find_task(conn: sqlite3.Connection, source: str, ref: str) -> sqlite3.Row:
    """Task importato a partire da un riferimento: owner/repo#n, repo#n (se univoco) o chiave Jira."""
    if source == "jira":
        rows = conn.execute("SELECT * FROM tasks WHERE source = 'jira' AND upper(external_id) = ?", (ref,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE source = 'github' AND (lower(external_id) = ? OR lower(external_id) LIKE ?)",
            (ref, "%/" + ref),
        ).fetchall()
    if len(rows) > 1:
        raise ReplyError(t("briefing.err.ambiguous_ref", ref=ref))
    if not rows:
        raise ReplyError(t("briefing.err.task_not_found", ref=ref))
    if not rows[0]["is_open"]:
        raise ReplyError(t("briefing.err.task_closed", ref=rows[0]["external_id"]))
    return rows[0]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve_projects(text: str, cfg: Config) -> list[str]:
    """"other-app e my-app" -> chiavi di projects.yaml. Accetta la chiave, il nome del repo o la chiave Jira
    (anche senza trattini o maiuscole) e un prefisso se univoco. [] per "tutti". Solleva ReplyError."""
    names = [w for w in _rx("FOCUS_SPLIT").split(" ".join(text.lower().split())) if w]
    if not names:
        raise ReplyError(t("briefing.err.pick_projects"))
    if any(w in words("focus_all") for w in names):
        return []
    labels: dict[str, set[str]] = {}
    for key, project in cfg.projects.items():
        sources = project.get("sources") or {}
        for label in [key, *(r.split("/")[-1] for r in sources.get("github") or []), *(sources.get("jira") or [])]:
            labels.setdefault(_norm(label), set()).add(key)
    chosen: list[str] = []
    for word in names:
        norm = _norm(word)
        found = labels.get(norm) or {k for label, keys in labels.items() if norm and label.startswith(norm)
                                     for k in keys}
        if not found:
            raise ReplyError(t("briefing.err.unknown_project", word=html.escape(word, quote=False),
                               available=", ".join(sorted(k for k, p in cfg.projects.items() if not p.get("paused")))))
        if len(found) > 1:
            raise ReplyError(t("briefing.err.ambiguous_project", word=html.escape(word, quote=False),
                               found=", ".join(sorted(found))))
        (key,) = found
        if cfg.projects[key].get("paused"):
            raise ReplyError(t("briefing.err.paused_project", key=key))
        if key not in chosen:
            chosen.append(key)
    return chosen


def parse(text: str, cfg: Config) -> list[dict]:
    """Testo della risposta -> lista di comandi. Solleva ReplyError se una parte non è riconosciuta."""
    names = aliases(cfg)
    if focus := _rx("FOCUS").fullmatch(" ".join(text.lower().split()).rstrip(".!")):  # prima di _parts: le virgole contano
        raw_hours, projects = focus.group(1), focus.group(2)
        if tail := _rx("FOCUS_HOURS").search(projects):  # le ore anche dopo i progetti: "briefing su my-app ore 5"
            if raw_hours:
                raise ReplyError(t("briefing.err.hours_twice"))
            raw_hours, projects = tail.group(1) or tail.group(2), projects[:tail.start()]
        hours = float(raw_hours.replace(",", ".")) if raw_hours else None
        if hours is not None and not 0 < hours <= 16:
            raise ReplyError(t("briefing.err.hours_out_of_range", value=raw_hours))
        return [{"cmd": "hours", "hours": hours, "focus": resolve_projects(projects, cfg), "set_focus": True}]
    commands = []
    for part in _parts(text):
        if part in words("confirm") or part.rstrip("!") in words("confirm"):
            commands.append({"cmd": "confirm"})
            continue
        if part in words("skip_all"):
            commands.append({"cmd": "skip_all"})
            continue
        if added := added_task(part, names):
            commands.append(added)
            continue
        if m := _rx("HOURS").fullmatch(part):
            hours = float(m.group(1).replace(",", "."))
            if not 0 < hours <= 16:
                raise ReplyError(t("briefing.err.hours_out_of_range", value=m.group(1)))
            commands.append({"cmd": "hours", "hours": hours})
            continue
        if m := re.fullmatch(r"(\d+)\s+(.+)", part):
            position, rest = int(m.group(1)), m.group(2)
            if rest in words("skip"):
                commands.append({"cmd": "skip", "position": position})
                continue
            if rest in words("never"):
                commands.append({"cmd": "never", "position": position})
                continue
            executor = names.get(rest) or names.get(rest.replace(" ", "-"))
            if executor:
                commands.append({"cmd": "executor", "position": position, "executor": executor})
                continue
            raise ReplyError(t("err.unknown_executor", name=rest))
        raise ReplyError(t("briefing.err.not_understood", part=part))
    if not commands:
        raise ReplyError(t("briefing.err.empty"))
    if any(c["cmd"] == "hours" for c in commands) and len(commands) > 1:
        raise ReplyError(t("briefing.err.hours_alone"))
    return commands


def apply(conn: sqlite3.Connection, briefing_id: int, commands: list[dict], cfg: Config) -> list[str]:
    """Applica salti, override, aggiunte e conferma alle voci. Restituisce le righe di riepilogo."""
    b = conn.execute("SELECT status, date FROM briefings WHERE id = ?", (briefing_id,)).fetchone()
    positions = {
        r["position"]: r
        for r in conn.execute("SELECT * FROM briefing_items WHERE briefing_id = ?", (briefing_id,)).fetchall()
    }
    for c in commands:
        if "position" in c and c["position"] not in positions:
            raise ReplyError(t("err.no_item", position=c["position"]))

    confirmed = b["status"] == "confirmed" or any(c["cmd"] == "confirm" for c in commands)
    done, replacements = [], []
    if any(c["cmd"] == "skip_all" for c in commands):  # prima di tutto il resto
        conn.execute("UPDATE briefing_items SET status = 'skipped' WHERE briefing_id = ?", (briefing_id,))
        done.append(t("briefing.all_skipped"))
    for c in commands:  # poi override, salti e aggiunte, infine la conferma
        if c["cmd"] == "add":
            task = find_task(conn, c["source"], c["ref"])
            executor = c["executor"] or cfg.executor_for(
                task["project"], task["type"],
                cfg.raw["routing"].get(task["type"]) or cfg.raw["routing"]["feature"])[0]
            status = "approved" if confirmed else "proposed"
            existing = conn.execute(
                "SELECT position FROM briefing_items WHERE briefing_id = ? AND task_id = ?", (briefing_id, task["id"])
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE briefing_items SET final_executor = ?, decision_source = 'telegram', status = ?
                       WHERE briefing_id = ? AND task_id = ?""",
                    (executor, status, briefing_id, task["id"]),
                )
                position = existing["position"]
            else:
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM briefing_items WHERE briefing_id = ?", (briefing_id,)
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO briefing_items (briefing_id, task_id, position, proposed_type, proposed_executor,
                           final_executor, rationale, decision_source, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'telegram', ?)""",
                    (briefing_id, task["id"], position, task["type"], executor, executor,
                     t("briefing.added_by", owner=cfg.owner_name), status),
                )
            # Aggiunto a mano: l'owner lo rivuole, salti e esclusioni passati non contano più.
            conn.execute("UPDATE tasks SET snoozed_until = NULL, skip_count = 0, planner_excluded = 0 WHERE id = ?",
                         (task["id"],))
            done.append(f"{position} {short_ref(task['external_id'])} → {executor}")
        elif c["cmd"] == "executor":
            conn.execute(
                """UPDATE briefing_items SET final_executor = ?, decision_source = 'telegram', status = ?
                   WHERE briefing_id = ? AND position = ?""",
                (c["executor"], "approved" if confirmed else "proposed", briefing_id, c["position"]),
            )
            done.append(f"{c['position']} → {c['executor']}")
        elif c["cmd"] in ("skip", "never"):
            done.extend(_skip(conn, cfg, briefing_id, b["date"], c["position"], c["cmd"] == "never", replacements))
    if any(c["cmd"] == "confirm" for c in commands):
        # Una riserva arrivata in questo stesso messaggio l'owner non l'ha ancora vista: resta da confermare.
        unseen = f" AND id NOT IN ({','.join('?' * len(replacements))})" if replacements else ""
        conn.execute(
            f"""UPDATE briefing_items SET final_executor = COALESCE(final_executor, proposed_executor), status = 'approved'
               WHERE briefing_id = ? AND status = 'proposed'{unseen}""",
            (briefing_id, *replacements),
        )
        conn.execute(
            "UPDATE briefings SET status = 'confirmed', confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ?",
            (_now(), briefing_id),
        )
        done.append(t("briefing.confirmed"))
    return done


def _skip(conn: sqlite3.Connection, cfg: Config, briefing_id: int, day: str, position: int, never: bool,
          replacements: list[int]) -> list[str]:
    """Salta una voce. Se era ancora da fare (proposta o approvata) il task viene ricordato (snooze, o escluso con
    "mai") e la prima riserva libera del planner ne prende il posto, da confermare. Rileggere lo stato qui rende
    innocuo un secondo "N salta" nello stesso messaggio o in un retry."""
    row = conn.execute(
        """SELECT i.status, i.task_id, t.external_id, t.source, t.skip_count FROM briefing_items i
           JOIN tasks t ON t.id = i.task_id WHERE i.briefing_id = ? AND i.position = ?""",
        (briefing_id, position),
    ).fetchone()
    conn.execute("UPDATE briefing_items SET status = 'skipped' WHERE briefing_id = ? AND position = ?",
                 (briefing_id, position))
    line = t("briefing.item_never" if never else "briefing.item_skipped", position=position)
    if row["status"] not in ("proposed", "approved"):
        if never:
            conn.execute("UPDATE tasks SET planner_excluded = 1 WHERE id = ?", (row["task_id"],))
        return [line]

    count = row["skip_count"] + 1
    until = (Date.fromisoformat(day) + timedelta(days=int(cfg.planner.get("skip_snooze_days", 7)))).isoformat()
    conn.execute("UPDATE tasks SET skip_count = ?, snoozed_until = ?, planner_excluded = MAX(planner_excluded, ?) "
                 "WHERE id = ?", (count, until, int(never), row["task_id"]))
    reserve = conn.execute(
        """SELECT r.*, t.external_id FROM briefing_reserves r JOIN tasks t ON t.id = r.task_id
           WHERE r.briefing_id = ? AND r.used = 0 AND t.is_open = 1 AND t.planner_excluded = 0
             AND (t.snoozed_until IS NULL OR t.snoozed_until <= ?)
             AND r.task_id NOT IN (SELECT task_id FROM briefing_items WHERE briefing_id = ?)
           ORDER BY r.rank LIMIT 1""",
        (briefing_id, day, briefing_id),
    ).fetchone()
    if reserve is None:
        line += t("briefing.no_reserve")
    else:
        new = conn.execute("SELECT MAX(position) + 1 FROM briefing_items WHERE briefing_id = ?",
                           (briefing_id,)).fetchone()[0]
        replacements.append(conn.execute(
            """INSERT INTO briefing_items (briefing_id, task_id, position, proposed_type, proposed_executor,
                   estimate_hours, rationale, decision_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (briefing_id, reserve["task_id"], new, reserve["proposed_type"], reserve["proposed_executor"],
             reserve["estimate_hours"], reserve["rationale"], reserve["decision_source"]),
        ).lastrowid)
        conn.execute("UPDATE briefing_reserves SET used = 1 WHERE id = ?", (reserve["id"],))
        line += t("briefing.replaced_by", position=new, ref=short_ref(reserve["external_id"]),
                  executor=reserve["proposed_executor"])
    lines = [line]
    if not never and count >= int(cfg.planner.get("skip_ask_after", 3)):
        ref = short_ref(row["external_id"])
        lines.append(t("briefing.skip_ask", ref=ref, count=count, position=position)
                     + (t("briefing.skip_ask_close", ref=row["external_id"]) if row["source"] == "github" else ""))
    return lines


def record_reply(conn: sqlite3.Connection, briefing_id: int, message_id: int, text: str, parsed) -> bool:
    """Salva la risposta grezza. False se il messaggio era già stato ricevuto (retry del webhook)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO briefing_replies (briefing_id, telegram_message_id, text, parsed, received_at)
           VALUES (?, ?, ?, ?, ?)""",
        (briefing_id, message_id, text, json.dumps(parsed) if parsed is not None else None, _now()),
    )
    return cur.rowcount == 1


def mark_sent(conn: sqlite3.Connection, date: str, message_id: int) -> bool:
    cur = conn.execute(
        """UPDATE briefings SET telegram_message_id = ?, sent_at = ?,
               status = CASE WHEN status = 'draft' THEN 'sent' ELSE status END
           WHERE date = ? AND status IN ('draft', 'sent', 'confirmed')""",
        (message_id, _now(), date),
    )
    return cur.rowcount == 1


def short_ref(external_id: str) -> str:
    """acme/my-app#936 -> my-app#936; PROJ-412 resta com'è."""
    return external_id.split("/", 1)[1] if "/" in external_id else external_id


def _hours(value) -> str:
    return t("briefing.hours", value=value) if value is not None else "?"


def render(b: dict, notes: list[str] = ()) -> str:
    """Testo HTML del briefing (parse_mode HTML). `notes`: avvisi in HTML, mostrati sotto il titolo."""
    d = Date.fromisoformat(b["date"])
    lines = [
        t("briefing.title", weekday=words("weekdays")[d.weekday()], date=d.strftime(t("fmt.day_month")),
          hours=_hours(b["available_hours"])),
    ]
    if b.get("focus"):
        lines[0] += " · 🎯 " + html.escape(", ".join(b["focus"]), quote=False)
    if b["status"] == "confirmed":
        lines[0] += t("briefing.confirmed_suffix")
    lines += [f"⚠️ {n}" for n in notes]
    if b.get("summary"):
        lines += ["", html.escape(b["summary"], quote=False)]
    total = 0.0
    for i in b["items"]:
        executor = i["final_executor"] or i["proposed_executor"]
        tag = " ✏️" if i["decision_source"] == "telegram" else (" 📌" if i["decision_source"] == "project_rule" else "")
        ref = f'<a href="{html.escape(i["url"], quote=True)}">{html.escape(short_ref(i["external_id"]))}</a>'
        head = f"<b>{i['position']}.</b> {ref} {html.escape(i['title'], quote=False)}"
        detail = (f"    {t('briefing.human') if executor == 'me' else executor}{tag} · {i['proposed_type'] or '-'}"
                  f" · {_hours(i['estimate_hours'])}")
        if i["status"] == "skipped":
            head, detail = f"<s>{head}</s>", t("briefing.skipped_detail")
        else:
            total += i["estimate_hours"] or 0
        lines += ["", head, detail]
        if i["rationale"] and i["status"] != "skipped":
            rationale = i["rationale"] if len(i["rationale"]) <= MAX_RATIONALE else i["rationale"][:MAX_RATIONALE] + "…"
            lines.append(f"    <i>{html.escape(rationale, quote=False)}</i>")
    lines += ["", t("briefing.total", total=_hours(total), available=_hours(b["available_hours"]))]
    if b["status"] != "confirmed":
        lines.append(help_text())
    text = "\n".join(lines)
    if len(text) > MAX_TEXT:  # niente tag aperti: taglia sull'ultima voce intera
        text = text[: text.rfind("\n\n<b>", 0, MAX_TEXT - 40)] + "\n\n" + t("briefing.too_long")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

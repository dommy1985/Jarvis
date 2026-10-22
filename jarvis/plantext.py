"""Il piano scritto da un modello (markdown, spesso con link a file) in HTML leggibile su Telegram."""

import html
import re

LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
CODE = re.compile(r"`([^`\n]+)`")
# Percorsi assoluti del worktree del run (con o senza file://): niente di utile per chi legge. Il lookbehind impedisce
# di partire dentro un URL (https://host/...), un percorso relativo o `~/...`.
START = r"(?<![\w:./~-])(?:file://)?"
BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
BULLET = re.compile(r"^([ \t]*)[-*•][ \t]+", re.MULTILINE)
RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
TOKEN = re.compile("\x00(\\d+)\x00")
QUESTION_SECTION = re.compile(
    r"(?:^|\n)[ \t]*(?:#{1,6}\s+|\d+\.\s+)?\**\s*(?:rischi\s+e\s+domande|domande|risks\s+and\s+questions|questions)\b[^\n]*\n([\s\S]*?)(?=\n[ \t]*(?:#{1,6}\s+|\d+\.\s+)\**[^\n]+\**|\Z)",
    re.I,
)


def extract_plan_questions(text: str) -> list[str]:
    """Estrae le domande o decisioni aperte indicate nella sezione 'Rischi e domande' del piano."""
    m = QUESTION_SECTION.search(text)
    if not m:
        return []
    questions: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if "?" in line:
            cleaned = re.sub(r"^(?:[-*•]|\d+\.)\s*", "", line).strip()
            if cleaned and cleaned not in questions:
                questions.append(cleaned)
    return questions


def _worktree_prefix(worktree: str | None) -> re.Pattern:
    if worktree:  # il worktree di questo run: <progetto>-<id run>
        return re.compile(START + r"(?:/[^\s/()\]`]+)*/" + re.escape(worktree) + "/")
    return re.compile(START + r"(?:/[^\s/()\]`]+)*/\.jarvis/worktrees/[^/\s()\]`]+/")


def to_telegram_html(text: str, worktree: str | None = None) -> str:
    """Link markdown ai file e percorsi assoluti tolti, **grassetto**, `codice`, titoli ed elenchi puntati nei tag
    HTML di Telegram. Ogni tag sta su una riga sola, così il testo si può spezzare ai fine riga."""
    kept: list[str] = []

    def keep(fragment: str) -> str:
        kept.append(fragment)
        return f"\x00{len(kept) - 1}\x00"

    text = _worktree_prefix(worktree).sub("", text.replace("\x00", ""))

    def link(m: re.Match) -> str:
        label, url = m.groups()
        if re.match(r"https?://", url):
            return keep(f'<a href="{html.escape(url, quote=True)}">{html.escape(label.strip("`"), quote=False)}</a>')
        return label  # link a un file locale: resta il nome, che è quello che si legge

    text = LINK.sub(link, text)
    text = CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = html.escape(text, quote=False)
    text = RULE.sub("", text)
    text = HEADING.sub(lambda m: "<b>" + m.group(1).replace("**", "") + "</b>", text)
    text = BOLD.sub(r"<b>\1</b>", text)
    text = BULLET.sub(lambda m: m.group(1) + "• ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return TOKEN.sub(lambda m: kept[int(m.group(1))], text)

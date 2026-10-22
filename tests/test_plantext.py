from jarvis.plantext import extract_plan_questions, to_telegram_html

WT = "/Users/someone/.jarvis/worktrees/my-app-19"


def test_local_file_links_keep_only_the_name():
    text = f"In [`RideDetailView.kt`](file://{WT}/history/src/main/RideDetailView.kt#L380-L414) la funzione [`FirstRow`](file://{WT}/history/src/main/RideDetailView.kt#L381-L413)"
    out = to_telegram_html(text)
    assert out == "In <code>RideDetailView.kt</code> la funzione <code>FirstRow</code>"
    assert "file://" not in out and "worktrees" not in out and "#L" not in out


def test_absolute_worktree_paths_in_plain_text_become_relative():
    assert to_telegram_html(f"vedi {WT}/app/build.gradle e file://{WT}/x.kt") == "vedi app/build.gradle e x.kt"


def test_web_links_stay_clickable_and_are_escaped():
    out = to_telegram_html('Issue [#3481](https://github.com/o/r/issues/3481?a=1&b=2) <ok>')
    assert out == 'Issue <a href="https://github.com/o/r/issues/3481?a=1&amp;b=2">#3481</a> &lt;ok&gt;'


def test_headings_bold_bullets_and_code_become_telegram_html():
    out = to_telegram_html("### 1. Causa\nIl valore è **fisso** a `16sp` <b>.\n\n- primo\n  * secondo\n---\n- terzo")
    assert out == ("<b>1. Causa</b>\nIl valore è <b>fisso</b> a <code>16sp</code> &lt;b&gt;.\n\n• primo\n  • secondo\n\n• terzo")


def test_code_spans_are_escaped_and_not_reformatted():
    assert to_telegram_html("`a < b` e `**x**`") == "<code>a &lt; b</code> e <code>**x**</code>"


def test_heading_with_bold_does_not_nest_tags_and_every_tag_stays_on_one_line():
    out = to_telegram_html("## **Rischi** e domande\ntesto **a\nb**")
    assert out.splitlines()[0] == "<b>Rischi e domande</b>"
    assert all(line.count("<b>") == line.count("</b>") for line in out.splitlines())


def test_plain_text_and_numbered_lists_are_untouched_apart_from_escaping():
    assert to_telegram_html("1. Tocca app.txt\n2. a & b") == "1. Tocca app.txt\n2. a &amp; b"
    assert to_telegram_html("\n\n\nuno\n\n\n\ndue\n") == "uno\n\ndue"


def test_only_the_runs_own_worktree_is_stripped():
    text = f"vedi {WT}/app/a.kt, ma {WT.replace('my-app-19', 'other-3')}/b.kt resta"
    out = to_telegram_html(text, "my-app-19")
    assert out == f"vedi app/a.kt, ma {WT.replace('my-app-19', 'other-3')}/b.kt resta"


def test_urls_relative_paths_and_home_paths_are_never_mangled():
    assert to_telegram_html("https://github.com/x/worktrees/y/z") == "https://github.com/x/worktrees/y/z"
    assert to_telegram_html("runner/worktrees/abc/f.py") == "runner/worktrees/abc/f.py"
    assert to_telegram_html("`~/.jarvis/worktrees/key-3/` e ~/.jarvis/worktrees/key-3/x") == (
        "<code>~/.jarvis/worktrees/key-3/</code> e ~/.jarvis/worktrees/key-3/x")
    link = to_telegram_html("[doc](https://github.com/x/worktrees/y/z)")
    assert link == '<a href="https://github.com/x/worktrees/y/z">doc</a>'


def test_nul_characters_in_the_model_text_cannot_break_the_tokens():
    assert to_telegram_html("a \x000\x00 `b`") == "a 0 <code>b</code>"


def test_extract_plan_questions_finds_questions_in_section_4():
    plan = (
        "1. Causa: fix calcolo\n"
        "2. Modifiche: app/Foo.kt\n"
        "3. Test: FooTest.kt\n"
        "4. **Rischi e domande**:\n"
        "- Come gestire i rimborsi parziali?\n"
        "- Attenzione alla compatibilità con versioni vecchie.\n"
        "• Serve aggiornare le release note?\n"
    )
    questions = extract_plan_questions(plan)
    assert questions == [
        "Come gestire i rimborsi parziali?",
        "Serve aggiornare le release note?",
    ]


def test_extract_plan_questions_returns_empty_when_no_questions():
    plan = (
        "1. Causa: fix calcolo\n"
        "2. Modifiche: app/Foo.kt\n"
        "3. Test: FooTest.kt\n"
        "4. **Rischi e domande**:\n"
        "- Nessun rischio rilevato.\n"
    )
    assert extract_plan_questions(plan) == []


def test_extract_plan_questions_returns_empty_when_no_questions_section():
    plan = (
        "1. Causa: perché accade? Non è chiaro.\n"
        "2. Modifiche: app/Foo.kt\n"
        "3. Test: FooTest.kt\n"
    )
    assert extract_plan_questions(plan) == []

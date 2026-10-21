from jarvis.plantext import to_telegram_html

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

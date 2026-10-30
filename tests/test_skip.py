"""`N salta` / `N mai`: la voce saltata lascia il posto alla prima riserva del planner e il task non torna per un po'."""

from pathlib import Path

import pytest

from jarvis import briefing, config, db, planner, runs
from jarvis.ingest import ingest

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"
DAY = "2026-10-11"


@pytest.fixture
def cfg():
    return config.load(CONFIG)


@pytest.fixture
def conn(tmp_path, cfg):
    c = db.connect(tmp_path / "t.db")
    tasks = [
        {"source": "jira", "external_id": "PROJ-1", "container": "PROJ", "title": "Crash", "url": "u",
         "source_status": "To Do", "labels": ["jarvis"], "issue_type": "Bug"},
        {"source": "jira", "external_id": "PROJ-2", "container": "PROJ", "title": "Login", "url": "u",
         "source_status": "To Do", "labels": ["jarvis"], "issue_type": "Bug"},
        {"source": "github", "external_id": "alex-dev/novainfo#31", "container": "alex-dev/novainfo",
         "title": "Dipendenze", "url": "u", "source_status": "OPEN", "labels": [], "created_by": "alex-dev"},
        {"source": "github", "external_id": "alex-dev/novainfo#32", "container": "alex-dev/novainfo",
         "title": "Icone", "url": "u", "source_status": "OPEN", "labels": [], "created_by": "alex-dev"},
    ]
    ingest(c, cfg, {"tasks": tasks}, "2026-10-01T08:00:00+00:00")
    return c


def tid(conn, ref):
    return conn.execute("SELECT id FROM tasks WHERE external_id = ?", (ref,)).fetchone()[0]


def item(task_id, executor="claude-sonnet", type_="bugfix", hours=1, rationale="r"):
    return {"task_id": task_id, "type": type_, "executor": executor, "estimate_hours": hours, "rationale": rationale}


class Fake:
    def __init__(self, output):
        self.output = output

    def __call__(self, system, message, schema, model, timeout):
        return self.output


def make_briefing(conn, cfg, items, reserves=None, date=DAY):
    output = {"summary": "s", "items": [item(tid(conn, r)) for r in items]}
    if reserves is not None:
        output["reserves"] = [item(tid(conn, r), rationale=f"riserva {r}") for r in reserves]
    return planner.plan(conn, cfg, date, available_hours=2, runner=Fake(output), force=True)


def reply(conn, cfg, briefing_id, text):
    conn.execute("BEGIN")
    done = briefing.apply(conn, briefing_id, briefing.parse(text, cfg), cfg)
    conn.execute("COMMIT")
    return done


def items(conn, date=DAY):
    return [(i["position"], i["external_id"], i["status"]) for i in planner.briefing(conn, date)["items"]]


def memory(conn, ref):
    r = conn.execute("SELECT snoozed_until, skip_count, planner_excluded FROM tasks WHERE external_id = ?",
                     (ref,)).fetchone()
    return tuple(r)


def test_reserves_are_stored_and_invalid_ones_dropped(conn, cfg):
    output = {"summary": "s", "items": [item(tid(conn, "PROJ-1"))], "reserves": [
        item(tid(conn, "PROJ-1")),                 # già fra le voci
        item(999),                                  # inventato
        item(tid(conn, "PROJ-2"), executor="claude-haiku"),  # my-app ha override bugfix -> sonnet
        item(tid(conn, "PROJ-2")),                 # duplicato
        item(tid(conn, "alex-dev/novainfo#31")),
    ]}
    b = planner.plan(conn, cfg, DAY, available_hours=2, runner=Fake(output))
    rows = conn.execute("SELECT task_id, rank, proposed_executor, decision_source FROM briefing_reserves "
                        "WHERE briefing_id = ? ORDER BY rank", (b,)).fetchall()
    assert [(r["task_id"], r["rank"]) for r in rows] == [(tid(conn, "PROJ-2"), 1), (tid(conn, "alex-dev/novainfo#31"), 2)]
    assert (rows[0]["proposed_executor"], rows[0]["decision_source"]) == ("claude-sonnet", "project_rule")


def test_reserves_are_optional_and_capped(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"])  # il planner non le ha date: il briefing vale lo stesso
    assert planner.briefing(conn, DAY)["status"] == "draft"
    assert conn.execute("SELECT count(*) FROM briefing_reserves WHERE briefing_id = ?", (b,)).fetchone()[0] == 0
    assert "reserves" in planner.schema(cfg)["properties"] and "reserves" not in planner.schema(cfg)["required"]

    output = {"summary": "s", "items": [], "reserves": [item(tid(conn, r)) for r in
                                                        ("PROJ-1", "PROJ-2", "alex-dev/novainfo#31", "alex-dev/novainfo#32")]}
    planner.MAX_RESERVES, old = 3, planner.MAX_RESERVES
    try:
        b = planner.plan(conn, cfg, DAY, available_hours=2, runner=Fake(output), force=True)
    finally:
        planner.MAX_RESERVES = old
    assert conn.execute("SELECT count(*) FROM briefing_reserves WHERE briefing_id = ?", (b,)).fetchone()[0] == 3


def test_skip_brings_in_next_reserve_and_snoozes(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1", "alex-dev/novainfo#31"], reserves=["PROJ-2", "alex-dev/novainfo#32"])
    done = reply(conn, cfg, b, "1 salta")
    assert done == ["1 saltata · al suo posto 3 PROJ-2 → claude-sonnet"]
    assert items(conn) == [(1, "PROJ-1", "skipped"), (2, "alex-dev/novainfo#31", "proposed"), (3, "PROJ-2", "proposed")]
    new = planner.briefing(conn, DAY)["items"][2]
    assert (new["rationale"], new["decision_source"]) == ("riserva PROJ-2", "project_rule")
    assert memory(conn, "PROJ-1") == ("2026-10-18", 1, 0)

    reply(conn, cfg, b, "3 salta")  # la riserva usata non torna: arriva la successiva
    assert items(conn)[-1] == (4, "alex-dev/novainfo#32", "proposed")
    assert reply(conn, cfg, b, "2 salta") == ["2 saltata · nessuna riserva (<code>ore N</code> per rigenerare)"]


def test_replacement_is_never_auto_approved(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1", "alex-dev/novainfo#31"], reserves=["PROJ-2"])
    reply(conn, cfg, b, "1 salta, ok")  # l'ok riguarda quello che l'owner ha visto
    assert items(conn)[-1] == (3, "PROJ-2", "proposed")
    reply(conn, cfg, b, "ok")
    assert items(conn)[-1] == (3, "PROJ-2", "approved")

    b2 = make_briefing(conn, cfg, ["alex-dev/novainfo#31", "alex-dev/novainfo#32"], reserves=["PROJ-2"],
                       date="2026-10-12")
    reply(conn, cfg, b2, "ok")
    reply(conn, cfg, b2, "1 salta")  # briefing già confermato
    assert items(conn, "2026-10-12")[-1] == (3, "PROJ-2", "proposed")
    with pytest.raises(runs.RunError):
        runs.queue(conn, cfg, b2, [3])


def test_double_skip_is_idempotent(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"], reserves=["PROJ-2", "alex-dev/novainfo#31"])
    reply(conn, cfg, b, "1 salta 1 salta")
    reply(conn, cfg, b, "1 salta")
    assert items(conn) == [(1, "PROJ-1", "skipped"), (2, "PROJ-2", "proposed")]
    assert memory(conn, "PROJ-1")[1] == 1


def test_skip_of_queued_item_changes_no_memory(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"], reserves=["PROJ-2"])
    conn.execute("UPDATE briefing_items SET status = 'queued' WHERE briefing_id = ?", (b,))
    reply(conn, cfg, b, "1 salta")
    assert memory(conn, "PROJ-1") == (None, 0, 0)
    assert [i[1] for i in items(conn)] == ["PROJ-1"]


def test_skip_all_changes_no_memory(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"], reserves=["PROJ-2"])
    reply(conn, cfg, b, "salta tutto")
    assert memory(conn, "PROJ-1") == (None, 0, 0)
    assert [i[1] for i in items(conn)] == ["PROJ-1"]


def test_snoozed_task_comes_back_after_its_date(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"])
    reply(conn, cfg, b, "1 salta")
    refs = lambda day: {t["ref"] for t in planner.candidates(conn, cfg, day)}  # noqa: E731
    assert "PROJ-1" not in refs(DAY)  # rigenerato lo stesso giorno ("ore 4"): resta fuori
    assert "PROJ-1" not in refs("2026-10-17")
    assert "PROJ-1" in refs("2026-10-18")


def test_snoozed_task_does_not_use_up_candidate_slots(conn, cfg):
    cfg.raw["planner"]["max_tasks"] = 3
    b = make_briefing(conn, cfg, ["PROJ-1"])
    reply(conn, cfg, b, "1 salta")
    assert len(planner.candidates(conn, cfg, DAY)) == 3


def test_ask_after_repeated_skips(conn, cfg):
    for n, day in enumerate(("2026-10-01", "2026-10-09", "2026-10-17"), start=1):
        b = make_briefing(conn, cfg, ["alex-dev/novainfo#31"], date=day)
        done = reply(conn, cfg, b, "1 salta")
    assert n == 3 and done[-1] == ("novainfo#31 saltato 3 volte: <code>1 mai</code> per non riproporlo"
                                   " o <code>chiudi la issue alex-dev/novainfo#31</code>")
    b = make_briefing(conn, cfg, ["PROJ-1"], date="2026-10-20")
    conn.execute("UPDATE tasks SET skip_count = 2 WHERE external_id = 'PROJ-1'")
    assert reply(conn, cfg, b, "1 salta")[-1] == "PROJ-1 saltato 3 volte: <code>1 mai</code> per non riproporlo"


def test_never_excludes_and_explicit_add_brings_back(conn, cfg):
    assert briefing.parse("2 mai", cfg) == [{"cmd": "never", "position": 2}]
    b = make_briefing(conn, cfg, ["PROJ-1", "alex-dev/novainfo#31"], reserves=["PROJ-2"])
    done = reply(conn, cfg, b, "1 mai")
    assert done == ["1 non verrà più proposta · al suo posto 3 PROJ-2 → claude-sonnet"]
    assert memory(conn, "PROJ-1")[2] == 1
    assert "PROJ-1" not in {t["ref"] for t in planner.candidates(conn, cfg, "2030-01-01")}

    reply(conn, cfg, b, "PROJ-1 sonnet")  # aggiunto a mano: l'owner lo rivuole
    assert memory(conn, "PROJ-1") == (None, 0, 0)


def test_queue_resets_skip_count(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1", "alex-dev/novainfo#31"])
    conn.execute("UPDATE tasks SET skip_count = 2 WHERE external_id = 'alex-dev/novainfo#31'")
    reply(conn, cfg, b, "ok")
    runs.queue(conn, cfg, b, [2])
    assert memory(conn, "alex-dev/novainfo#31")[1] == 0


def test_replan_drops_old_reserves(conn, cfg):
    b = make_briefing(conn, cfg, ["PROJ-1"], reserves=["PROJ-2"])
    make_briefing(conn, cfg, ["PROJ-1"], reserves=["alex-dev/novainfo#31"])
    rows = conn.execute("SELECT task_id FROM briefing_reserves WHERE briefing_id = ?", (b,)).fetchall()
    assert [r[0] for r in rows] == [tid(conn, "alex-dev/novainfo#31")]

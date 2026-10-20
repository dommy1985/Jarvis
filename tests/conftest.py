import pytest

from jarvis import i18n


@pytest.fixture(autouse=True)
def italian_locale():
    """I test storici asseriscono i testi italiani, come il config del hub (owner.locale: it). I test degli altri
    locali usano i18n.using("en")."""
    i18n.set_locale("it")
    yield
    i18n.set_locale("it")


@pytest.fixture(autouse=True)
def provider_clis_are_installed(monkeypatch):
    """I test non dipendono dalle CLI installate sulla macchina (in CI non c'è nessuna): per i provider
    `shutil.which` trova sempre la CLI. I test su "CLI non installata" lo ridefiniscono."""
    import shutil

    real = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **kw: f"/usr/bin/{name}" if name in ("claude", "agy", "copilot") else real(name, *a, **kw))


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Le commit dei test non dipendono dalla configurazione git globale della macchina (in CI non c'è)."""
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Alex")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "d@example.com")  # come nei repo di prova dei test

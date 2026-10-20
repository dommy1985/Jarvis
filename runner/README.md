# Runner Jarvis (Mac)

Esegue i run messi in coda da Telegram (`vai N`): un task alla volta, in un git worktree, con l'esecutore
assegnato (Claude Code o Antigravity), poi build e test, push del branch `agent/<id>` e PR draft con `gh`.
Dettagli in [`docs/GUIDA.md`](../docs/GUIDA.md#esecuzione-fase-2) e [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

## Installazione

```
scripts/install-runner.sh
```

- copia `JARVIS_RUNNER_TOKEN` dal `.env` del VPS in `~/.config/jarvis/runner.env` (permessi 600, senza stamparlo);
- macOS: installa il LaunchAgent `dev.jarvis.runner` (`JARVIS_RUNNER_LABEL`) da `runner/jarvis-runner.plist`: parte al login e si riavvia se cade. Con `JARVIS_RUNNER_LEGACY_LABEL` il LaunchAgent di una vecchia installazione viene fermato e rimosso (un run in corso viene interrotto). Log in `~/Library/Logs/jarvis-runner.log` (`JARVIS_RUNNER_LOG`);
- Linux: installa l'unit systemd utente `jarvis-runner.service` (`JARVIS_RUNNER_UNIT`) in `~/.config/systemd/user` da `runner/jarvis-runner.service`. Log: `journalctl --user -u jarvis-runner -f`. Per tenerlo acceso senza login: `loginctl enable-linger $USER`. Lo script serve anche per runner e server sulla stessa macchina (VPS): senza `JARVIS_SSH_HOST` non c'è tunnel.

Stato locale del runner (log degli agenti, hook, aggiornamenti non consegnati): `~/.jarvis`, cambiabile con la variabile `JARVIS_HOME` nell'ambiente del processo (aggiungila al plist o all'unit); i worktree sono in `limits.worktrees` di `projects.yaml`. Su Linux i comandi di setup/build/test girano nella shell di login di `/etc/passwd` (zsh sul Mac): evita sintassi solo-zsh nei comandi di `projects.yaml`.

Aggiornamento: dopo `git pull` riavvia il runner subito (un processo con il codice vecchio cerca i prompt in `prompts/*.md`, ora in `prompts/<locale>/`). Lingua di prompt, PR e messaggi: `job["locale"]`, dal `owner.locale` del server; un job senza `locale` (server precedente) resta in italiano.

Rimozione: `scripts/install-runner.sh --remove`. Prova a mano senza LaunchAgent: `uv run python -m runner.runner --once`.

## Requisiti sul Mac

- collegamento al server, in `~/.config/jarvis/runner.env` (vedi sotto);
- `gh` autenticato come `owner.github` di `projects.yaml`, `claude` loggato con la subscription, `agy` loggato; `copilot` (opzionale, `cli: copilot`) loggato con `copilot /login` o `GH_TOKEN`;
- i repo ai path di `config/projects.yaml`, con `origin` e il branch di default su `origin/HEAD`.

## Collegamento al server

Due modi, scelti da `~/.config/jarvis/runner.env` (le variabili `JARVIS_*` dell'ambiente hanno la precedenza):

- **Un solo host** (runner e server sulla stessa macchina): niente tunnel. `JARVIS_URL` (default `http://127.0.0.1:8090`).
- **Host separati** (server su un VPS, runner su un laptop): `JARVIS_SSH_HOST=<host>` apre `ssh -L <locale>:127.0.0.1:<remota> <host>` e lo riapre se cade (serve `ssh <host>` senza password). Porte: `JARVIS_SSH_LOCAL_PORT` (default: la porta di `JARVIS_URL`, altrimenti 8090) e `JARVIS_SSH_REMOTE_PORT` (8090). Senza `JARVIS_URL` il runner usa `http://127.0.0.1:<porta locale>`.

Senza `JARVIS_SSH_HOST` il runner non apre mai un tunnel (chi usa il tunnel deve avere `JARVIS_SSH_HOST` in `runner.env`). `scripts/install-runner.sh` scrive queste variabili alla prima installazione se sono nell'ambiente, e copia il token dal server con `JARVIS_SSH_HOST` (file in `JARVIS_SERVER_ENV`) oppure lo prende da `JARVIS_RUNNER_TOKEN`.

## Cosa fa per ogni run

1. `git fetch`, worktree in `~/.jarvis/worktrees/<progetto>-<id>` sul nuovo branch `agent/<id>` da `origin/<default>`.
2. Copia i `copy_files` del progetto dal repo principale (mai committati).
3. `setup` del progetto.
4. Agente nel worktree con `prompts/<locale>/task.md`:
   - Claude: `claude -p --permission-mode acceptEdits`, Bash solo per i comandi di setup/build/test e git in lettura, niente `git push` né `gh`; impostazioni solo di progetto (`CLAUDE.md`, advisor).
   - Antigravity: `agy -p --mode accept-edits`; i comandi di shell sono permessi solo se nella allowlist globale di agy (`permissions.allow` in `~/.gemini/antigravity-cli/settings.json`, formato `command(<prefisso>)`, vale anche con argomenti, pipe e `&&`). Al primo comando negato agy chiude il turno: il prompt gli elenca i comandi permessi (`AGY_COMMANDS` in `runner.py`), che vanno tenuti allineati alla allowlist.
   - Copilot (`cli: copilot`, capability `execute` e `review`): `copilot -p --output-format json --no-ask-user`; con `skip_permissions` `--allow-all-tools`, altrimenti `--allow-tool=write` e `--allow-tool=shell(<cmd>:*)` per i comandi permessi (allowlist non verificata in headless). `git push` e `gh` sono sempre negati con `--deny-tool`. Piano e review: `--mode plan` con `--deny-tool=write --deny-tool=shell` (la review ha lo schema nel prompt, nessun `--json-schema`). Niente quote (`quota`) né `plan_structured` nativo; errori riconosciuti: exit 1 `No authentication information found` (verificato), limiti/rate limit (euristica).
5. `build && test` lanciati dal runner.
6. Commit (autore: la tua identità git), blocco se nel commit finiscono file che sembrano segreti, push del branch, `gh pr create --draft`.
7. Worktree rimosso se la PR è aperta; lasciato sul disco se qualcosa fallisce; rimosso (con il branch) se il run viene fermato da Telegram.

Durante setup, agente e test il runner chiede a jarvis ogni 30 secondi se il run va fermato (`ferma N`) e, se sì, termina il comando con tutti i processi figli.

Review incrociata (fase 3): dopo build e test il runner passa il diff (app e submodule cambiati, max 80k caratteri) alla controparte con `prompts/<locale>/review.md`: Claude con `--tools Read,Grep,Glob`, agy con `--sandbox` senza `accept-edits`, entrambi con `--json-schema`. Se il revisore cambia comunque qualcosa nel worktree, le modifiche vengono annullate (il lavoro dell'agente è già nell'index). Con problemi `blocker`/`major` l'esecutore riparte una volta con `prompts/<locale>/fix.md` in coda al prompt, poi build e test ripartono. Anche i test falliti fanno partire il giro di correzione: l'esecutore riceve i test falliti letti dai report JUnit (`**/build/test-results/**/TEST-*.xml`). Se agy si ferma su un comando negato senza dare la review, il runner riprova una volta dicendogli di non usare comandi. La review ha un tempo massimo suo (`review.timeout_minutes`, 15) e il prompt chiede di restare sui file del diff. Errori, timeout o quota del revisore non bloccano la PR. L'output grezzo di agente, revisore e correzione resta in `~/.jarvis/logs/run-<id>-*.txt`.

Quote: prima di lanciare agy (esecutore o revisore) `agy -p /usage` (non consuma quota); bucket del gruppo del modello a zero → run fallito con `rate_limited`, e jarvis lo rimette in coda con la controparte. Gli snapshot finiscono in `quota_events`. Per Claude il limite si riconosce dall'output di `claude -p` (anche quando non è JSON o arriva con `is_error: false`); l'evento `rate_limited` porta `reset_at` letto dal messaggio (`|<epoch>` o `resets 3pm (Europe/Rome)`), e jarvis fino a quell'ora fa partire i run di Claude direttamente con la controparte.

Run `plan` (`vai N piano`, `modifica N <testo>`): worktree staccato sulla base + setup, poi solo l'agente in sola lettura con `prompts/<locale>/plan.md` (Claude con `--tools Read,Grep,Glob`; agy con `--mode plan --sandbox`, mai `--dangerously-skip-permissions`; `plan.timeout_minutes`, 15). Un piano da rivedere riceve il precedente e la nota dell'owner (`prompts/<locale>/plan-revise.md`). Niente build, test, review, commit o push; se il worktree cambia, il runner annulla. Il piano torna a jarvis nel campo `plan` con `status=done`. Con `approva N` parte un run `agent` normale con `job["approved_plan"]`, aggiunto al prompt da `prompts/<locale>/approved-plan.md`.

Log dal vivo: gli agenti girano in `stream-json` (`claude --verbose`, `agy`) con `sh_stream`; `claude_event_lines` / `agy_event_lines` trasformano gli eventi in una riga per azione (strumenti e testo, mai il contenuto dei file) e `LogFeed` le manda a jarvis ogni 5 secondi (`log` in `/runner/update`), con i segreti coperti da `mask`. Le righe finali viaggiano con l'aggiornamento di stato finale.

Run `repair` (`ripara N` da Telegram): worktree staccato su `origin/agent/N` (anche nel submodule, tramite il puntatore), niente lavoro da capo. Build e test; se falliscono, o se il job porta `repair.feedback` (commenti di review e check CI falliti raccolti da jarvis), giro di correzione con test falliti, `commenti_pr` e `ci_fallita` nel blocco `<review>` di `prompts/<locale>/fix.md` e test di nuovo; poi review incrociata di tutto il lavoro rispetto alla base (nel submodule dal commit puntato dall'app su `develop`) e, con problemi gravi, un altro giro. Commit e push su `agent/N` (submodule prima), commento con esito e review su ogni PR; se il submodule viene toccato solo ora, apre la sua PR. Esito `done`.

Run `realign` (niente agente): worktree staccato su `origin/agent/N` della PR app, submodule sul commit dello squash merge, build e test, commit `Realign <path> to merged <sha>` e push sullo stesso branch. Lo mette in coda jarvis quando vede mergiata la PR del submodule.

Ambiente dell'agente: `core.hooksPath` con un `pre-push` che rifiuta, `credential.helper` vuoto, `GIT_SSH_COMMAND` che fallisce, `GH_CONFIG_DIR` vuota, niente `GH_TOKEN`/`SSH_AUTH_SOCK`. Il runner usa invece il tuo ambiente normale per push e PR.

Progetti con submodule (`submodules` in `projects.yaml`, es. my-app → my-lib): l'agente lavora nel worktree dell'app e può modificare il submodule, che resta sul commit puntato dall'app. Se il submodule cambia, il runner lancia anche i suoi test (`test` del progetto del submodule, nella sua cartella, con i suoi `copy_files`), poi crea `agent/<id>` nel submodule, committa, pusha e apre la PR draft sul repo del submodule; infine committa l'app con il puntatore aggiornato e apre la PR dell'app, che rimanda alla prima (e viceversa). `Closes` va nella PR del repo del task. I task di un submodule (`vai my-lib#12 …`) girano nel worktree dell'app, con il submodule come focus nel prompt. Il framework fa squash merge: dopo il merge il puntatore dell'app va riportato sul commit mergiato.

`agent_notes` del progetto finiscono nel prompt come regole in più (es. riga nel `changelog.md` di ogni modulo toccato).

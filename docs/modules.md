# Moduli per sito

Un modulo è lo studio salvato di un negozio, in due procedure più i segnali del monitor:

| procedura | cosa contiene | codice |
|---|---|---|
| **auth** (login e registrazione) | endpoint, token, campi del form e mappa verso il profilo, captcha, esiti, limiti | `drop_monitor/account.py` |
| **checkout** (carrello e acquisto) | add-to-cart, carrello, passi del one-page checkout, scelta spedizione/pagamento, riepilogo, conferma | `drop_monitor/order/` |
| **monitor** | pagine da leggere e segnali di disponibilità/prezzo | `drop_monitor/parsers/` |

Il registro (`drop_monitor/sites/__init__.py`) risolve il modulo dal dominio dell'URL; la finestra lo mostra in
**Moduli** con la data della verifica. Oggi: `gemcard` (gemcardinfinitycollection.it, nopCommerce 3.x).

## Come si aggiunge un sito

1. Studio: richieste reali con un User-Agent onesto, pagine salvate come fixture in `tests/fixtures/`.
2. Parser (se la piattaforma è nuova) in `parsers/`, test sulle fixture.
3. Procedura auth: campi, token, captcha (sempre risolto da una persona), esiti.
4. Procedura checkout: fino al riepilogo in dry-run, conferma solo dopo limite e conferma umana.
5. `drop_monitor/sites/<key>.py` con `Procedure` documentate e `register(SiteModule(...))`.

## Generatore account (teoria)

Previsto ma non attivo: N account da N profili, con il modulo auth del sito, captcha visto dalla persona,
esito salvato nel profilo. Decisione aperta: quale e-mail per ogni account e come leggere le conferme.
Principio: un account per persona e indirizzo reali; non è uno strumento per aggirare i limiti del negozio.

## Registrazione nel browser (modulo auth, modalità visibile)

`drop_monitor/browser.py` esegue la procedura auth in un Chromium reale (Playwright): apre il sito, clicca
"Registrati", scrive ogni campo dal profilo (nome, cognome, e-mail, indirizzo, CAP, città, nazione, provincia via
AJAX, telefono, dati fatturazione), spunta i consensi, newsletter e "come ci hai conosciuto", inserisce la password,
poi si ferma sul captcha finché la persona lo scrive (nel browser o nella finestra). In dry-run non preme
"Registrati". Altrimenti preme, legge l'esito (`registerresult/1|2|3` o errori), salva la password in `.env`, i
cookie in `personal/sessions/<profilo>.json` e marca il profilo come registrato (`account.registered_at`). Ogni
passo è registrato con orario e screenshot in `data/browser/<data-ora>/`. Modalità headless disponibile per quando
la fase sarà "senza schermo": il captcha arriva allora dalla finestra.

Requisiti: `pip install playwright` + `playwright install chromium` (bottone "Installa browser" nella sezione
Account). Test automatico: Chromium headless contro una copia locale del modulo del negozio, senza rete.

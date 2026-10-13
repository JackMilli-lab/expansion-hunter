# Expansion Hunter — Windows desktop packaging report

Datum: **5 oktober 2026, Europe/Amsterdam**.

**Dit is een aanvullende desktopverpakking van de bestaande Expansion Hunter. De werkende scanner, cockpit, opslag, indicators, scores en read-only exchange-adapters blijven behouden. Er is geen live orderuitvoering toegevoegd.**

De bestaande webapp heeft echte publieke marktdata ontvangen en is in Chromium op desktop/mobile getest. Een native Windows-installatie, WebView2-venster en sluit/herstartcyclus zijn op deze Linux-werkplek **nog niet daadwerkelijk getest**. Een Linux-loopbacktest of aanwezigheid van een EXE mag die Windows-test niet vervangen.

## Gebruikersdoel

De Windows-gebruiker moet een meegeleverd programma kunnen openen door te dubbelklikken. Hij hoeft geen Python te installeren, geen terminalcommando te typen en geen URL te openen. De bestaande cockpit verschijnt in een eigen Windows-appvenster.

Het pakket bevat een Windows Python-runtime en de bestaande app met zijn dependencies. De launcher start een lokale beschermde backend op een vrije willekeurige loopbackpoort. De poort en het tijdelijke toegangstoken blijven binnen de app; de gebruiker hoeft ze niet in te voeren. Dit is lokale desktopverpakking, geen installatie van een exchange trading client.

De scanner start automatisch. Het geconfigureerde app-universum blijft 500; een persoonlijke watchlist is **niet vereist**. Automatisch gepromoveerde kandidaten krijgen intensieve monitoring; de brede scanner doet geen voortdurende dure 1m/orderboekanalyse op alle 500.

## Desktoparchitectuur en hergebruik

```text
Windows launcher / meegeleverde Python-runtime
  → expansion_hunter.desktop
  → apart proces: expansion_hunter.desktop_backend
  → bestaande beschermde FastAPI-app op 127.0.0.1 en vrije poort
  → native pywebview / Microsoft WebView2-venster
  → bestaande cockpit.html, API en realtime dataworkers
  → bestaande SQLite discovery/fast-event/watchlist-archieven
```

De backend gebruikt bestaande API- en lifecycle-abstractions. Automatic observation intent wordt persistent vastgelegd, zodat geen handmatige symbolenlijst nodig is. Het sluiten van het native venster stopt uitsluitend de bijbehorende backend/scannerprocessen veilig. Tijdgevoelige snapshots worden na herstart pas weer als vers behandeld wanneer verse waarnemingen arriveren.

## Per-user opslag

De desktopversie gebruikt:

```text
%LOCALAPPDATA%\ExpansionHunter\
  config\settings.json
  data\cockpit.sqlite3
  logs\desktop.log
  logs\backend.log
  sessions\<willekeurige-sessie>\
```

Sessionbestanden regelen de gereed-/stopcommunicatie. De persoonlijke database wordt niet in de geïnstalleerde runtime weggegooid. De bestaande repositorydatabase en historische `data/smoke.sqlite3` worden niet verwijderd. Het klonen van de app naar een nieuwe Windows-gebruiker impliceert niet automatisch het kopiëren van de ontwikkelaarsdatabase naar die gebruiker.

## Distributie en status

| Onderdeel | Huidige bewijsstatus |
| --- | --- |
| Desktopcontroller / backend modules | Uitvoerbare modules; zeven root-tests geslaagd inclusief echte beschermde subprocess-backend, geen Windows-GUI-bewijs. |
| Zelfstandige Windows EXE / ZIP | Buildscript en dependenciespecificatie aanwezig; geen werkelijk gebouwd distributiepakket geclaimd. |
| Windows 10/11 x64 als doelplatform | Ontwerpdoel; native uitvoering nog niet getest. |
| Eigen appvenster met pywebview/WebView2 | Implementatiepad; geen native Windows-venster op deze werkplek geverifieerd. |
| Meegeleverde Python/runtime/dependencies | Pakketbouw in uitvoering; geen geslaagde installatie op Windows geclaimd. |
| Bureaubladsnelkoppeling | Native CreateProcessW/COM-snelkoppeling en veilige extractor op disk; C-parser/quotingtests uitgevoerd, Windows uitvoering niet bevestigd. |
| WebView2 prerequisite-helper | Niet bevestigd. Officiële Microsoft-runtime vereist als WebView2 ontbreekt. |
| Automatische loopbackbackend en toegangstoken | Implementatie in uitvoering; controller/subprocesschecks volgen. |
| Persoonlijke watchlist optioneel | Werkend principe van de automatische scanner; final desktop-appsmoke volgt. |
| Privé tradingkeys / orderuitvoering | Niet aanwezig; uitsluitend publieke exchange-data. |

De definitieve pakkethandleiding staat in [desktop/windows/README_NL.md](desktop/windows/README_NL.md). De gebruiker hoeft geen broncode te schrijven of buildcommando uit te voeren. Onderstaande verificatie is voor de ontwikkelaars/buildomgeving, niet voor de eindgebruiker.

## Verificatie die nog moet worden toegevoegd

1. Finale desktop unit-/controller-/subprocesstestresultaten en volledige regressietelling.
2. Exacte buildcommand, Windows runtimeversie, wheel/platformselectie en pakkethashes.
3. Werkelijke distributiebestanden en eventuele snelkoppeling-/WebView2-helpernamen.
4. Native Windowscontrole: dubbelklik → eigen venster → automatische scanner → live candles/liquiditeit → sluiten → herstart met behouden data.
5. Afhandeling van ontbrekende WebView2, gelijktijdige start en backendstartfouten op Windows.

Geen enkele verificatiestap wordt als geslaagd gemarkeerd totdat uitvoerbaar bewijs beschikbaar is.

## Echte marktdata-evidence uit de bestaande app

Deze evidence hoort bij de bestaande backend/webcockpit, **niet bij een reeds geverifieerde Windows-installatie**:

- Publieke Binance WebSocket-proef: 3.776 echte berichten in 20 seconden, geen synthetische fallback ([public-websocket-smoke.json](reports/public-websocket-smoke.json)).
- Live browserproef: candles en prijs veranderden zonder refresh, zes tijdframes, exacte orderboekprijzen en zichtbaar stop/start-herstel ([live-app-smoke.json](reports/live-app-smoke.json)).
- Echte broad-discoveryproef: 500 gevraagd, 491 ontdekt, 122 lichte histories, 76 geschikte assets, tien deep scans, 415 uitsluitingen, 76 partial-data assets, nul failed; status DEGRADED ([real-500-discovery.json](reports/real-500-discovery.json)).
- Werkelijke BTC-events: een gesloten 1m breakout, latere 5m acceptatie en vervolgens een failed breakout bleven afzonderlijk in het archief staan ([real-app-events.json](reports/real-app-events.json)).

Het doel is automatische marktontdekking en signalering. Deze cijfers bewijzen geen volledige 500-assetdekking, geen 24/7 beschikbaarheid en geen voorspellende winstgevendheid. Scores en Fibonacci/cycluslabels blijven hypotheses; echte SAND/BEAM/AIN controles en out-of-sample kalibratie zijn nog niet uitgevoerd.

## Resterende beperkingen

- Werkelijke Windows-uitvoering hangt ook af van WebView2/runtimebeschikbaarheid en toegankelijke publieke providerhosts.
- Het native programma is geen cloudservice; als het venster/de bijbehorende processen sluiten, stopt die installatie met marktdata verzamelen.
- De huidige public adapters leveren niet alle derivatives, volledige boekdiepte, consensus of live macro releases. Ontbrekende waarden blijven UNAVAILABLE.
- Browsernotificaties en de lokale outbox zijn geen geverifieerde Telegram- of achtergrond-pushdienst. Native desktopnotificatiedelivery wordt niet verondersteld omdat er een appvenster is.
- Een unsigned Windows-pakket en een daadwerkelijk geteste installer zijn afzonderlijke distributie-eigenschappen; digitale ondertekening/installeracceptatie zijn nog niet aangetoond.
- Het nieuwste volledige automatische lege-watchlist browser-smokeresultaat en final regression total blijven pending in [APP_IMPLEMENTATION_REPORT.md](APP_IMPLEMENTATION_REPORT.md) totdat het parent-integratieresultaat beschikbaar is.


## Broncode-download versus Windows-app

`expansion_hunter_full.zip` bewaart het project en historische database. Het is geen Setup.exe en start de app niet door alleen de ZIP te openen. De source-buildscripts zijn `scripts/build_windows_bundle.py` en `scripts/build_windows_launcher.py`. Officiële CPython/Windows wheels en een echte Windows-linkerbuild blijven nodig voor het distributiebestand. De toolchain-download van de subagent was geblokkeerd; er is geen fictief EXE-bestand aangemaakt om dit te verhullen.

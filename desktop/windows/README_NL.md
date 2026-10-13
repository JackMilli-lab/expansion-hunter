# Expansion Hunter voor Windows

Expansion Hunter opent als een eigen desktopvenster. De Windows-distributie bevat zijn eigen Python-runtime: je hoeft geen Python te installeren, geen terminal te openen en geen websiteadres te typen.

**Windows-uitvoering is nog niet op een echte Windows-pc geverifieerd.** Deze handleiding beschrijft de desktopverpakking in aanbouw. De definitieve ZIP/EXE-bestandsnamen en de eventuele WebView2-installatiehulp worden ingevuld zodra het pakket is gebouwd. De echte marktdata- en browserproeven staan in [het app-rapport](../../APP_IMPLEMENTATION_REPORT.md).

## Openen

1. Pak de volledige Windows ZIP uit naar een map die je kunt bewaren, bijvoorbeeld `Documenten\Expansion Hunter`.
2. Dubbelklik op de meegeleverde **Expansion Hunter-app**. Houd de overige bestanden en runtime-map bij het programma.
3. De cockpit opent in een eigen venster. De scanner start automatisch en ontdekt kandidaten uit het geconfigureerde universum van 500 coins. Je hoeft geen persoonlijke watchlist te maken.
4. Wacht tot de datastatus verbinding en actuele waarnemingen toont. De eerste discovery en het ophalen van chartgeschiedenis kunnen even duren. Uitgesloten en onvolledige assets blijven zichtbaar in de dekkingstellers.

Een persoonlijke watchlist is optioneel: je kunt eigen favorieten extra laten volgen, zonder dat hun onderzoeksscore hierdoor kunstmatig hoger wordt.

## Wat je in de app ziet

- **Discovery:** automatisch gevonden coins, rangschikking, huidige staat en datakwaliteit.
- **Live charts:** candles op 1m, 5m, 15m, 1h, 4h en 1D wanneer de provider de gegevens levert.
- **Liquiditeit:** exacte geobserveerde bid/ask-prijzen, afzonderlijk gelabelde historische structuur en vermoedelijke liquiditeitszones.
- **Indicatoren:** bestaande VWAP/AVWAP, geschatte Volume Profile, steun/weerstand en experimentele Fibonacci/Golden Pocket-overlays.
- **Signalen:** 5m druk tegen weerstand, een intraminute poging, bevestiging na een gesloten 1m candle, daarna eventuele 5m acceptatie en 15m structuur.
- **Data gezondheid:** live verbinding, laatste bericht, ontbrekende informatie en verouderde gegevens.

“500” is het scandoel. In de vastgelegde echte proef leverde de provider 491 assets, waarvan 122 lichte histories en 76 geschikte assets; tien kregen deep analysis. De app doet dus niet alsof alle 500 continu volledig gedekt zijn.

Scores zijn meetbare, ongekalibreerde onderzoeksscores. **85/100 betekent niet 85% kans op winst.** Fibonacci heeft geen aangetoonde voorspellende waarde in dit project. De app observeert en waarschuwt; hij plaatst geen orders.

## Gegevens bewaren en afsluiten

Je persoonlijke gegevens worden buiten de programmamap bewaard in:

```text
%LOCALAPPDATA%\ExpansionHunter
```

Daar blijven de database, instellingen, watchlist, eerdere signalen en logbestanden staan. Je kunt deze map openen door het adres in de adresbalk van Windows Verkenner te plakken. Bewaar de map als je een back-up van je observaties wilt.

Het sluiten van het appvenster stopt de bijbehorende backend en scanner. De database en watchlist blijven bewaard. Bij opnieuw openen worden tijdgevoelige toestanden als verouderd behandeld totdat verse marktdata arriveert.

Je kunt via Windows een snelkoppeling naar de app op je bureaublad maken. Verplaats de EXE niet los van de meegeleverde runtime-bestanden.

## Benodigdheden

- Windows 10 of Windows 11, 64-bit.
- Een internetverbinding voor publieke marktdata.
- Microsoft Edge WebView2 Runtime voor het appvenster. Veel Windows-systemen hebben deze al. Als de app meldt dat hij ontbreekt, is de officiële [Microsoft WebView2-download](https://developer.microsoft.com/microsoft-edge/webview2/#download-section) de betrouwbare bron. Een gebundelde installatiehulp is nog niet bevestigd.

Geen private Binance API-sleutel, trading-accountkoppeling of betaalde data-abonnementen zijn nodig voor de huidige publieke datapaden. Optionele betaalde providers kunnen later alleen via expliciete configuratie worden toegevoegd.

## Als er geen live data verschijnt

Bekijk **Data gezondheid** in de cockpit. Een provider kan tijdelijk onbereikbaar zijn, een coin kan geen betrouwbare instrumentmapping hebben of gegevens kunnen ontbreken. De app vult dat niet op met demoprijzen of verzonnen liquiditeit.

Voor gerichte ondersteuning staan de logs in `%LOCALAPPDATA%\ExpansionHunter\logs`. Deel geen API-token of andere geheimen. Het desktopprogramma gebruikt automatisch een lokale, beveiligde verbinding; je hoeft zelf geen poort, browseradres of token in te voeren.

De Windows-pakketbouw, native startcontrole en definitieve distributiebestanden worden beschreven in [WINDOWS_DESKTOP_REPORT.md](../../WINDOWS_DESKTOP_REPORT.md).

# Onderzoek: winnaars versus vergelijkbare mislukte setups

De onderzoeksvraag is: **welke eigenschappen waren vóór de beweging anders bij de winnaar dan bij tientallen coins die op dat moment bijna dezelfde setup hadden, maar niet uitbraken?** QNT/ZEC achteraf beschrijven is onvoldoende. Dit onderzoek verandert geen handelsregels automatisch en plaatst geen orders.

## Selectie vóór uitkomst

Een cohort bevat het echte universum op een historisch tijdstip, met bron en beschikbaarheidstijd. Het universum moet toen zijn vastgelegd en ook coins behouden die later verdwenen. De huidige top 500 terugprojecteren naar 2022 is geen geldig cohort. Alleen dezelfde historische observatiedatum en marktcontext mogen worden vergeleken.

Per mogelijke winnaar selecteert de module standaard de 50 meest vergelijkbare beschikbare coins op vooraf vastgelegde kenmerken van de basis en compressie. Hierbij zijn de latere koerspaden afgeschermd. Ontbrekende kenmerken worden niet met neutrale of positieve waarden opgevuld. Wanneer onvoldoende geschikte controls bestaan, meldt het rapport dit; het zoekt geen slechter passende coins buiten de gekozen periode om toch het gewenste aantal te halen.

**De module kiest niet vooraf 50 verliezers.** Dat zou de populatie kunstmatig negatief maken en de base rate vervalsen. Eerst worden de gelijkenissen bepaald. Daarna worden binnen dezelfde controlegroep andere winnaars, mislukte uitbraken, beperkte uitbraken en onbekende uitkomsten zichtbaar. De niet-uitgebroken subset wordt afzonderlijk gerapporteerd, met tekorten als er te weinig controles zijn.

## Gescheiden gegevens

`StudyDataset` bevat historische cohorten. Elke observatie bestaat uit een `AssetSnapshot` met uitsluitend toen beschikbare gegevens en een apart `future_bars`-veld voor de uitkomstmeting. Barafsluiting, oorspronkelijke beschikbaarheid, metadata, regime en narratiefbewijs houden hun eigen tijdstempels. Een later opgehaald archief is geen bewijs dat die gegevens destijds precies zo beschikbaar waren.

De bron- en provenancevelden zijn verplichte operatorverklaringen; de software kan onjuiste verklaringen niet zelfstandig waarmaken. De input moet daarom uit een betrouwbare historische leverancier of oorspronkelijk opgeslagen live snapshots komen. De lege template bevat bewust geen verzonnen QNT/ZEC-cases.

Onderzoeksankers moeten exact op de UTC-bargrens van `future_timeframe` liggen. Een intraday ophaaltijd zoals 07:36 mag niet achteraf naar middernacht worden verplaatst: maak een nieuwe cohortgrens en gebruik uitsluitend gegevens die aantoonbaar vóór die grens beschikbaar waren. De module weigert onduidelijke gedeeltelijke toekomstbars, omdat hun hoog/laag vóór het beslismoment kan zijn ontstaan.

## Vooraf vastgelegde uitkomsten

De standaardonderzoeksinstellingen gebruiken een horizon van 30 dagen, een explosieve beweging van minimaal 50% en bevestigde acceptatie boven de vooraf bekende weerstand. Deze grenzen zijn configureerbare onderzoeksdefinities, geen empirisch bewezen optimale drempels. Leg de configuratie vast voordat de testperiode wordt bekeken.

Een incompleet toekomstpad, ontbrekende candles of onbekende delisting-uitkomst wordt gecensureerd. Het wordt niet automatisch als mislukte trade, nulrendement of winst behandeld. De rapportage bevat de gunstigste/ongunstigste beweging en het eindrendement volgens de gekozen horizon, plus aantallen en dekking. OHLCV-uitkomsten zijn geen gesimuleerde orderfills.

## Onderscheidende kenmerken en onafhankelijke toets

Eigenschappen die de selectie bepalen, moeten worden gescheiden van eigenschappen waarvan we willen onderzoeken of ze winnaars onderscheiden. Wie controles selecteert op exact dezelfde spotvraag en relatieve sterkte, kan later niet ontdekken dat juist die twee factoren onderscheidend waren. De module gebruikt vooraf opgegeven, vaste schalen en rapporteert de onderscheidende kenmerken afzonderlijk.

Gebruik `train_end` als grens tussen ontwikkeling en een latere onaangeroerde testperiode. Ontwikkelingsvoorbeelden waarvan de toekomsthorizon over deze grens loopt, worden verwijderd uit de ontwikkelingsvergelijking. Een verschil in de ontwikkelingsset moet dezelfde richting en voldoende dekking laten zien in latere cohorten voordat het geloofwaardig is. Kleine aantallen, gecorreleerde coins en overlappende cases leveren geen onafhankelijk bewijs op.

De featureverschillen zijn beschrijvende associaties, geen causale effecten. Handmatig geselecteerde beroemde winnaars leveren een selectiebias op; rapporteer daarom ook de uitkomstverdeling en scorefouten van het volledige beschikbare cohort. Geen featureweging, scorerangorde of automatische orderfunctie wordt op basis van dit onderzoek stilzwijgend aangepast.

## Gebruik

```bash
expansion-hunter research-schema --output reports/study.schema.json
expansion-hunter research --dataset /path/to/verified-point-in-time-study.json --research-config config/research.json --output reports/matched-study.json
```

Voeg voor een echte validatie een vooraf gekozen UTC `train_end` toe aan een kopie van de onderzoeksconfiguratie. Raadpleeg het geëxporteerde schema voor het actuele invoercontract. De uitkomstmetadata, fouten en tekorten horen bij de beoordeling; een rapport met onvoldoende historische data is geen gevalideerde edge.

Er is in deze workspace nog geen volledige oorspronkelijke historische QNT/ZEC-dataset met tientallen controles beschikbaar. De ingebouwde tests gebruiken uitsluitend herkenbaar synthetische gegevens om selectie, censurering en het weren van toekomstinformatie te controleren. Zij bewijzen geen marktvoordeel.

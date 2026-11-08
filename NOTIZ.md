# Notiz zur Case Study „Finn“

**Annahmen**
- Die Prüfung ist eine **Frühwarnung auf Planbasis** (Stichtag 31.12.2027), kein Vertragsbruch: Alle Verträge stellen auf den festgestellten Jahresabschluss ab. Finn formuliert die Ergebnisse entsprechend.
- Maßgeblich ist die Definition **des jeweiligen Vertrags**. Daran hängen die Fallstricke: Sonderposten zu 50 % (Förderbank) bzw. gar nicht (Konsortialkredit), Gesellschafterdarlehen immer drin (Landesbank) bzw. bei qualifiziertem Rangrücktritt draußen (Konsortialkredit; sonst 3,57 statt 3,37), Aufzinsung und Sondertilgung nicht im Schuldendienst. Die Kennzahlen in der Präambel der Sparkasse beschreiben 2019 und sind keine Covenants.
- Ohne Rundungsregel im Vertrag rechnet Finn mit zwei Nachkommastellen und sagt das dazu.

**Was ich an der Vorgabe geändert habe – und warum**
- **Verträge einmal auslesen statt bei jeder Mail:** Das LLM übersetzt jeden Vertrag in ein Profil mit „Rezept“ (welche Planposten, welche Gewichte, welche Bedingungen) und wörtlichen Zitaten; ein Mensch gibt es frei. Jede Mail rechnet dann deterministisch auf dem geprüften Profil – reproduzierbar und unabhängig davon, ob das LLM gerade verfügbar ist.
- **Plan über Zeilenbeschriftungen lesen, nicht über feste Zellen**, mit Abstimmungsprüfungen. Ein falscher oder unvollständiger Plan führt zu einer präzisen Rückfrage statt zu einem falschen „alles eingehalten“.
- **Resilienz statt Features:** Temporal (Schritte überleben Neustarts, Retries, Warten auf die Nachlieferung im Thread), Absender-Gate, Schleifenschutz, idempotentes Senden. 21 Fehlerfälle laufen automatisiert gegen einen lokalen Mailserver.
- **Datensparsamkeit:** Der Plan geht nie ans LLM; Mailtext wird pseudonymisiert; Zahlen in der Antwort setzt der Code ein und prüft sie nach.

**Wie ich KI eingesetzt habe**
- **In Finn:** gpt-6.1-sol für Vertragsextraktion (mit Prüfpass, Zitatprüfung und Abgleich 95/95 gegen meine handgeschriebenen Referenzprofile), Absichtserkennung und sprachliche Glättung der Antwort – nie für Zahlen.
- **In der Entwicklung:** Claude Code als Pair-Programmer: Analyse der Verträge, Architekturdiskussion, Umsetzung in kleinen, einzeln bestätigten Schritten, Review und Refactoring. Die fachlichen Entscheidungen (Antwortschlüssel, Rezeptmodell, Fehlerverhalten) habe ich vorgegeben und gegen Tests abgesichert.

**Was ich mit mehr Zeit als Nächstes bauen würde**
1. **Fristenkalender aus den Verträgen** (Planvorlage 15.12./31.12./31.01., Covenant-Bescheinigungen) mit Erinnerungen und vorbereiteten Mails, die die Finanzleitung nur noch freigibt.
2. **Plan-Mapping per LLM** für fremde Excel-Vorlagen – nur Beschriftungen ans Modell, Zahlen bleiben im Code, Freigabe einmal je Kunde.
3. **Produktbetrieb:** Postfach im Tenant des Kunden (Microsoft Graph), Mandanten und Lizenzen, Temporal mit Postgres, Tracing über OpenTelemetry/Langfuse.
4. Offene Aufräumarbeiten: [docs/KNOWN_CLEANUPS.md](docs/KNOWN_CLEANUPS.md).

# Alpha Vantage fixtures (SYNTHETIC)

Every file here is invented for tests. None is a real Alpha Vantage reply, and no row is a
real earnings date. The column set (`symbol,name,reportDate,fiscalDateEnding,estimate,currency`)
follows a third-party guide; the real endpoint's output is unconfirmed until Ivan's first
live call (CLAUDE.md rule 6).

- `av_earnings_calendar_good.csv`: a 3-month whole-market calendar as fetched for a run on
  Monday 2026-11-09 (range 2026-11-09..2026-11-16). 52 rows on 11-09..11-12 (four trading
  days), one TSLA row on 2026-11-13 (the 4th trading day ahead), 15 rows after 11-16. Row 5's
  name holds a quoted comma. CRLF line ends.
- `av_earnings_calendar_time_of_day.csv`: the same rows plus a `timeOfTheDay` column.
- `av_rate_limit.json`: the shape of Alpha Vantage's rate-limit message (HTTP 200, JSON).

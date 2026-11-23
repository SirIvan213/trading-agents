REAL Alpaca paper Trading API replies, recorded by `python -m pipeline.probe_broker --live`
on 2026-11-23 (one share of SPY; #196). Each file is `{"what", "method", "path", "status",
"body"}`: the HTTP status and body exactly as Alpaca sent them, except:

- the `/v2/account` reply is left out entirely (its `id` identifies the account);
- every order, leg, group and asset UUID is replaced by `00000000-0000-4000-8000-0000000000NN`
  (the same real id always maps to the same placeholder, so legs still match their entry);
- the probe's own client_order_ids are replaced by `probe-000000000000000000000001` and
  `probe-unknown-0000000000000000`.

Prices, quantities, statuses, order classes and Alpaca's error codes and messages are
unchanged. The legs' client_order_ids, which Alpaca makes as UUIDs, are replaced like the
other ids. tests/test_broker_recorded.py runs tools/broker.py
against them. The synthetic replies in tests/fixtures/alpaca_broker/ stay for the cases
the probe did not record.

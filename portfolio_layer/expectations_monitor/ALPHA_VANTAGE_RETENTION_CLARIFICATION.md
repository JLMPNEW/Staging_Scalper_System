# Alpha Vantage Retention Clarification

Status: `PENDING_WRITTEN_RESPONSE`

The monthly premium subscription increases request capacity. Under
`../MONITOR_LEVELS_RETENTION_AMENDMENT_2026-07-31.md`, normalized snapshots are retained locally
under `provisional_user_authorized`; raw payloads remain prohibited. A written response would decide
whether this provisional classification can become `confirmed` and clarify post-cancellation
retention. Nothing in this document is sent automatically.

## Request to Alpha Vantage

Send to Alpha Vantage Premium support from the account email:

> I use an Alpha Vantage Premium API key for my own private investment research and monitoring. I
> do not redistribute, display, sell, or provide the data to any third party. Please confirm in
> writing whether my subscription permits me to:
>
> 1. store raw API responses locally for audit and reproducibility;
> 2. store normalized point-in-time daily snapshots of analyst estimates and revisions;
> 3. calculate and retain internal derived signals, alerts, and research results from those data;
> 4. continue retaining the raw, normalized, and derived records after cancelling the subscription;
> 5. use those records solely to make decisions for my own brokerage account.
>
> Please identify any required deletion period, storage limit, attribution requirement, exchange
> entitlement, or restriction that differs by endpoint, particularly `EARNINGS_ESTIMATES`.

## Required adjudication

Record the response date, responder, exact permitted uses, endpoint scope, deletion obligations, and
the response-file SHA-256. Do not place correspondence containing account or credential values in
the repository.

Only after a documented affirmative response may the entitlement status become `confirmed`. Even
then, Alpha remains a secondary estimates source until a new sealed coverage run clears the 90%
floor or the role policy is amended prospectively. A negative response triggers the controlled
provider/date purge and invalidation process.

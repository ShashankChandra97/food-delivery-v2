# Workbook Audit and Implementation Map

Source: `BUAN6385_Wk4_SpecPack_Template.xlsx`, read in full on September 21, 2026. All eight sheets, all nonempty cells, and example-row formatting were inspected. The source workbook was not changed. Workbook content is a specification reference, not authorization to upload assignments, contact people, or submit anything to Canvas.

## What the first workflow missed

The original three-node workflow only returned calculated flags. It did not create an order record, lock a driver, deduplicate an event, verify a vendor against a baseline, geocode an address, call a map provider, choose actual alternatives, persist logs, or process the delivery lifecycle. It also treated missing traffic as zero, permitted coercion of blank coordinates, and could imply driver assignment before customer and restaurant confirmation. The v2 package replaces those behaviors with a local executable demo, n8n orchestration, and transactional SQLite state.

## Sheet-by-Sheet Coverage

| Sheet and cells | Specification | Implemented behavior and limits |
| --- | --- | --- |
| START HERE B7:B10 | Team and Food Delivery API context | Workflow, control page, and documentation use this process. |
| START HERE B13:B15 | Orange italic rows are examples; unspecified facts should not be invented | Invoice examples excluded. Unspecified settings are explicitly labeled demo assumptions below. |
| START HERE B18:B21 | Client order API; customer and restaurant location; ongoing route and price decisions | POST webhook, active restaurant baseline, address verification, ten event branches, route reevaluation. Prices are frozen after customer confirmation. |
| START HERE B22 | Multiple maps; restart failures; more than three failures leads to human review; disaster recovery | Two primary attempts then two backup attempts per map operation. Four failures stop routing for review. n8n retries connection failures up to four attempts. SQL transaction and same-event retries prevent duplicate state changes. Nightly local SQLite backup with integrity check; off-machine disaster recovery remains infrastructure work. |
| START HERE B23 | Dashboard, end states, exceptions, summaries | Orders, exceptions, run traces, local inbox, metrics, and operations views. |
| START HERE B25:B31 | Classroom submission checklist | Read as document instructions only. No Canvas submission or claim that missing red-team/disclosure work was completed. |
| 1 Trigger A6:F6 | New order; unique order ID; never-fired order stays unprocessed | `order_placed`; unique SQL order key plus unique event key; no-order alert after configured quiet period. Cannot detect a particular missing upstream order without reconciliation against the real client app. |
| 1 Trigger A7:F7 | Restaurant accepts; dedupe before route job | `restaurant_accepted`, restaurant identity match, duplicate guard, no duplicate route job; missing acceptance timeout goes to dispatch inbox. |
| 1 Trigger A8:F8 | Pickup/driver acceptance; first driver locks; timeout escalates | Separate `driver_accepted` and `driver_picked_up`; SQLite `BEGIN IMMEDIATE` guards first-claim assignment; driver and pickup timeouts. Real driver authentication is not part of the local demo. |
| 2 Data Contract A6:F6 | Required geocodable street/city; quarantine and prompt | Demo address book or configured geocoding adapter. Invalid address persisted as QUARANTINED with re-entry notice. `address_updated` permits correction before assignment. Coordinates sent by a customer cannot bypass validation. |
| 2 Data Contract A7:F7 | Vendor ID must be active in baseline; reject/support notice | Server-owned `config.json` restaurant registry, not the caller's `restaurant_active` flag. Invalid vendor is rejected and logged with a support inbox notice. |
| 2 Data Contract A8:F8 | Timestamp within five minutes of UTC server time | Timezone-aware parsing and absolute 300-second bound. Exact duplicate event returns its original result even later; a new event with an old timestamp is rejected. Later lifecycle events use `event_timestamp`, not the original order time. |
| 2 Data Contract A9:F9 | Numeric nonnegative traffic delay; historical median fallback and review | Traffic comes from map adapter. Missing, negative or nonnumeric delay uses configured historical median and requires review. Zero is valid. The median is a demo assumption until historical data is available. |
| 3 Decision Table A6:F6 | Step 1 RULE: validate address | Verified address gate; first entry in `decision_trace`. |
| 3 Decision Table A7:F7 | Step 2 RULE: approved service boundary | Configured geographic boundary and persisted emergency switch; boundary-margin review; outside boundary blocks automatic dispatch. |
| 3 Decision Table A8:F8 | Step 3 MODEL: current traffic and historical patterns | Provider estimate with timestamp, provider identity, and missing-traffic handling. Demo estimates are fixtures. No fabricated model confidence: `confidence` is null. |
| 3 Decision Table A9:F9 | Step 4 RULE: road closures and vehicle restrictions | Exclude adapter-blocked/restricted and driver-reported routes. Live deployment needs a validated independent closure/restriction feed, described below. |
| 3 Decision Table A10:F10 | Step 5 MODEL: compare feasible routes | Compare provider alternatives by ETA, prefer those within threshold. Demo compares three distinct fixture alternatives. |
| 3 Decision Table A11:F11 | Step 6 RULE: alternative if preferred route is blocked or too slow | Explicit alternative-needed trace and selection; no safe route means human review, never a false assignment. |
| 3 Decision Table A12:F12 | Step 7 MODEL: multi-stop routes, windows, capacity | Evaluates two possible stop orders for a same-restaurant peer with sharing consent. Checks both ETAs, promises, detour limit, vehicle match and capacity two. Shared assignment locks both orders in one transaction. Remaining shared stops are recomputed during delivery. |
| 3 Decision Table A13:F13 | Step 8 RULE: base, distance, time, tolls, surge | Transparent server-owned fee formula, USD, two-decimal rounding, unknown toll price blocks confirmation; caller cannot set a fee or surge. Confirmed price does not change on later route updates. |
| 3 Decision Table A14:F14 | Step 9 HUMAN: judgment on exceptions | Local dispatcher form records reviewer, reason and decision. Cannot approve unverified addresses, disabled areas or absent safe routes. Review is actual persisted state, not a boolean suggesting someone was notified externally. |
| 3 Decision Table A15:F15 | Step 10 MODEL: re-evaluate while delivering | Fresh driver-location events and minute scheduler recompute route/ETA. Location age is not refreshed by the scheduler. Stale driver location stops automatic rerouting and escalates. |
| 4 Actions A6:F6 | Create order; cancel before preparation/dispatch | Real SQL order insertion and gated cancellation. |
| 4 Actions A7:E7 | Confirm location; update before dispatch | Validated address saved; correction event before assignment. |
| 4 Actions A8:E8 | Display options; change before confirmation | Standard/shared options in JSON and control page; selected quote ID and expiry required. |
| 4 Actions A9:E9 | Dynamic fee; recalculate before confirmation | Versioned quotes with expiry and server pricing; confirmed fee frozen. No real payment or charge occurs. |
| 4 Actions A10:E10 | Confirm customer selection; policy controls cancellation | Explicit customer confirmation and cancellation gate. Workbook leaves post-preparation refund/cancellation policy undefined, so those cancellations are held for operations. |
| 4 Actions A11:A13 | Assign driver, shared route, send stop sequence | Atomic driver/group assignment, persisted stop order, driver route in local inbox. No real driver app is connected. |
| 4 Actions A14:A16 | ETA updates, rerouting, significant-delay notice | Event and scheduled route checks; updated route and local customer/dispatch notifications; wrong route recommendations remain in the audit. |
| 4 Actions A17 | Delivered state | Assigned driver, prior pickup and delivery-proof reference required; terminal transition logs actual duration and missed promise. Earlier shared stops must be completed first. |
| 5 Failure Modes A6:E6 | Live API unavailable | Four bounded attempts across primary/backup; both failed means review and paused routing. |
| 5 Failure Modes A7:E7 | Stale traffic | Freshness gate; stale primary demo scenario uses backup. Live API response receipt time is NOT proof of the underlying incident feed's freshness. |
| 5 Failure Modes A8:E8 | Confidently wrong blocked route | Driver route report logs original route ID, excludes it, recalculates and sends dispatch inbox notice. |
| 5 Failure Modes A9:E9 | Unsuitable vehicle route | Vehicle profile and restriction exclusions; report becomes a persisted per-order exclusion. Network-wide road-segment bans need the real road-data model. |
| 5 Failure Modes A10:E10 | Wrong serviceability near boundaries | Configured margin sends order to human; boundary and emergency controls remain server-owned. Production boundaries must be approved by operations. |
| 5 Failure Modes A11:E11 | Alternative misses promised time | Compare predicted arrival and actual delivery against persisted promise; notify customer/dispatch locally and log lateness. |
| 6 Observability A6:C6 | SQL log every order; dashboard; custom views; emergency area disable | SQLite orders/events/runs/API calls/inbox/alerts/outbox. Filters and named local saved views. Region switch pauses existing nonterminal orders and blocks new dispatch. Re-enabling does not silently approve held orders. |
| 6 Observability A7:C7 | Smart alerts, regional issues, request overload, Loki | Regional failure spike, request limit, no-order alert, sustained failure alert, SQL-backed Loki retry queue. These are signals, not a wildfire detector or production DDoS defense. Loki remains unconfigured until an endpoint is supplied. |
| 6 Observability A19:C19 | Escalation over 25% for three days | This row is orange/italic TEMPLATE EXAMPLE, not a team-authored requirement. Adopted as a clearly labeled additional useful metric, evaluated over three completed UTC days. |
| 6 Observability A20:C20 | Failure rate over 25% for two hours | Evaluated using eight populated 15-minute buckets, each strictly above 25%. No data is unknown, not zero. One short spike does not satisfy two-hour persistence. |
| 7 Red Team Notes A4:C7 | Reviewing team and findings | Reviewing team is blank; sole finding is an orange italic invoice example. No completed food-delivery red-team review is claimed. The automated adversarial tests are listed separately below. |

## Unspecified Details and Explicit Demo Decisions

The workbook does not provide production service boundaries, baseline restaurant data, a historical median, freshness limit, fee values, ETA limit, driver timeout, vehicle capacity, significant-delay threshold, real API credentials, messaging destinations, target systems for most actions, or complete owner/reversal rules. The action sheet's step numbers also differ from the decision table (for example pricing refers to step 7 although pricing is decision step 8); v2 follows action intent and the decision table's ten step numbers.

All operational constants are in `config.json`. Demo defaults: Richardson rectangle; REST-001; five-minute map/quote freshness; eight-minute historical delay; 55-minute maximum ETA; ten-minute restaurant/driver timeouts; five-minute driver-location freshness; 0.25-mile boundary margin; two orders per shared route; ten-minute shared detour and promise buffer; $3.99 base + $0.95/mile + $0.08/route-minute + toll, multiplied by configured surge; 15% shared discount; 60 requests/minute; five-run minimum regional alert; 24-hour quiet-trigger alert. These are not workbook facts.

Failure rate = failed nonduplicate business events / nonduplicate business events. Failed includes HTTP 4xx/5xx, quarantine/rejection and technical provider failure; dispatch review alone is counted as escalation. Escalation rate = events returning HUMAN_REVIEW / nonduplicate business events. Maintenance/admin/error-log operations are excluded from these denominators. Completed orders counts distinct SQL orders with DELIVERED state. Repeated delivery events therefore cannot inflate completion counts.

## What Remains Before Real Operations

The requested local demo is executable. It is not a production delivery platform. Maps are labeled fixtures until `mode=live` plus keys are configured. Google/HERE adapters are supplied but not verified against paid/live credentials. Fresh map response time cannot establish source incident freshness; independent road-closure timestamps, segment exclusions, truck profiles, provider quality evaluation and geographic boundary approval are still needed. Sharing is deliberately limited to two same-restaurant orders; no fleet optimizer, cross-restaurant dispatch, or network-wide restriction database is claimed.

Notifications are persisted and visible in the local inbox. There is no SMS/email/push delivery, real restaurant app, real driver app, payment, or dispatch workforce connection. Local reviewer labels are audit labels, not identity verification. The support server binds only to localhost and rejects cross-origin browser writes. Before remote use add authenticated actor-specific webhooks/admin access, TLS, proper secrets, per-client ingress limits, and database capacity suitable for concurrency.

Nightly local backups survive application corruption, not loss of the machine. Off-machine encrypted copies, restoration drills, service supervision, retention policies, access control, independent outage monitoring and upstream order reconciliation remain deployment responsibilities. Requests rejected before n8n starts (bad JSON, network outage, wrong URL) may not appear in the business SQL audit; inspect n8n/access logs for those. If SQL itself is unavailable, the API returns an error and does not falsely claim persistence. n8n execution history is the fallback record; no system can write its own SQL outage into an unavailable database.

## Verification

`test_service.py` exercises timestamp edges, bad addresses/vendors, duplicate and conflicting events, concurrent duplicate requests, competing drivers, lifecycle gates, route fallback, blocked/restricted routes, frozen confirmed prices, shared capacity, route reports, missing-trigger timeouts, region controls, SQLite restart persistence and alert duration. Integration tests execute the actual n8n Switch/HTTP/Code nodes against this service; live credentials and real external notifications are intentionally excluded.

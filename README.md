# Food Delivery v2: Start Here

This is the updated implementation for all eight sheets of your spec pack. It runs a local demo with actual SQL storage and a browser control page. It uses your installed n8n at port 5678 plus a small local support service at port 8787. The original workflow is preserved.

## Vercel dashboard

Deploy this repository from its root with Vercel's **Other** framework preset. The build copies the exact `dashboard.html` used locally to `public/index.html`, so the Vercel root shows the same interface. When you open the hosted page on the computer running this project, it calls the support service on `127.0.0.1:8787`, which uses your existing n8n at `localhost:5678` and local SQLite data. Start n8n and run `START.command` before using the hosted controls.

The page loads for everyone, but visitors on other computers cannot reach services on your computer through `localhost`. They see a connection message and cannot view your local orders or run the workflow. To make the complete dashboard work for everyone, the support service, database and n8n must be reachable from the internet; this repository does not set up a second n8n instance.

## Open It

1. Start your existing local n8n at [http://localhost:5678](http://localhost:5678). Import `food-delivery-v2-errors.n8n.json` and `food-delivery-v2.n8n.json` there if they are not already present, then publish both workflows.
2. Double-click `START.command`. It starts only this project's SQLite support service and opens [Food Delivery Control](http://localhost:8787).
3. In Food Delivery Control, use **Try a delivery**, then **New order**, then **Send this event**. The default target is your n8n production webhook.
4. The response offers the next event. Click **Confirm standard**, then **Send this event**; continue with restaurant acceptance, driver acceptance, pickup, route update and delivery. The UI prepares fresh event IDs and timestamps for each new event.

The n8n editor is at [Food Delivery v2 in your n8n](http://localhost:5678/workflow/foodDeliverySpecV2). `START.command` never creates, imports into, publishes, or starts an n8n instance. It only manages the support service. Its log and PID are in `data/service.log` and `data/service.pid`.

Importing on another n8n instance: import `food-delivery-v2-errors.n8n.json` first, then `food-delivery-v2.n8n.json`. In workflow settings, select **Food Delivery v2 - Workflow Error Log** as the error workflow if IDs changed. For CLI import, `IMPORT-ALL.n8n.json` contains both workflows. The original three-node workflow is not the v2 workflow.

## Exact POST URLs

**Open the dashboard at [http://localhost:8787](http://localhost:8787).** The URLs below are API endpoints for POST requests. Pasting one into a browser address bar sends GET and shows an n8n 404 instead of the dashboard.

Production, after publishing:

```text
http://localhost:5678/webhook/food-delivery-serviceability-v2
```

Test, after clicking Execute workflow:

```text
http://localhost:5678/webhook-test/food-delivery-serviceability-v2
```

Opening either URL in the address bar sends GET, not POST. Use the control page, Postman, or the helper. Test webhooks are temporary listeners; production is the reusable endpoint. See [n8n webhook documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/).

## Easy Calls

The browser control page is the simplest option. It includes scenario selection, JSON editing, POST, exact retry, curl copying and guided next-event payloads. It forwards requests to n8n; it does not bypass the workflow. **Retry same event** preserves the exact body and event ID. **New order** creates a different order and fresh timestamp.

Alternatively, double-click `send-demo.command` to make a production POST. From this directory:

```sh
./send-demo.command
./send-demo.command --test
./send-demo.command --retry
```

The helper generates a fresh order ID and timestamp, avoiding the five-minute stale-request problem. `--retry` resends `last-request.json` exactly. Use it only to retry that request, not to create another order.

For Postman, import `Food-Delivery-v2.postman_collection.json`. Run requests 1 through 7 in order. The collection generates current timestamps and event IDs and captures the quote ID. Set a new `orderId` collection variable before beginning a new lifecycle. For an exact retry, reuse the request's event ID and body rather than regenerating its pre-request values.

## What the Canvas Does

The main path is **POST -> normalize field names -> route event type -> corresponding SQL transaction -> normalize HTTP status -> response**. Ten visible branches handle order placement, address correction, customer confirmation, restaurant confirmation, driver locking, pickup, live route updates, blocked-route reports, delivery and cancellation. Unsupported events go to a rejection/audit branch.

The minute schedule rechecks active routes with fresh driver locations, escalates missing confirmations/pickups, evaluates regional and sustained alerts, and attempts Loki export if configured. The nightly UTC schedule makes and verifies a local SQLite backup. The companion Error Trigger workflow records failed production executions when the support service is reachable. n8n execution history preserves failures if the support service is down.

The ten workbook decisions are implemented inside one local SQL transaction where needed. Keeping the event record, order state, driver lock, notification inbox and audit commit together prevents two simultaneous requests from both assigning the same order. The response's `decision_trace` names and explains each RULE/MODEL/HUMAN step. Native n8n nodes orchestrate the service; this is not a self-contained JSON-only workflow.

## Request Contract

For new orders: `event_id`, `order_id`, `event_type: order_placed`, `customer_address`, `restaurant_id`, `order_timestamp`. Use `REST-001` and `1200 E Campbell Rd, Richardson, TX` for the normal demo. Workbook spellings `Customer_address`, `Restaurant_id`, `Order_timestamp` are accepted. Restaurant address and active status are loaded from the server baseline. Traffic comes from the mapping adapter, not an untrusted client value.

For later events: reuse `order_id`, supply a NEW `event_id` and current `event_timestamp`. `customer_confirmed` needs the latest `quote_id` and option; restaurant acceptance needs the matching restaurant ID; driver events need the assigned driver ID; location updates need finite lat/lng; completion needs a nonempty `delivery_proof` reference. A repeated event ID with different data returns 409.

| Status | Meaning |
| --- | --- |
| 200 | Event accepted or exact duplicate returned without repeating actions |
| 202 | Persisted and waiting for dispatcher review |
| 400 / 422 | Invalid input, stale timestamp, unknown vendor or quarantined address |
| 403 | Attempt to send an admin decision through the public event webhook |
| 404 | Unknown order, or n8n webhook is not registered |
| 409 | State conflict, competing driver, reused event ID with different data, or expired quote |
| 429 | Local demo request limit exceeded |
| 503 | SQL/support service unavailable; do not assume the event committed, retry the same event ID |

## Logs and Controls

Persistent database: `data/delivery.sqlite3`. It is actual SQLite SQL, independent of n8n's internal database. Orders/events, every processed run, map attempts, decisions, notifications, alerts and Loki export state persist across restarts. Do not delete the `data` folder to restart the app.

The control page provides **Orders**, **Exceptions**, **Run log** with detailed traces, **Local inbox**, and **Operations**. Operations can disable a region, record dispatcher decisions, refresh an order's route, and run timeout checks. Saved filter views stay in that browser's local storage. The inbox represents real local writes; it does not claim delivery to a real customer or driver device.

Failure metrics exclude exact duplicates and maintenance/admin events. A 25% failure threshold must be exceeded for the full two-hour evaluation window; 25% exactly does not trigger. The three-day escalation threshold is adopted from a template example and labeled as an additional metric in `SPEC_AUDIT.md`. Empty periods remain unknown.

## Demo Scenarios

Choose normal delivery, primary outage, both APIs unavailable, stale primary, preferred road blocked, all roads blocked, vehicle restriction, invalid traffic delay, heavy traffic, boundary review or invalid address. None calls paid map services. To test sharing, first get an order with `allow_shared: true` to AWAITING_DRIVER. Place a nearby second order, select its shared quote, confirm the restaurant, then accept it with a driver. Both orders lock together; pickup must be recorded for each and delivery follows the returned stop order.

The hardcoded demo boundary, fees, sample vendor, historical median and timeouts are explicitly replaceable assumptions. See the complete per-sheet/cell mapping in `SPEC_AUDIT.md`.

## Optional Live Connections

Keep `mode: demo` for the requested local demo. For later integration, set `mode: live` in `config.json`, approve real boundaries/vendor data, and provide `GOOGLE_MAPS_API_KEY` and `HERE_API_KEY` in the service's environment before starting it. Do not put keys in request bodies or the workflow JSON. Live mode never substitutes fixtures when providers fail. Live adapters need credentialed end-to-end validation and an independently verified closure/freshness feed before real dispatch.

Adapters follow [Google Routes computeRoutes](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes), [HERE Routing v8](https://docs.here.com/routing/reference/routing-api-v8-calculateroutes), and [HERE traffic routing](https://docs.here.com/routing/docs/routing-v8-traffic-in-routing). The demo does not simulate a trained model's accuracy or claim provider confidence scores.

To export audit logs, set `LOKI_PUSH_URL` to your Loki `/loki/api/v1/push` endpoint and optionally `LOKI_AUTHORIZATION` to the required Authorization header. Start the service from that same environment. Pending redacted audit records are sent by the schedule; outages keep them in SQL with retry backoff. No full addresses, precise coordinates, provider keys or HTTP headers are exported. This follows the [Loki push API](https://grafana.com/docs/loki/latest/reference/loki-http-api/). Already-old queued data may be rejected by your Loki retention/ingestion policy; check `loki_outbox.last_error` before a backfill.

Real notifications require an authenticated, idempotent customer/driver/support integration. They are intentionally local until you choose and authorize a destination. A real deployment also requires role-based authentication, TLS, ingress limits, off-machine backups, log retention, independent uptime monitoring and upstream reconciliation. Do not expose the demo directly to the Internet.

## Recovery

`START.command` starts the support service and opens the control page. Your existing local n8n can reach `127.0.0.1:8787`; Docker/cloud n8n cannot use that address to reach your Mac without different networking. Keep your n8n instance running for its schedules and webhook.

Nightly verified SQL backups are placed in `backups/`. To make one manually, run `python3 backup.py`. To restore safely, stop the support service, choose a backup, then start `python3 service.py --db /absolute/path/to/the/restored-copy.sqlite3`. Restore to a separate file; keep the original. Backups on the same machine are not complete disaster recovery.

If port 8787 is occupied by another app, do not terminate it. Change the support port and all n8n service URLs together. If the control page returns 404 for a production POST, check that you opened and published v2, not the old workflow. If a quote expires, Operations -> Refresh route generates a new quote, or an expired confirmation returns the refreshed quote for reconfirmation. If logs cannot be written, the service fails the request rather than reporting fake success.

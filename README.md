# BIG W Pokémon TCG monitor

Checks BIG W every 15 minutes, one second past each quarter hour (hh:00:01, hh:15:01, hh:30:01, hh:45:01), using GitHub Actions, for Pokémon TCG products **sold by BIG W itself** and posts to Discord. Marketplace sellers are excluded, and so are drop-shipped knock-offs such as "Jenjo Games Pokemon Cards". It works the same way as `jb-monitor`.

- **Alerts**: an embed when a product is **newly listed** or comes **back in stock**. Each one shows the price (with the original price and the Clearance/Special tag when discounted), status, per-customer limit, where you can get it (Delivery, Pick up, In stores), and an image.
- **Live board**: one card for every product currently available, ordered Pre-orders → In stock. It's edited in place on every run, so you pin it once. A message holds 9 cards plus the header. When the count crosses 9, 19 and so on, the board is reposted, and you re-pin it.

By default it tracks sealed card product only (boosters, blisters, collections, ETBs, tins). Set `INCLUDE_ACCESSORIES=true` to also track Pokémon sleeves, binders, portfolios and deck boxes.

## Statuses

| Status | Meaning on BIG W | Alerts? |
|---|---|---|
| 🟣 Pre-order | Pre-order open | yes |
| 🟢 In stock | "Add to cart" is shown | yes |
| ⚫ Sold out | "View item" only, or it has dropped out of BIG W's results | no (alerts when it returns) |

**Available** shows how you can get it. *Delivery* and *Pick up* are BIG W's online options. *In stores* means it's a regular in-store line, not an online-only item. It doesn't confirm that your local store has it on the shelf.

The limit is shown only when it's tighter than BIG W's default of 24 per order.

## Setup

1. **Create Discord webhooks.** Go to Channel settings → Integrations → Webhooks → New Webhook → Copy URL. You need one for alerts, for example `#bigw-alerts`. A second one for the board, for example `#bigw-stock`, is optional.
2. **Push this folder to its own public GitHub repo.** It must be public because the runs wait up to about 7 minutes each, which is around 20k runner-minutes a month. That's free on public repos only.
   ```bash
   git remote add origin https://github.com/<you>/bigw-monitor.git
   git push -u origin main
   ```
3. **Add the settings** under repo → Settings → Secrets and variables → Actions.

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `DISCORD_WEBHOOK_URL` | Alerts webhook URL (required) |
   | Secret | `DISCORD_BOARD_WEBHOOK_URL` | Board webhook URL (optional; defaults to the alerts webhook) |
   | Secret | `BIGW_PROXY` | Proxy for BIG W requests, `host:port:user:pass` (optional; see below) |
   | Variable | `DISCORD_PING` | Who to ping on alerts: `<@YOUR_USER_ID>` or `<@&ROLE_ID>` (optional) |
   | Variable | `INCLUDE_ACCESSORIES` | `true` to include accessories (optional) |

4. **Start it.** Go to Actions → *BIG W Pokemon monitor* → Run workflow. The first run records what's listed **without** alerting, then posts the board. Pin the board message. To preview an alert, run the workflow again with **Send a sample alert** ticked.

## Do I need a proxy?

Probably not. The monitor uses BIG W's search API (`api.bigw.com.au/search/v1/search`), which, unlike BIG W's product pages, isn't behind Akamai's bot check. It answered plain requests during testing. If GitHub runs start failing with HTTP 403 or timeouts, add `BIGW_PROXY` as a secret. It accepts `host:port:user:pass`, `host:port`, or a full `http://user:pass@host:port` URL. Only BIG W requests go through the proxy. Discord requests never do.

## How it works

- **Timing:** this is the same as `jb-monitor`. The cron fires 7 minutes early, and `--at-quarter` sleeps until exactly `:00:01`/`:15:01`/`:30:01`/`:45:01`. Manual runs check straight away.
- `monitor.py` asks the search API for everything in the *Trading Cards* category with the server-side **Sold By = BIG W** filter, which is about 2 pages. It then keeps items with "Pokémon" in the name or brand. Sealed product must also be Pokémon-branded, and accessories are kept only when `INCLUDE_ACCESSORIES` is on.
- BIG W's index mangles "é" (`Pok�mon`); titles are repaired before display.
- `state.json` remembers every product seen and its status. The workflow commits it back only when something changed.
- An alert fires when an article appears for the first time while purchasable, or goes from sold out/missing back to purchasable. If the API returns nothing at all, the run is skipped instead of marking everything sold out.

## Running locally

Copy `.env.example` to `.env` and fill in a webhook. Use a **separate test channel** while trialling.

```bash
python monitor.py --dry-run          # fetch + print, no Discord, no state written
python monitor.py --test-alert       # send one sample alert
python monitor.py --loop 15 --state state.local.json   # near-live polling, logs to logs/
```

Loop mode polls every ~15 s (±20%) and backs off on errors (up to 5 minutes). It writes `logs/polls.csv` (every request: success, response time, product count) and `logs/alerts.csv`. If you run it permanently on an always-on machine, turn off the GitHub schedule so alerts don't arrive twice.

## Caveats

- GitHub's scheduled runs are best-effort and occasionally late or dropped. GitHub also disables schedules in public repos after 60 days with no activity. Check the Actions tab if alerts stop.
- BIG W hasn't had a Pokémon pre-order listed during testing, so the pre-order detection reads the `fulfilment.preorder` field the site uses, but hasn't been seen live yet. A pre-order would at worst show as 🟢 In stock and still alert.
- This relies on BIG W's unofficial frontend API. If they change it, runs fail visibly in the Actions tab (GitHub emails you on failed runs).

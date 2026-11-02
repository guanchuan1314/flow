# Flow — Broker CRM with the MetaTrader 5 Manager API integrated

**Flow** is a broker CRM written in Python (FastAPI, SQLAlchemy, PostgreSQL) with the **MetaTrader 5 Manager API
integrated**, through MetaQuotes' official **MT5Manager** Python package (Windows only). It manages clients,
KYC, trading accounts, deposits and withdrawals, IBs, copy trading and PAMM, and it also manages the MT5
server's own setup: account groups, symbols, liquidity providers, routing, pricing markups, server health and
the firewall. Changes made in Flow are written to MT5 first, and what already exists on MT5 is imported.

| Front-end | URL | What it is |
|---|---|---|
| **Public website** | `/` | Marketing site: live prices, account types, platforms, copy trading and PAMM, promotions, IB programme, contact form (enquiries become CRM leads), legal pages |
| **Client area** | `/portal` | Accounts, deposit / withdraw / transfer, MT5 login details and downloads, copy trading, PAMM, referral, verification (KYC), password and 2FA |
| **Staff back-office** | `/admin` | Clients and KYC, MT5 accounts and server setup, payments, trading monitor, copy trading and PAMM, IBs and campaigns, staff, roles, audit log |

There is also a JSON API under `/api` for staff and clients (bearer tokens). Its OpenAPI docs are at `/docs`
outside production only.

**Your brand.** "Flow" is only the default name. Set your broker's name, brand colour, legal name, tagline,
support contacts and regulatory text under **Administration → Branding** (no restart needed), or as defaults
in `.env` (`APP_NAME`, `ACCENT_COLOR`, ...); `setup.ps1 -BrokerName "..."` sets the name at install time.

**A starting point, not a finished product.** Every broker has different needs. Flow gives you the
foundations to fork and shape around your business.

### Tour

![Flow: website, client area and back office](docs/media/flow-preview.gif)

A four-minute narrated walkthrough: [docs/media/flow-intro.mp4](docs/media/flow-intro.mp4). Every page in it is also
in [docs/screenshots/](docs/screenshots/). All data shown is simulated (`run-demo.ps1`).

| Website | Client area | Back office |
|---|---|---|
| ![Home](docs/screenshots/home.png) | ![Client dashboard](docs/screenshots/portal.png) | ![Dashboard](docs/screenshots/admin-dashboard.png) |
| ![Copy trading](docs/screenshots/copy-trading.png) | ![PAMM](docs/screenshots/portal-pamm.png) | ![PAMM fund](docs/screenshots/admin-pamm.png) |

| Automation | | |
|---|---|---|
| ![Trading setup](docs/screenshots/trading-setup.png) | ![Markups re-applied automatically](docs/screenshots/pricing.png) | ![A-book / B-book routing](docs/screenshots/routing.png) |
| ![Trader styles](docs/screenshots/trader-styles.png) | ![Automation rules](docs/screenshots/automation.png) | ![Trade sync](docs/screenshots/sync.png) |

## Contents

1. [Key concept: one MT5 server](#key-concept-one-mt5-server)
2. [Quick start (development and demo)](#quick-start-development-and-demo)
3. [Back-office menu](#back-office-menu)
4. [Features](#features)
5. [Security](#security)
6. [Configuration (.env) and branding](#configuration-env-and-branding)
7. [Production deployment on Windows Server](#production-deployment-on-windows-server)
8. [Architecture (for developers)](#architecture-for-developers)
9. [Tests](#tests)
10. [Before going live](#before-going-live)

## Key concept: one MT5 server

- Flow works with **exactly one MT5 server**. Adding a second one is refused.
- **Live and demo accounts are both on it**, each in their own MT5 groups (for example `real\*` and `demo\*`).
  Give live and demo groups their own login ranges in MT5 Administrator (for example live 1000000-1999999,
  demo 5000000-5999999) so a login tells you which kind of account it is.
- The server is set up in **MT5 → Configuration**: address (`host:port`), manager login and password (stored
  encrypted, never shown again), the server name clients pick in the MT5 terminal, enable / disable and
  **Test connection**. Website prices come from this server and stream live to visitors over a WebSocket (`/ws/quotes`, one shared
  read every 0.5 s however many visitors; `/quotes.json` is the fallback when a proxy blocks WebSockets).
- On a fresh database, the first `migrate` creates the server from `MT5_SERVER`, `MT5_MANAGER_LOGIN` and
  `MT5_MANAGER_PASSWORD` when `MT5_GATEWAY=manager` (in development it creates the simulator). After that,
  edit it only in MT5 → Configuration.
- **Remove from the CRM** (on the Configuration page) deletes Flow's copy of the server's data only; nothing
  changes on MT5. It is refused while accounts have copy-trading / PAMM roles, pending payments or unpaid IB
  commissions.

## Quick start (development and demo)

Requirements: **Windows 10/11 or Windows Server** (the MT5 Manager API is a Windows-only library). Everything
else is installed by `setup.ps1` when missing: Python (the version in `.python-version`, 3.12) and Docker
Desktop. The database is
PostgreSQL 16 in a Docker container; Flow itself runs on Windows.

```powershell
git clone https://github.com/guanchuan1314/flow.git flow; cd flow
.\setup.ps1                          # installs what is missing, creates the database in Docker, .venv, .env
.\setup.ps1 -BrokerName "Northwind"  # the same, with your broker's name (default: Flow)
.\run-demo.ps1                       # MT5 simulator + demo data, opens /admin in the browser
.\run-demo.ps1 -Reset                # start over: deletes the demo database, storage\ and mt5data\
.\run-demo.ps1 -Port 8080            # another port
```

If PowerShell refuses to run scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

`setup.ps1` (development mode):

1. Installs the Python in `.python-version` (with winget, or the official installer from python.org where
   winget is missing, e.g. Windows Server) and Docker Desktop if they are missing (`-NoInstall` stops instead).
   After installing Docker Desktop, Windows may need a restart; start Docker Desktop once, then run the
   script again.
2. Creates `.venv` (again, when it runs another Python version) and installs Flow with the `dev` and `mt5`
   extras.
3. Creates `.env` from `.env.example`, generates `SECRET_KEY`, `CREDENTIALS_KEY` and the database password,
   picks a free port for PostgreSQL (5432, else 55432 and up) and writes `DATABASE_URL`.
4. Starts the database (`docker compose up -d`, container `flow-db-1`, data in the `flow_pgdata` volume, reachable
   from this machine only) and migrates it.

`run-demo.ps1` starts the database container, then `python -m app.devserver`, which:

- uses the built-in MT5 simulator (state kept in `mt5data\mock_state.pkl`) with moving prices and a demo trader,
  so copy trading works live;
- migrates the database and seeds demo data on the first run;
- runs the worker inside the web process (it does that whenever the server is the simulator).

Without Docker, leave `DATABASE_URL` empty and the demo starts a bundled PostgreSQL (`pgserver`, data in
`pgdata\`), or point `DATABASE_URL` at a PostgreSQL of your own.

Demo logins (also printed in the console):

| Who | Email | Password |
|---|---|---|
| Admin (everything) | `admin@demo-broker.com` | `Admin-Pass1` |
| Manager / Sales / Finance / Support / Dealer | `manager@`, `sam@`, `finance@`, `support@`, `dealer@demo-broker.com` | `Staff-Pass1` |
| Client: copies a strategy, has a demo account | `jane.doe@example.com` | `Client-Pass1` |
| Client: strategy provider | `kenji.watanabe@example.com` | `Client-Pass1` |
| Client: PAMM investor referred by an IB | `marco.rossi@example.com` | `Client-Pass1` |
| Client: master IB with commissions | `priya.shah@example.com` | `Client-Pass1` |
| Client: KYC pending | `emma.schmidt@example.com` | `Client-Pass1` |

The demo includes a **sandbox payment / KYC provider** (`/sandbox/provider/...`, never mounted in production)
used by the methods "Card (sandbox)" and "Online verification (sandbox)", so the full provider round trip
(redirect, approve or decline, signed callback) can be tried.

Run the tests (they start a throw-away PostgreSQL with `pgserver`; or set `TEST_DATABASE_URL` to a database
that may be wiped):

```powershell
.venv\Scripts\python -m pytest -q
```

Useful CLI commands (`.venv\Scripts\python -m app.cli <command>`): `migrate`, `check-config`,
`create-admin --email ...`, `seed-demo`.

**Windows notes.** Set `PYTHONUTF8=1` in interactive shells (the scripts and services do); a server's default
encoding may not be UTF-8 (for example cp950). `.env` is always read as UTF-8.

## Back-office menu

Entries appear only for staff with the permission shown.

| Section | Entries (permission) |
|---|---|
| (top) | Dashboard |
| **Clients** | Clients (`clients.view`), KYC queue (`kyc.review`), KYC methods (`kyc.methods`) |
| **MT5** | **Accounts ▸** Trading accounts (`accounts.view`), Account groups (`account_groups.manage`), Trader styles (`accounts.view`) |
| | **Trading setup ▸** Overview, Symbols (`symbols.manage`), Liquidity providers (`liquidity.manage`), Routing (`routing.manage`) |
| | **Automation** (`automation.manage`) |
| | **Import from MT5 ▸** Account groups, Trading accounts (`mt5_servers.manage`), Trade sync (`trading.view`) |
| | **Server tools ▸** Server health (`mt5_health.view`), Symbols & pricing (`symbols.manage`), Inactive accounts (`accounts.cleanup`), Restart components (`mt5_servers.restart`) |
| | **Configuration** (`mt5_servers.manage`) |
| **Finance** | Payments (`finance.view`), Payment methods (`finance.methods`) |
| **Trading** | Open positions, Deals, Orders (`trading.view`) |
| **Copy trading** | MAM strategies, PAMM funds (`copy.view`) |
| **Marketing** | IBs, Commission plans, Commissions, Campaigns, Lead sources (`marketing.view`) |
| **Administration** | Staff, Roles, Departments (`staff.view`), Audit log, Login attempts (`security.view`), IP access rules (`security.ip_rules`), Branding (`settings.branding`) |

Groups marked ▸ open beside the menu on hover (under the group on a phone). The user menu has
**Password & 2FA** and **Sign out**.

## Features

### MT5 integration

- **Read from MT5:** users and accounts (balance, equity, margin), deals, open positions, open orders and order
  history, groups, symbols, gateways and data feeds, routing rules, firewall, server components and their load,
  each account's last terminal connection, quotes.
- **Written to MT5:** users (create, update, delete, archive, passwords, rights), balance operations, trades
  for copy trading (dealer requests), groups (create, update, delete, pricing), symbols, gateways and data feeds,
  routing rules, firewall rules, component and gateway restarts.
- **MT5 first, then Flow.** Every change to MT5 data is sent to MT5 first; Flow saves only after MT5 has
  accepted it, so both sides hold the same settings. An MT5 refusal is shown to the user and nothing is saved.
- **Client details:** Flow is the master copy. Name, email, phone, country, city and address are written to
  all of the client's MT5 users; a suspended or closed client cannot log in to MT5. If MT5 is unreachable, the
  change is kept, the client page shows a warning with **Retry now**, and the worker retries every minute.
- **Rights:** the manager login needs rights for users, accounts, deals / positions, balance operations and
  dealer operations; configuring groups for account groups and pricing. **Symbols, liquidity providers,
  routing, the firewall, archiving and group deletion use MT5's Administrator API**, so the manager login also
  needs administrator access for those.

### Accounts and account groups

- **Account groups** (live or demo) each mirror one MT5 group on the server:
  - **Create** for an existing MT5 group, or tick **Create this group on MT5** to copy an existing group
    (symbols, margin and commission settings) under the new name.
  - **Update** writes margin call and stop-out (and, for demo groups, default leverage and starting balance) to
    MT5. Changes made in MT5 Administrator are read back on each import and when the edit page opens.
  - **CRM-only settings:** name, minimum deposit, leverage choices, spread markup, availability, and for demo
    groups **open automatically for new clients** (the account is created at registration).
  - **Delete** also deletes the MT5 group. Accounts still in it can be **moved** to another group (balances,
    positions and history stay) or **force deleted** (MT5 logins deleted, Flow keeps them archived; the
    group's name must be typed). Refused while accounts have open positions or copy-trading / PAMM roles, while
    IB commission rules refer to the group, or while the MT5 group holds users Flow has not imported.
- **Trading accounts:** open, change leverage, password, group, enable / disable trading, refresh, transfer
  between wallet and account. MT5 passwords are shown once and never stored.

### Import from MT5 and trade sync

The import runs every `MT5_IMPORT_INTERVAL_MINUTES` (15) and on demand (MT5 → Import from MT5). It is
idempotent and never deletes anything.

- **Account groups → MT5 groups.** New groups arrive **disabled** so you can name them and set leverage and
  pricing first. Groups whose name starts with `demo` become demo groups. Groups gone from MT5 are flagged
  "not on server" and disabled. `MT5_SYNC_EXCLUDE_GROUPS` skips manager / system groups.
- **Trading accounts → MT5 users.** Each user becomes a trading account linked to a client, matched by email or
  created (`lead_source = mt5-import`). Users without a usable email get a placeholder `mt5-<server>-<login>@import.invalid`
  address; set the real email before the client can use the portal. Group, leverage and rights are refreshed
  on each run; accounts deleted on MT5 are flagged "not on MT5". Balances and deal history are fetched after
  import.
- **Live sync** (worker): deals are pushed in real time and polled every `SYNC_INTERVAL_SECONDS`; open orders,
  order history and open positions are kept in step. **Polling follows activity:** accounts with an open
  position or order, or a deal in the last `SYNC_ACTIVE_MINUTES`, are polled every pass; idle ones every
  `SYNC_IDLE_INTERVAL_SECONDS`, spread evenly. Results are under **Trading → Deals / Orders / Open positions**.
- **Trade sync** (MT5 → Import from MT5 → Trade sync) re-reads missing history as a background task for all
  accounts or a list of logins and a period: in batches of 50 accounts and one-month slices it adds the deals
  and orders Flow is missing (no duplicates), then refreshes positions, open orders and balances. The page
  shows live progress; tasks can be cancelled, and an interrupted task restarts automatically.

### Trading setup, symbols, liquidity providers and routing

- **Trading setup → Overview** shows the server's setup in the order a trade flows: data feeds, symbols, routing
  rules, gateways. It warns about gaps (an enabled feed not connected, a gateway no rule sends orders to).
  **Sync everything from MT5** syncs all four; a failing part does not stop the others.
- **Symbols:** **Sync from MT5** makes Flow's list match the server (adds, updates, removes). Create (by
  copying an existing symbol, as MT5 does), edit (folder, currencies, digits, contract size, trade and execution
  mode, volumes, spread, stops / freeze levels, swaps, margins, quote and trade sessions per weekday) and
  delete (refused while Flow knows of open positions or orders). **Bulk edit** applies trade / execution mode,
  spread, levels, swaps or volumes to many symbols; a symbol MT5 refuses is reported, the others are saved.
  Holidays, per-group symbol settings and margin rates stay in MT5 Administrator.
- **Liquidity providers:** **gateways** (orders to an LP, optionally its quotes) and **data feeds** (an LP's
  quotes and news), each running a module the server reports (MetaTrader platforms, FX LPs, exchanges, data and
  news feeds). Set the connection (address, login, password), flags, symbols and groups, module parameters and
  symbol translations (can be pre-filled with every server symbol). A new gateway can get its routing rule in the
  same step. LP passwords and secret module parameters are written to MT5 and never stored in Flow (leave
  empty to keep MT5's value). **Restart all gateways / all data feeds** restarts them on MT5; they reconnect
  within a minute. Deleting a gateway is refused while a routing rule uses it.
- **Routing:** the server's rules in MT5's order. MT5 checks enabled rules from the top and the first final
  action decides (confirm, send to a gateway or dealer, requote, reject, cancel); delays and SL / TP clearing
  modify the request and continue. Conditions include group, symbol, login, volume, time, weekday, comment,
  country, leverage, balance, equity, "placed by an Expert Advisor", "placed by a signal" and "deals today";
  conditions the form does not list are kept. Create (top or bottom), edit, enable / disable, move and delete are
  done on MT5 first, then Flow re-reads the rules.
- **Load-reducing presets** (Routing page): each is added at the top **switched off** and never refuses closing
  a position:

  | Preset | Suggested value | Effect |
  |---|---|---|
  | Slow EA SL/TP and order changes | 100 ms | Delay on EA stop and order modifications |
  | Delay accounts with many deals today | 500 deals | 300 ms delay on new orders and changes above that |
  | Refuse EA pending orders after many deals | 1000 deals | EAs can no longer place / change pending orders |
  | Confirm small EA orders on the server | 0.1 lots | Confirmed by MT5 at market instead of the gateway (you take the other side, B-book) |

### Symbols & pricing

**MT5 → Server tools → Symbols & pricing** writes the broker's markups on top of the LP's prices. Masks are
MT5's (comma-separated, `*` for any text, `!` to exclude); the first matching row applies.

- **Auto sync symbols** (off by default; every N minutes, at least 5): syncs symbols from MT5 (so symbols
  created by gateways and feeds appear and changed swaps are seen), then re-applies the markups.
- **Spread markup:** points added to the spread for account-group and symbol masks (MT5's spread difference),
  for example Standard +15, ECN 0. A rule for all symbols also becomes the group's spread markup for spread-share
  IB rebates.
- **Commission:** money per lot, charged on every deal in and out, written as a group commission named `CRM …`.
  Commissions set up in MT5 Administrator are kept and listed.
- **Swap markup:** taken off the LP's swap, long and short. When MT5's swap changes (new LP value or a manual
  edit), the markup is applied to the new value. Symbols with swaps off are skipped.
- **Removing a row** puts MT5 back (the symbol's own spread, no CRM commission, the LP's swap).
- **Website prices:** the symbols streamed live to the website ticker and markets page, in the order you drag
  them into (the first 6 also show on the home page and in the client area). Pick from the server's symbols;
  the name and category default to MT5's description and folder, and decimals always follow the MT5 symbol.
  Saved in the database (live within 30 seconds); until then `PUBLIC_SYMBOLS` in `.env` is used.

### Server health and restarts

- **Server health** (`mt5_health.view`) shows every component (trade, access, history, backup servers) with the
  peaks MT5 reports for CPU, memory, disk, connections and network, checked against MT5's warning levels, with
  24-hour trend lines. It also shows Flow's own load: active / idle accounts, sync pass time, MT5 response
  time. Readings every `HEALTH_INTERVAL_MINUTES` (or **Read now**), kept `HEALTH_KEEP_DAYS`.
- While a component is disconnected, every admin page shows a red banner (to staff with `mt5_health.view`);
  disconnects and reconnects go to the audit log.
- **Restart components** (`mt5_servers.restart`) restarts a component on MT5; restarting the main trade server
  needs its name typed.

### Inactive demo accounts

**MT5 → Server tools → Inactive accounts** (`accounts.cleanup`) lists demo accounts with no activity for N days
and archives or deletes them on MT5.

- **Activity** is kept in Flow: last terminal connection (read every `ACTIVITY_REFRESH_MINUTES` from the
  Manager API's local copy, at no cost to MT5) and last trade. The list is a database query.
- **Always kept:** accounts with open positions or orders, copy-trading masters and followers, PAMM masters.
- **Re-checked on MT5** before each batch (last connection, positions, orders).
- Archived accounts can be restored in MT5 Administrator (archiving needs administrator access). An optional
  **daily run** handles at most 5,000 accounts. The same page sets **MT5's own demo inactivity period** on demo
  groups (0 = never).

### Trader styles

**MT5 → Accounts → Trader styles** classifies each account from the deals stored in Flow (no MT5 request).

- **Set up** (`/admin/trader-styles/setup`): **styles** are checked in order, first match wins (a style without
  conditions catches the rest); **tags** are extra badges, every match shown. Each has a name, colour, on/off,
  conditions (metric ≥, ≤ or between) and, for styles, an optional suggested account group.
- **Metrics** over the period: deals, active days, deals per day; share by EA, desktop, mobile, web, signal and
  dealer; SL / TP and stop-out closes; holding times, positions closed within 2 minutes, intraday / overnight;
  volumes; martingale-like sizing; hedging; symbols traded; night and weekend deals; win rate and profit.
- **Defaults:** Signal follower, High frequency, EA / algo, Scalper, Day trader, Swing / position trader, Manual,
  Mixed; tags Martingale-like, Hedger, Mobile, Consistent winner (Night trader and Large sizes off).
  **Try on an account** shows every metric and which conditions pass.
- **Moving:** accounts whose style suggests a group can be moved (on MT5 first, same currency only). Viewing
  needs `accounts.view`; moving and setup need `accounts.manage`.

### Automation

**MT5 → Automation** (`automation.manage`): rules the worker checks every 5 minutes against Flow's data.
Rules live in Flow only and are created **switched off**.

- **Triggers:** account inactive N days; trader style detected; N deals within H hours; margin level below X %;
  equity below an amount; no deposit N days after registering; KYC waiting N days. Account triggers can be
  limited to MT5 group masks and live / demo.
- **Actions:** email staff (an address, or the client's account manager / sales), email the client, add a client
  note, move to an account group, change leverage, disable or enable trading (the last four on MT5). Messages
  can use `{client}`, `{email}`, `{login}`, `{group}`, `{rule}`, `{detail}`.
- **Safety:** cooldown per rule and target (0 = once ever), at most 200 targets per run, **Preview** (no side
  effects), **Run now**, a run log on the rule and in the audit log.
- **Permissions:** MT5 actions also need `accounts.manage` and `clients.view_all`; emailing clients needs
  `marketing.manage`.

### Payments and payment methods

- **Maker-checker:** the approver must differ from the creator. Wallet withdrawals are held (debited) when
  requested and refunded on rejection; MT5 withdrawals are taken from MT5 on approval. Deposits go to the wallet
  or straight to an MT5 account. KYC is required for deposits and withdrawals.
- **Methods** (Finance → Payment methods): name, icon, currency, min / max, deposits and / or withdrawals.
  - **Manual:** client pays outside Flow, enters a reference and can upload a proof (optionally required).
  - **Custom provider:** Flow creates a unique reference and gets a payment URL, either by POSTing signed JSON
    to the provider's API or from a URL template. Withdrawals are paid out by finance.
- **Provider callbacks:** each method has its own unguessable callback URL (regenerate it if it leaks), signed
  with the shared secret (`X-Signature`, hex HMAC-SHA256 of the body). An optional **callback IP whitelist**
  (IPs / CIDR ranges; the default rule `*` allows any address) refuses calls from other addresses. Every callback
  is logged on the method. KYC methods have the same callback settings.
- **Auto-credit rules:** with auto-credit on, a "paid" callback for the **exact amount** (and the same currency,
  when the provider sends a `currency` field) credits the deposit at once. A missing or different amount or
  currency is **held for finance**. Auto-credit requires the amount field. Callbacks after a staff decision or
  repeated callbacks change nothing. Setting the secret or turning auto-credit on also needs `finance.approve`.
- **Your own provider:** [docs/payment](docs/payment/) explains manual and custom methods (with a video) and has a
  ready-to-run sample mini program to connect any payment provider.

### KYC methods

Clients → KYC methods: several can be configured, **exactly one is active**.

- **Manual:** choose the required documents (photo ID, proof of address, selfie); staff review them in the KYC
  queue.
- **Custom provider:** same integration settings as payment methods. The signed callback approves or rejects the
  client. Setting the secret or activating it also needs `kyc.review`.
  [docs/kyc](docs/kyc/) explains manual and custom methods (with a video) and has a ready-to-run sample mini
  program to connect any verification provider.

### Copy trading, PAMM, IBs and marketing

- **MAM / copy trading:** each master deal is mirrored to followers (sized by the allocation mode; partial closes
  close the same fraction). Execution is idempotent across push and poll; a dealer timeout is reconciled by
  checking MT5 before any retry. Performance fees are settled by the worker. Assumes hedging accounts.
- **PAMM:** pooled accounts with unit (NAV) accounting: deposits buy and withdrawals redeem units at the current
  NAV; performance fee per investor high-water mark and annual management fee, charged in units. NAV snapshots
  and monthly fees run in the worker.
- **IBs:** multi-level commission plans computed from synced deals (first matching rule by priority, split up the
  IB tree), recalculation, payouts (`marketing.payout`).
- **Campaigns:** promo codes with deposit bonuses paid as MT5 credit. **Lead sources:** UTM tracking of
  registrations; website contact enquiries become leads.

### IP access rules

**Administration → IP access rules** (`security.ip_rules`): address or CIDR (IPv4 / IPv6), **block** or **allow**,
scope (whole CRM, back-office, client area, website), description, optional expiry.

- Once a scope has an allow rule, only allowed addresses can use it. Changes that would lock out the person
  making them are refused. Static files and health checks are never filtered; provider callbacks ignore allow
  lists.
- **Also apply on MT5** mirrors the rule to the MT5 server's firewall (kept in step on change, expiry and delete,
  with a **Re-apply** button). The MT5 firewall applies to every connection to the server, whatever the scope in Flow.
  The current firewall can be read on the same page.
- Staff can also have personal IP allowlists.

### Staff, roles and permissions

Permissions are listed in `app/core/permissions.py` (for example `clients.view`, `finance.approve`,
`symbols.manage`). Default roles: **Admin** (everything), **Manager**, **Sales** (sees only assigned clients),
**Business Development**, **Finance**, **Support**, **Dealer** (including symbols, routing and server health).

**Business Development** (`clients.referrals`) is for the sales people who bring in clients and IBs. Each of
them has a referral link on their dashboard. Whoever signs up with it, client or IB, is assigned to them, and so
is everyone below: the clients that IB refers, the clients those clients refer, at any depth (even after
someone below is assigned to another owner). They see those clients, their accounts, open positions, deals and
orders, and nothing else. Under **Clients → Referral links** they can also make tagged links, one per place they
share it (a website, an event, a partner): each shows how many signed up with it, and the client page shows
which link a client came from.

Roles are edited under
Administration → Roles; departments group staff. Staff with `staff.manage` can never grant permissions they do
not hold, edit a role they hold, or change the password / 2FA / roles of someone with permissions they lack.

## Security

| Area | Measure |
|---|---|
| CSRF | Token required on every non-GET request of the back-office, client area and website (form field or `X-CSRF-Token` header) |
| Browser hardening | Strict CSP (`script-src 'self'; style-src 'self'`, no inline code), `frame-ancestors 'none'`, X-Frame-Options, nosniff, Referrer-Policy, HSTS in production, `Cache-Control: no-store` on pages, Host-header allowlist |
| Passwords | bcrypt, password policy, single-use reset links (30 min), responses don't reveal whether an email exists |
| Sign-in backoff | From `MAX_FAILED_LOGINS` failures, each failure makes the account wait 1, 2, 4 … minutes (at most `LOCKOUT_MINUTES`); per-IP rate limit on login / register / reset |
| 2FA | TOTP for staff and clients; each code works once (replay refused); secrets encrypted at rest. `STAFF_2FA_REQUIRED=true` (mandatory in production) sends staff without 2FA to set it up |
| Sessions | Signed HttpOnly cookie (Secure in production), at most `SESSION_HOURS`, idle timeout per audience; checked against the database on each request. Sign-out and password change sign the user out everywhere |
| API tokens | JWTs carry the user's token version; logout, password change, 2FA disable or deactivation revokes all of them |
| Permissions | Role-based; no privilege escalation (see above); changing a client's email needs `clients.change_email` and the old address is notified |
| Money flows | Maker-checker; row locks on payment approval / rejection, wallet postings, PAMM investments and withdrawals, commission payouts, so parallel requests pay once |
| Uploads | KYC / proof files up to 10 MB, read in chunks, type checked by file signature, random names, number of documents capped; callback bodies up to 64 KB |
| Outbound calls | Provider URLs must be `https://` in production and resolve to public addresses; redirects are not followed; provider errors are not shown to users |
| Health endpoints | `/ready` gives details (database, MT5) only to requests from localhost; others get the status only |
| Secrets at rest | MT5 manager password, 2FA secrets and provider secrets are encrypted with a key derived from `CREDENTIALS_KEY` (falls back to `SECRET_KEY`) |
| Audit | Staff and client actions are recorded under Administration → Audit log; login attempts are kept |

## Configuration (.env) and branding

`setup.ps1` creates `.env` from `.env.example` and generates the secrets. Existing values are never
overwritten by the scripts, except the ones you pass as parameters. The most important settings:

| Group | Settings |
|---|---|
| Core | `APP_ENV` (dev / prod), `APP_NAME` (default `Flow`), `PUBLIC_URL`, `SECRET_KEY`, `CREDENTIALS_KEY`, `ALLOWED_HOSTS`, `AUTO_MIGRATE` |
| Database | `DATABASE_URL` (PostgreSQL only, `postgresql+psycopg://user:pass@host:5432/db`); the Docker database: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT` |
| Hosts (prod) | `PORTAL_HOST`, `BACKOFFICE_HOST`: the two names the HTTPS proxy serves |
| MT5 | `MT5_GATEWAY` (mock / manager), `MT5_SERVER`, `MT5_MANAGER_LOGIN`, `MT5_MANAGER_PASSWORD` (first migrate only), `MT5_TIMEOUT_MS`, `MT5_WORK_DIR`, `MOCK_STATE_FILE`, `EMBEDDED_WORKER`, `MT5_SYNC_EXCLUDE_GROUPS` |
| Security / sessions | `STAFF_2FA_REQUIRED`, `ACCESS_TOKEN_MINUTES`, `CLIENT_TOKEN_MINUTES`, `SESSION_HOURS`, `STAFF_SESSION_IDLE_MINUTES`, `CLIENT_SESSION_IDLE_MINUTES`, `MAX_FAILED_LOGINS`, `LOCKOUT_MINUTES`, `PASSWORD_MIN_LENGTH`, `PASSWORD_RESET_MINUTES`, `AUTH_RATE_LIMIT_PER_MINUTE`, `LIVE_REQUIRES_KYC` |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_FROM` (no host: only recipient and subject are logged) |
| Branding (defaults) | `APP_NAME`, `ACCENT_COLOR`, `BRAND_TAGLINE`, `COMPANY_LEGAL_NAME`, `REGULATORY_TEXT`, `SUPPORT_EMAIL`, `SUPPORT_PHONE`, `MT5_SERVER_NAME` |
| Website | `MT5_DOWNLOAD_*`, `MT5_WEBTERMINAL_URL`, `PUBLIC_SYMBOLS` (default price list until Website prices is saved) |
| Business | `BASE_CURRENCY`, `DEPOSIT_INSTRUCTIONS`, `COPY_MAX_OPEN_AGE_SECONDS`, `COPY_LOT_STEP`, `COPY_MIN_LOT` |
| Worker / sync | `SYNC_INTERVAL_SECONDS` (10), `SYNC_IDLE_INTERVAL_SECONDS` (300), `SYNC_ACTIVE_MINUTES` (30), `SYNC_OVERLAP_SECONDS` (120), `ACTIVITY_REFRESH_MINUTES` (5), `HEALTH_INTERVAL_MINUTES` (5), `HEALTH_KEEP_DAYS` (7), `MT5_IMPORT_INTERVAL_MINUTES` (15) |
| Files / logs | `STORAGE_DIR` (KYC documents and proofs; back it up), `LOG_DIR`, `LOG_LEVEL` |

**Branding.** Administration → Branding (permission `settings.branding`) overrides the branding defaults in
the database: broker name, legal company name, tagline, brand colour, support email and phone, MT5 server
name and regulatory text. An empty field falls back to `.env`. Every process picks a change up within 30
seconds. The brand colour is served as CSS variables at `/brand.css` and used by the website, client area and
back-office.

**Production requirements.** `DATABASE_URL` must be PostgreSQL in every mode. With `APP_ENV=prod` the app
refuses to start (and `check-config` fails) unless all of these hold:

- `SECRET_KEY` is set to a random string of at least 32 characters
- `PUBLIC_URL` is an `https://` URL
- `ALLOWED_HOSTS` lists your host names (not `*`)
- `CREDENTIALS_KEY` is set
- `STAFF_2FA_REQUIRED` is true
- `SMTP_HOST` is set
- `AUTO_MIGRATE` is false (run `python -m app.cli migrate` when deploying)
- `MT5_GATEWAY` is `manager`, not `mock`
- `EMBEDDED_WORKER` is not true (run the separate worker service)
- `MOCK_STATE_FILE` is empty

In production the API docs (`/docs`, `/openapi.json`) and the sandbox provider are not mounted.

Keep `CREDENTIALS_KEY` safe and back it up: if it changes, stored manager passwords and 2FA secrets can no
longer be read. On an install that ran without it, set it to the current `SECRET_KEY` value.

## Production deployment on Windows Server

The Manager API is Windows-only, so Flow runs on Windows, next to or near the MT5 server.

### Prerequisites

- Windows Server 2019 or later (or Windows 10/11). `setup.ps1` installs Python (for all users, so the services
  can run it), Docker Desktop, NSSM and Caddy when they are missing.
- An MT5 **manager** account (not a trader login) with the rights listed under
  [MT5 integration](#mt5-integration), including administrator access for the server-setup pages.
- An SMTP account; two DNS names (for example `portal.yourbroker.com` and `backoffice.yourbroker.com`) pointing
  at the server, and ports 80 / 443 open.
- The database: PostgreSQL in Docker (default), or your own PostgreSQL 14+ with `-DatabaseUrl`. With Docker,
  tick **Start Docker Desktop when you sign in** in Docker Desktop's settings and let the server sign in
  automatically after a restart, so the database comes up with Windows; for an unattended server a PostgreSQL
  Windows service or a managed database is the more robust choice.

### 1. Set up

From an elevated PowerShell in the project folder:

```powershell
.\setup.ps1 -Mode prod -BrokerName "Your Broker" `
  -PortalHost portal.yourbroker.com -BackofficeHost backoffice.yourbroker.com `
  -SmtpHost smtp.yourprovider.com -AdminEmail you@yourbroker.com -InstallServices
```

`setup.ps1 -Mode prod`:

1. Installs what is missing (Python, Docker Desktop), creates `.venv` and installs Flow with the `mt5` extra.
2. Creates `.env` if missing, generates the secrets and the database password, and writes `APP_ENV=prod`,
   `AUTO_MIGRATE=false`, `MT5_GATEWAY=manager`, `EMBEDDED_WORKER=false`, `STAFF_2FA_REQUIRED=true`,
   `PORTAL_HOST`, `BACKOFFICE_HOST`, `PUBLIC_URL=https://<PortalHost>` and `ALLOWED_HOSTS=<both hosts>`
   (override with `-PublicUrl` / `-AllowedHosts`).
3. Starts the database in Docker (unless `-DatabaseUrl` points at your own PostgreSQL).
4. Stops with a list if the host names, an https `PUBLIC_URL` or `SMTP_HOST` is missing. Fill in `SMTP_USER`,
   `SMTP_PASSWORD` and `SMTP_FROM` in `.env`.
5. Runs `migrate`, then `check-config` (settings, database, test login to the MT5 server) unless
   `-SkipChecks`, then `create-admin` when `-AdminEmail` is given (asks for the password).
6. With `-InstallServices` (elevated): installs NSSM and Caddy if missing and runs `install-services.ps1`,
   which installs the three services below.

On a brand-new database there is no MT5 server yet: `check-config` says so, and you add it after the first
sign-in under **MT5 → Configuration** (the connection is tested when you save). Alternatively, set
`MT5_SERVER`, `MT5_MANAGER_LOGIN` and `MT5_MANAGER_PASSWORD` in `.env` before the first migrate and it is
created from them.

### 2. Windows services

| Service | Runs |
|---|---|
| `FlowCRM-API` | `uvicorn app.main:app` on `127.0.0.1:8000` (`-Port`), one process, proxy headers trusted from localhost only |
| `FlowCRM-Worker` | `python -m app.workers`: deal push, polling, scheduler, sync tasks |
| `FlowCRM-Proxy` | Caddy with `deploy\Caddyfile`: HTTPS for `PORTAL_HOST` and `BACKOFFICE_HOST` (certificates in `storage\caddy`) |

To (re)install them by hand, or as a dedicated account (recommended over LocalSystem):

```powershell
.\deploy\windows\install-services.ps1 -Nssm (Get-Command nssm).Source -Caddy (Get-Command caddy).Source `
  -ServiceUser ".\flowcrm" -ServicePassword (Read-Host -AsSecureString) -PostgresService ""
```

- Run **exactly one worker**. It exits with code 1 if a thread dies and NSSM restarts it.
- The script migrates, runs `check-config`, (re)installs the services (auto start, restart on exit,
  `PYTHONUTF8=1`, `AUTO_MIGRATE=false`), makes them depend on a local PostgreSQL Windows service if one exists
  (`-PostgresService`; empty with the Docker database, where NSSM restarts the API until the database is up),
  starts them, then checks `/ready` and that the worker is running.
- `-ServiceUser` / `-ServicePassword` run the API and worker as a dedicated account (needs read access to the
  folder, write access to `logs\`, `storage\` and `mt5data\`).
- **Logs:** the app writes daily files kept 30 days in `logs\crm` (`api.log`, `worker.log`). NSSM's console
  capture (`logs\services\*-console.log`, rotated at 10 MB) is only a fallback for start-up crashes.

### 3. HTTPS (Caddy)

The `FlowCRM-Proxy` service runs Caddy natively on Windows (not in Docker: Docker Desktop hides the visitor's IP
address from containers, and IP rules, rate limits and the audit log need it). Certificates come from Let's
Encrypt automatically once both DNS names point at the server.

- **Portal host:** the website, client area and client API only; `/admin`, `/docs`, `/openapi.json` and the staff
  API return 404.
- **Back-office host:** `/` redirects to `/admin`. A commented example in `deploy\Caddyfile` restricts it to
  office / VPN addresses; replace the documentation range and uncomment it (recommended), then
  `nssm restart FlowCRM-Proxy`.
- **Both:** `/ready` and `/health` answer only from the machine itself, request bodies are capped at 12 MB,
  the `Server` header is removed, access logs go to `logs\caddy`.

After the first sign-in (2FA setup is required), set your branding under **Administration → Branding**, check
the server under **MT5 → Configuration**, then import groups and accounts under **MT5 → Import from MT5**.

### Backups

Schedule `deploy\windows\backup.ps1` daily in Task Scheduler:

```powershell
.\deploy\windows\backup.ps1 -BackupDir D:\backups\flow          # database in Docker
.\deploy\windows\backup.ps1 -BackupDir D:\backups\flow -PgDump "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
```

It writes a `pg_dump` (custom format; with the Docker database it runs inside the container, otherwise the
password goes through a temporary PGPASSFILE), a zip of
`STORAGE_DIR` and a **copy of `.env`**, deletes files older than `-KeepDays` (30) and prunes old service console
logs. **The `.env` copy holds the secrets** (`SECRET_KEY`, `CREDENTIALS_KEY`, database password): without it
the encrypted secrets in the dump cannot be read, and with it anyone can. Restrict the backup folder to
administrators and encrypt it if it leaves the machine. Restore with
`pg_restore --clean --no-owner -d <db-url> <file>.dump`.

### Upgrading

From an elevated PowerShell in the project folder:

```powershell
.\deploy.ps1 -BackupDir D:\backups\flow    # back up, then update to the latest code of this branch
```

`deploy.ps1` follows the branch that is checked out (`git checkout <branch>` once to follow another one):

1. Stops when this copy has local changes, or when the branch has nothing new (`-Force` runs anyway).
2. Backs up with `backup.ps1` (with `-BackupDir`), then stops the services.
3. `git pull --ff-only` from origin.
4. Runs `setup.ps1` in the mode `.env` is in. When the new code needs another Python (`.python-version`),
   it installs that version and creates `.venv` again. Then packages, migration, `check-config`.
5. Starts the services and waits until `/ready` answers. If setup failed, the services stay stopped and the
   script says how to go back to the previous version.

In development it does the same without the services: stop `run-demo.ps1` first, start it again afterwards.

The API and worker refuse to work on an out-of-date schema (the worker exits; the API logs a critical error).

### Operating

- **Health:** `GET /health` (process up). `GET /ready` returns 503 only when the database is down; otherwise
  `ok`, or `degraded` while the MT5 server is not connected. Details only from localhost.
- **Logs:** `logs\crm\api.log`, `logs\crm\worker.log`. Every request has an `X-Request-ID`, shown on error pages
  as a reference.
- **MT5:** Server health page and the red disconnect banner; slow scheduler steps are logged.
- **Audit:** Administration → Audit log and Login attempts.

## Architecture (for developers)

```
app/
  config.py      settings (.env) and production checks
  main.py        FastAPI app: middleware, routers, /health, /ready, embedded worker
  cli.py         migrate, check-config, create-admin, seed-demo
  core/          security (hashing, tokens, TOTP, encryption), deps (auth, RBAC), permissions,
                 middleware (headers, request log, IP filter), audit, rate limit, masks.py (MT5 name masks)
  models/        SQLAlchemy models (clients, staff, MT5, trading, finance, copy, marketing, automation, …)
  services/      business logic shared by the UI, the API and the worker;
                 mt5_calls.py wraps MT5 calls (MT5 errors become ServiceError / HTTP 502)
  mt5/           gateway.py (MT5Gateway interface), manager.py (MT5Manager adapter), mock.py (simulator),
                 routing.py (routed(): forwards to the one server), __init__.py (gateway registry)
  web/           server-rendered UI: admin/ (back-office), portal.py, site.py, callbacks.py (provider
                 callbacks), sandbox.py (dev only), common.py (sessions, CSRF, guards)
  api/           JSON API routers under /api
  workers/       runner.py (worker threads), __main__.py (standalone worker with supervisor)
  templates/     Jinja2 templates (admin, portal, site); static/ holds CSS / JS (Bootstrap, htmx, vendored)
migrations/      Alembic schema history (0001-0017)
deploy/          Caddyfile, windows\install-services.ps1, windows\backup.ps1
docker-compose.yml   the PostgreSQL container (setup.ps1 / run-demo.ps1 start it)
setup.ps1, deploy.ps1, run-demo.ps1, .python-version (the Python version Flow runs on)
tests/
```

- **Only `app/mt5` imports MT5Manager.** Everything else talks to `MT5Gateway`, so the real adapter and the
  simulator are interchangeable. Each process keeps its own Manager API cache under `MT5_WORK_DIR`
  (for example `mt5data\api-server1`, `mt5data\worker-server1`), and picks up server edits without a restart.
- **"MT5 first, then Flow" write pattern:** a service validates input, sends the change to MT5 through
  `mt5_calls`, and only then writes its own rows and the audit entry. If MT5 refuses, the transaction is rolled
  back and the message is shown. Where MT5 re-orders or normalises data (routing), the service re-reads it.
- **Worker threads** (`app/workers/runner.py`), each surviving errors with back-off:
  - `deal-push`: MT5 deal subscription → ingest → copy engine and IB commissions;
  - `poller`: deals, positions, orders and account summaries (active / idle), resubscribes after server edits,
    writes the worker heartbeat;
  - `scheduler` (every 60 s, each step in its own transaction): PAMM, copy reconcile, copy fees, MT5 import,
    client details retry, IP rules expiry, server health, account activity, demo clean-up, pricing, automation;
  - `sync-jobs`: queued Trade sync tasks.
- **Adding a feature:** service function in `app/services` (checks, MT5 call, DB write, `audit(...)`); a
  permission in `app/core/permissions.py` (and in `DEFAULT_ROLES` if needed); a route in `app/web/admin/*.py`
  with `Depends(staff_perm("..."))` (and / or an `/api` router); a template under `app/templates/admin` and a
  menu entry in `layout.html`; an Alembic migration for model changes; tests in `tests/` using the mock gateway.

## Tests

```powershell
.venv\Scripts\python -m pytest -q        # throw-away PostgreSQL via pgserver
$env:TEST_DATABASE_URL = "postgresql+psycopg://user:pw@localhost/crm_test"   # or your own (it is wiped)
```

**239 tests**, on PostgreSQL only, against the MT5 simulator. They cover:

- Security: lockout and backoff, 2FA (single use, encrypted secrets), RBAC and no privilege escalation, CSRF,
  rate limits, headers, session idle timeout and logout everywhere, token revocation, email change permission,
  upload caps, provider URL restrictions, `/ready` details, production settings refused.
- Money: maker-checker, parallel approvals and PAMM withdrawals paying once, wallet holds, promo bonuses,
  provider callbacks (signatures, amount / currency checks, held payments, replays), the sandbox.
- Every admin and portal page renders; main form flows end to end; the public website.
- MT5: import of groups and users, account groups mirrored to MT5 groups (create, update, move / force delete),
  client details pushed with retry, symbols, gateways, data feeds and routing written MT5-first, presets,
  pricing markups, IP rules and the MT5 firewall mirror, removing the server without touching MT5.
- Sync: idempotent ingest, orders, Trade sync tasks, active / idle polling, account activity, server health,
  inactive demo accounts, worker resilience (failing steps, dead threads, heartbeat).
- Copy trading (partial closes, fees, timeout reconciliation), PAMM NAV and fees, multi-level IB commissions.
- Trader styles and automation (each trigger and action, cooldown, preview, permissions).
- Alembic migrations match the models and survive a full down / up round trip.

## Before going live

1. **Production settings:** `check-config` passes with `APP_ENV=prod`; `CREDENTIALS_KEY` and `.env` are backed
   up somewhere safe.
2. **Live MT5 verification.** The Manager adapter follows the package's type stubs (build 5.0.6231); a few parts
   (timestamps, the dealer sink, ticks) are isolated in small helpers because they could not be verified without
   a live server. On a demo MT5 server with a demo manager account, test: open accounts, deposit and withdraw,
   copy a trade, sync and Trade sync, import, and one change each for symbols, gateways, routing and pricing.
3. **Manager rights:** confirm the manager login has the rights listed above, including administrator access if
   you use the server-setup pages, archiving or the firewall mirror.
4. **Payment and KYC providers:** test each in its sandbox. Providers with another signing scheme (signatures
   over selected fields, RSA) need a small adapter in `app/services/integrations.py`.
5. **Network:** Caddy in front with separate portal and back-office hosts; back-office restricted to office / VPN
   addresses (Caddy or IP access rules); only ports 80 / 443 open.
6. **Operations:** backups scheduled and a restore tested; services run as a dedicated account; staff 2FA set up;
   default roles reviewed.
7. **Compliance content:** terms, risk disclosures, privacy policy and `REGULATORY_TEXT` come from your legal team.

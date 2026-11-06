# Custom modules

Add your own features to Flow without changing its code. Your work lives in `custom/<name>/`. Git ignores that
folder, so `git pull` and `deploy.ps1` update Flow and leave your module alone.

[`big_deals/`](big_deals) is a working sample that uses every feature on this page. To try it:

```powershell
Copy-Item -Recurse docs\custom\big_deals custom\big_deals
```

Restart Flow, then give your role the **View the list of big deals** permission (Admin already has it). **Big deals**
appears in the menu.

## Keeping your module in your own repository

`custom/` is ignored by Flow's repository, so your code is not saved anywhere until you give it a home. Make each
module its own private repository and clone it into place:

```powershell
git clone https://github.com/your-company/flow-acme.git custom\acme
```

To update a server: `git -C custom\acme pull`, then `.\deploy.ps1`. Use `.\deploy.ps1 -Force` when only your module
changed, so that the services restart. Keep `custom/` in your backups if it isn't in a repository.

## What a module looks like

```
custom/acme/
  __init__.py         required: holds setup(crm)
  pages.py            your code, split however you like (use relative imports)
  templates/acme/     your pages (put them in a folder named after the module)
  static/             served at /static/custom/acme/
  migrations/         001_first.sql, 002_next.sql: your database tables
  requirements.txt    extra Python packages, installed by setup.ps1 and deploy.ps1
```

The folder name must be lower case letters, digits and `_`.

```python
from .pages import router, on_new_deals, nightly

def setup(crm):
    crm.add_permission("acme.view", "View Acme reports")
    crm.add_admin_routes(router)
    crm.add_menu("Acme reports", "/admin/custom/acme", "bar-chart", "acme.view", section="Acme")
    crm.on_deals(on_new_deals)
    crm.every(86400, nightly)
```

| Call | What it does |
|---|---|
| `crm.add_admin_routes(router)` | Back-office pages at `/admin/custom/<name>/...`. Staff must be signed in; check permissions with `staff_perm`. |
| `crm.add_portal_routes(router)` | Client area pages at `/portal/custom/<name>/...`. Clients must be signed in. |
| `crm.add_public_routes(router)` | Open addresses at `/custom/<name>/...`, for example a provider's callback. **Anyone can reach these**, so check who is calling. |
| `crm.add_menu(text, href, icon, perm, section)` | A back-office menu entry. `icon` is a [Bootstrap icon](https://icons.getbootstrap.com) name. |
| `crm.add_permission(code, description)` | A permission you can give to roles under Staff → Roles. Admin has it automatically. |
| `crm.on_deals(handler)` | `handler(db, deals)` runs after new deals are saved. |
| `crm.every(seconds, fn)` | `fn(db)` runs in the worker every N seconds, at most once a minute. |

Pages use `render(request, "acme/page.html", ...)` from `app.web.common`. Forms on admin and portal pages need
`{{ csrf_input(csrf) }}`, as Flow's own forms do.

Deal handlers and jobs commit their own work. A deal can reach a handler twice, so make handlers safe to repeat. They
can also miss a deal if the process stops at the wrong moment, so anything that must be complete needs its own
catch-up job.

## Replacing one of Flow's pages

A file in your `templates/` with the same path as one of Flow's replaces it. For example,
`custom/acme/templates/admin/dashboard.html` replaces `app/templates/admin/dashboard.html`.

Do this as little as you can: when an update changes Flow's page, your copy doesn't get the change, and it may stop
working if the update changes what the page is given. Flow logs a warning for each replaced page at start-up, so you
have a list to check after each update.

## Your database tables

Files in `migrations/` run once each, in name order, when Flow starts or `python -m app.cli migrate` runs. Flow records
what has run in the `custom_migrations` table.

- Never edit a file that has already run. Add a new one.
- Start your table names with `custom_`, so they can't clash with Flow's.
- Don't change Flow's tables. An update could break, or be broken by, your change.

## What updates can break

Your module is safe from `git pull`, but it still runs inside Flow.

- **Stable:** the `crm` calls above and the `/admin/custom`, `/portal/custom` and `/custom` addresses.
- **Can change in any update:** everything else you import from `app`, such as services, models and templates.
  Test your module on a copy before updating a live server.

## When a module fails

If a module fails to load, Flow starts without it. The error goes to the log, and staff who manage MT5 servers see a
red banner in the back-office. A migration that fails shows the same banner, and it is tried again at the next start,
but that module's pages stay on, so they may show errors until it's fixed.

To tell whether a problem comes from your module or from Flow, set `CUSTOM_ENABLED=false` in `.env` and restart: Flow
then runs without any module.

## Security

A module runs inside Flow with full access to its database, settings and MT5 connection. Only install code you trust,
and review a module as you would a change to Flow itself.

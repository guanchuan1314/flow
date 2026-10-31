"""Your payment provider. This is the only file you need to change.

The sample ships with a built-in demo checkout page so the whole round trip works out of the box.
For a real provider (card acquirer, e-wallet, crypto gateway, local bank transfer...):

  create_checkout()  call your provider's API to create a payment for this session and return the
                     provider's checkout URL. Give the provider:
                       - the payment id / order id:   session["id"]  (to find the session again)
                       - the amount and currency:     session["amount"], session["currency"]
                       - its return URL:              f"{public_url}/return/{session['id']}"
                       - its webhook URL:             f"{public_url}/provider/webhook"
  parse_webhook()    verify the provider's notification (its own signature!) and translate it.
"""

import html


def create_checkout(session: dict, public_url: str) -> str:
    """Return the URL the client is redirected to (step 2)."""
    # Real provider, for example:
    #   resp = requests.post("https://api.provider.com/v1/payments", headers={"Authorization": "Bearer ..."}, json={
    #       "order_id": session["id"], "amount": session["amount"], "currency": session["currency"],
    #       "customer_email": session["email"],
    #       "return_url": f"{public_url}/return/{session['id']}",
    #       "notify_url": f"{public_url}/provider/webhook"})
    #   return resp.json()["checkout_url"]
    return f"{public_url}/checkout/{session['id']}"  # the demo page below


def parse_webhook(headers, raw: bytes):
    """Your provider's notification -> (session id, status, transaction id, amount, currency, reason),
    or None to reject it. status must be "paid", "failed", "cancelled" or "pending"."""
    # Real provider, for example:
    #   if not provider_signature_valid(headers, raw): return None
    #   data = json.loads(raw)
    #   status = {"SUCCESS": "paid", "FAILED": "failed", "CANCELLED": "cancelled"}.get(data["state"], "pending")
    #   return data["order_id"], status, data["transaction_id"], data["paid_amount"], data["currency"], data.get("message", "")
    return None  # the demo provider has no webhook


# ---------------------------------------------------------------- demo provider (replace or delete)
def demo_decision(form: dict) -> tuple[str, str, str]:
    """The demo page's buttons -> (status, transaction id, reason)."""
    if form.get("decision") == "pay":
        return "paid", "DEMO-" + form.get("sid", "")[:8], ""
    return "cancelled", "", "Cancelled by the client"


def demo_page(session: dict) -> str:
    e = html.escape
    name = f"{session.get('first_name', '')} {session.get('last_name', '')}".strip()
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo checkout</title><style>
body{{font-family:system-ui,sans-serif;background:#f4f6fb;margin:0;display:grid;place-items:center;min-height:100vh}}
.card{{background:#fff;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,.08);padding:32px;width:min(420px,92vw)}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#667085;font-size:14px}} .amount{{font-size:34px;font-weight:700;margin:20px 0}}
button{{width:100%;padding:12px;border-radius:10px;border:0;font-size:16px;cursor:pointer;margin-top:10px}}
.pay{{background:#2563eb;color:#fff}} .cancel{{background:#eef0f4;color:#344054}}
</style></head><body><form class="card" method="post">
<h1>Demo payment provider</h1><div class="muted">Replace this page with your provider in provider.py</div>
<div class="amount">{e(str(session['amount']))} {e(session['currency'])}</div>
<div class="muted">{e(session.get('description', ''))} &middot; {e(name)} &middot; ref {e(session['reference'])}</div>
<input type="hidden" name="sid" value="{e(session['id'])}">
<button class="pay" name="decision" value="pay">Pay now</button>
<button class="cancel" name="decision" value="cancel">Cancel</button>
</form></body></html>"""


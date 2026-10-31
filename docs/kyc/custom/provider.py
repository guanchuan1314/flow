"""Your identity verification provider. This is the only file you need to change.

The sample ships with a built-in demo verification page so the whole round trip works out of the box.
For a real provider (Sumsub, Onfido, Veriff, a local e-KYC service, your own review team...):

  create_verification()  call your provider's API to create a verification for this client and return
                         the provider's verification URL. Give the provider:
                           - your own id for it:     session["id"]  (to find the session again)
                           - the client's details:   session["first_name"], ["last_name"], ["email"],
                                                     ["country"], ["date_of_birth"], ["client_id"]
                           - its return URL:         f"{public_url}/return/{session['id']}"
                           - its webhook URL:        f"{public_url}/provider/webhook"
  parse_webhook()        verify the provider's notification (its own signature!) and translate it.
"""

import html


def create_verification(session: dict, public_url: str) -> str:
    """Return the URL the client is redirected to (step 2)."""
    # Real provider, for example:
    #   resp = requests.post("https://api.kyc-provider.com/v1/applicants", headers={"Authorization": "Bearer ..."}, json={
    #       "external_id": session["id"], "first_name": session["first_name"], "last_name": session["last_name"],
    #       "email": session["email"], "country": session["country"], "dob": session["date_of_birth"],
    #       "redirect_url": f"{public_url}/return/{session['id']}",
    #       "webhook_url": f"{public_url}/provider/webhook"})
    #   return resp.json()["verification_url"]
    return f"{public_url}/verify/{session['id']}"  # the demo page below


def parse_webhook(headers, raw: bytes):
    """Your provider's notification -> (session id, status, reason), or None to reject it.
    status must be "approved", "rejected" or "pending"."""
    # Real provider, for example:
    #   if not provider_signature_valid(headers, raw): return None
    #   data = json.loads(raw)
    #   status = {"GREEN": "approved", "RED": "rejected"}.get(data["review_result"], "pending")
    #   return data["external_id"], status, data.get("reject_reason", "")
    return None  # the demo provider has no webhook


# ---------------------------------------------------------------- demo provider (replace or delete)
def demo_decision(form: dict) -> tuple[str, str]:
    """The demo page's buttons -> (status, reason)."""
    if form.get("decision") == "approve":
        return "approved", ""
    return "rejected", "The document photo was not readable"


def demo_page(session: dict) -> str:
    e = html.escape
    name = f"{session.get('first_name', '')} {session.get('last_name', '')}".strip()
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo identity verification</title><style>
body{{font-family:system-ui,sans-serif;background:#f4f6fb;margin:0;display:grid;place-items:center;min-height:100vh}}
.card{{background:#fff;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,.08);padding:32px;width:min(440px,92vw)}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:#667085;font-size:14px}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:20px 0;font-size:15px}} dt{{color:#667085}}
.drop{{border:2px dashed #cfd6e4;border-radius:12px;padding:18px;text-align:center;color:#667085;font-size:14px}}
button{{width:100%;padding:12px;border-radius:10px;border:0;font-size:16px;cursor:pointer;margin-top:10px}}
.ok{{background:#16a34a;color:#fff}} .no{{background:#eef0f4;color:#344054}}
</style></head><body><form class="card" method="post">
<h1>Demo identity verification</h1><div class="muted">Replace this page with your provider in provider.py</div>
<dl><dt>Name</dt><dd>{e(name)}</dd><dt>Email</dt><dd>{e(session.get('email', ''))}</dd>
<dt>Country</dt><dd>{e(session.get('country', '') or '-')}</dd><dt>Reference</dt><dd>{e(session['reference'])}</dd></dl>
<div class="drop">Your provider's document capture and liveness check go here</div>
<button class="ok" name="decision" value="approve">Approve (documents OK)</button>
<button class="no" name="decision" value="reject">Reject (document unreadable)</button>
</form></body></html>"""

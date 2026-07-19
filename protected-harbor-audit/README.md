# Protected Harbor SaaS Infrastructure Resilience Audit
"Can You Take a Server Down?"

Same architecture as AUDIT-LITE. Flask + single page frontend, deploy to Render.

## Deploy (Render)
1. Push this folder to a GitHub repo (e.g. NAVVAI-BOYS/PH-RESILIENCE). If the app lives in a
   subfolder, set Root Directory accordingly (same lesson as navvai-audit).
2. New Web Service → Build: `pip install -r requirements.txt` → Start: `gunicorn app:app --timeout 180`
   (long timeout matters: the analyze call can take a while).
3. Environment variables:
   - ANTHROPIC_API_KEY  (required for AI; without it every AI section falls back client side, app still works)
   - MODEL              default claude-sonnet-4-6
   - ADMIN_KEY          pick a strong value; needed for /api/leads, /api/leads.csv, /api/events
   - RESEND_API_KEY     optional until Resend is configured; email falls back to mailto
   - FROM_EMAIL         e.g. "Protected Harbor Resilience Audit <info@navvai.com>" (domain must be verified in Resend)
   - NOTIFY_EMAIL       internal lead copy, default info@navvai.com
   - CALENDLY_URL       the booking link used in the CTA and emails

## Modes
- Prospect mode: /            (email gated, teaser above the gate)
- Consultant mode: /?mode=consultant   (no gate, follow ups on every question; for Rob's live intro calls)

## Admin
- Leads:  /api/leads?key=ADMIN_KEY   and   /api/leads.csv?key=ADMIN_KEY
- Events: /api/events?key=ADMIN_KEY
- Health: /healthz  (shows whether AI + email are configured)
- Every AI call logs duration, output size and stop reason to Render logs.

## Known gaps (carry-overs from AUDIT-LITE, close before real volume)
- Storage is data/*.json on local disk: Render free tier disk is ephemeral, so add a Render Disk
  or move leads to persistent storage before the sprint.
- Rate limiting is basic in-memory per IP.

## Before shipping to Protected Harbor
- Verify the three "wider picture" stats and their sources (Gartner downtime figure, Uptime
  Institute outage analysis, IBM Cost of a Data Breach 2024); swap in fresher editions if needed.
- Replace CALENDLY_URL with Protected Harbor's own booking link if co-branded.
- protectedharbor.com is referenced in the footer.

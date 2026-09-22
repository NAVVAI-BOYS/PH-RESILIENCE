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
   - BASE_URL           public URL used in the "View your full report" email link,
                        e.g. https://protectedharboraudit.com (falls back to the request host)
   - DATA_DIR           optional. Point at a Render Disk mount (e.g. /var/data) so leads and
                        hosted reports survive redeploys. Defaults to ./data next to app.py.

## Report delivery (email, hosted link, PDF)
- On gate unlock the browser posts the lead straight away (stage "capture") with the report
  exactly as rendered on screen. It is stored at data/reports/<id>.html and served at /r/<id>.
- The browser then builds a PDF of that same page (html2canvas + jsPDF, page breaks never cut
  through a line or a bar) and posts it (stage "send"). The prospect gets a short branded
  email: verdict at a glance, "View your full report" button to /r/<id>, PDF attached.
  The internal copy to NOTIFY_EMAIL carries the same link and attachment.
- If the PDF cannot be built (old browser, CDN blocked, timeout) the email still goes out with
  the link only. "Download the full report (PDF)" on screen uses the same generator, and falls
  back to a standalone HTML file that prints clean.
- /r/<id>.pdf serves the stored PDF; the hosted page shows a "Download PDF" button when it exists.
- Report ids are unguessable (secrets.token_urlsafe) and pages are marked noindex.

## Modes
- Prospect mode: /            (email gated, teaser above the gate)
- Consultant mode: /?mode=consultant   (no gate, follow ups on every question; for Rob's live intro calls)

## Admin
- Leads:  /api/leads?key=ADMIN_KEY   and   /api/leads.csv?key=ADMIN_KEY
- Events: /api/events?key=ADMIN_KEY
- Health: /healthz  (shows whether AI + email are configured)
- Every AI call logs duration, output size and stop reason to Render logs.

## Known gaps (carry-overs from AUDIT-LITE, close before real volume)
- Storage is data/*.json plus data/reports/ on local disk: Render free tier disk is ephemeral, so
  add a Render Disk and set DATA_DIR to its mount path before the sprint. Without it, hosted
  report links stop working after a redeploy (the PDF in the email is unaffected and the link
  page explains how to get a new copy).
- Rate limiting is basic in-memory per IP.

## Before shipping to Protected Harbor
- Verify the three "wider picture" stats and their sources (Gartner downtime figure, Uptime
  Institute outage analysis, IBM Cost of a Data Breach 2024); swap in fresher editions if needed.
- Replace CALENDLY_URL with Protected Harbor's own booking link if co-branded.
- protectedharbor.com is referenced in the footer.

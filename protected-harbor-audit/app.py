"""
Protected Harbor SaaS Infrastructure Resilience Audit
"Can You Take a Server Down?"

Flask backend. Mirrors the AUDIT-LITE architecture:
  - AI endpoints (reroute, followup, analyze) with duration/stop-reason logging
  - Verdict computed server side from the weakest pillar (never vibed)
  - Email gate leads stored in data/leads.json, retrievable via /api/leads (+ CSV)
  - Funnel events at /api/event + /api/events
  - Resend email on gate unlock (graceful mailto fallback until configured)
  - Basic in-memory rate limiting per IP

Env vars (set on Render):
  ANTHROPIC_API_KEY   required for AI sections (client-side fallbacks cover outages)
  MODEL               default claude-sonnet-4-6
  ADMIN_KEY           required to read /api/leads, /api/leads.csv, /api/events
  RESEND_API_KEY      optional; enables real email sending
  FROM_EMAIL          default "Protected Harbor Resilience Audit <info@navvai.com>"
  NOTIFY_EMAIL        default info@navvai.com  (internal lead notification copy)
  CALENDLY_URL        booking link used in emails + report CTA
"""

import base64
import json
import os
import re
import secrets
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request, Response, abort

app = Flask(__name__)

# ---------------------------------------------------------------- config

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get(
    "FROM_EMAIL", "Protected Harbor Resilience Audit <info@navvai.com>"
)
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "info@navvai.com")
CALENDLY_URL = os.environ.get("CALENDLY_URL", "https://calendly.com/navvai")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "info@navvai.com")

# DATA_DIR can point at a Render Disk mount so leads and hosted reports survive
# redeploys (e.g. DATA_DIR=/var/data). Defaults to ./data next to app.py.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEADS_PATH = os.path.join(DATA_DIR, "leads.json")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
# Public base URL used in emails for the hosted report link. Falls back to the
# request host, so it only needs setting if the app sits behind a proxy.
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
# Reports carry a PDF, so the lead payload can be a few MB.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

_lock = threading.Lock()

# ---------------------------------------------------------------- rate limit

_hits = {}  # ip -> [timestamps]
LIMITS = {"analyze": (12, 3600), "default": (90, 3600)}


def rate_limited(ip, bucket="default"):
    limit, window = LIMITS.get(bucket, LIMITS["default"])
    now = time.time()
    key = f"{ip}:{bucket}"
    with _lock:
        arr = [t for t in _hits.get(key, []) if now - t < window]
        if len(arr) >= limit:
            _hits[key] = arr
            return True
        arr.append(now)
        _hits[key] = arr
    return False


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "?")


# ---------------------------------------------------------------- json store


def _load(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _append(path, item):
    with _lock:
        items = _load(path)
        items.append(item)
        with open(path, "w") as f:
            json.dump(items, f, indent=1)


# ---------------------------------------------------------------- anthropic


def call_ai(system, user, max_tokens=6000, tag="ai"):
    """Call the Anthropic API. Logs duration, output size and stop reason.
    Raises RuntimeError on any failure; callers convert to a 502 so the
    client-side fallback takes over. Never returns a blank."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    t0 = time.time()
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=110,
    )
    dur = time.time() - t0
    if r.status_code != 200:
        app.logger.error("[%s] api %s in %.1fs: %s", tag, r.status_code, dur, r.text[:400])
        raise RuntimeError(f"api {r.status_code}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    app.logger.info(
        "[%s] ok in %.1fs, %s chars, stop=%s",
        tag, dur, len(text), data.get("stop_reason"),
    )
    return text


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("no json object in output")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- prompts

HOUSE_RULES = """You write for Protected Harbor, a SaaS infrastructure engineering firm.
Voice: a straight-talking senior engineer who explains complex things in simple terms. Plain English only.
HARD RULES:
- Never use a term the reader has to already know. If a technical term is unavoidable (HA, failover, stateless), define it in the same sentence in brackets.
- Every claim carries its reason in plain English. No unexplained scores, no jargon, no buzzwords.
- Never invent money figures. Money math is computed elsewhere from the prospect's own numbers. You may reference "your own figures" but never state amounts.
- Be honest even if it costs the sale. If they are genuinely in good shape, say so plainly.
- Ground everything in what they actually told you. Quote their words back where useful.
- No hyphens or em dashes in output text. Use commas or full stops.

BREVITY, THE HARDEST RULE (an executive reads this in four minutes or not at all):
- Say it once. If a point appears in one section it does not reappear in another, in any rewording.
- Every sentence must earn its place. Cut throat clearing, restatement of the question, and any phrase that could be deleted without losing meaning.
- Prefer the shorter construction every time. Same meaning, half the words.
- Never pad to fill a field. A short honest line beats a long one.
- WORD CAPS ARE HARD LIMITS, not targets. Exceeding one is a failure:
  headline max 14 words · summary max 55 words · first_move max 45 words
  each pillar note max 22 words · each pillar gap max 20 words · each pillar consequence max 22 words · blind_spot max 60 words
- EXCEPTION to the caps: the exposure narrative fields (blind_spot, ransomware_exposure, frankenstein, ai_readiness) carry the reasoning, so they may run to 90 words each. Explain WHY the finding follows from their answers, not just what it is. Everywhere else the caps hold.
  each risk/check explanation max 30 words · each pain fix line max 28 words
  why_winner max 35 words · each phase description max 35 words
  within_reach max 40 words · return_source max 40 words
- Write for someone skimming on a phone between meetings."""

ANALYZE_SYSTEM = HOUSE_RULES + """

You produce a SaaS infrastructure resilience report as STRICT JSON only. No preamble, no markdown fences, no text outside the JSON object.

Scoring: score each pillar 0 to 10 from their answers. Be conservative and honest. An unanswered or "not sure" answer lowers confidence and the score, and the report should say so honestly.
The verdict is computed by the system from the weakest pillar (7+ Resilient, 4 to 6.9 Partially Resilient, under 4 Critical Exposure). Do not argue with that rule; write consistently with it.

Blind spot patterns to check their answers against (pick the ONE most true for them, never generic):
- a failover that has never actually been exercised is a hope, not a plan
- clients mapped across shared servers so no single server can ever come down
- process holes behind the tooling (the person at the desk can bypass the system)
- security nobody owns end to end because every specialist only knows their slice
- a VMware renewal shock nobody has priced in
- a cloud bill quietly making the case for moving workloads back
- environments that can never do maintenance are the ones ransomware walks into

Return EXACTLY this JSON shape:
{
 "headline": "one sentence, specific to them, their biggest truth",
 "pillars": [
   {"key":"resilience","name":"Resilience and availability","score":0,"reason":"plain English, cites their answers","gap":"the specific gap this score reveals, max 20 words, concrete not abstract","consequence":"what that gap leads to in practice, max 22 words, an operational outcome not a feeling","source_questions":["ids of the questions that drove this score"]},
   {"key":"security","name":"Security and maintenance debt","score":0,"reason":"...","gap":"...","consequence":"...","source_questions":[]},
   {"key":"scale","name":"Scalability, performance and cost","score":0,"reason":"...","gap":"...","consequence":"...","source_questions":[]},
   {"key":"ops","name":"Operational readiness","score":0,"reason":"...","gap":"...","consequence":"...","source_questions":[]}
 ],
 "painkiller":{"label":"Painkiller" or "Vitamin","reason":"is infrastructure a bleeding wound for them right now or an insurance policy, and why"},
 "first_move":"one sentence: given the verdict and weakest pillar, the single thing to do before anything else, specific to their answers",
 "within_reach":"one short paragraph on why fixing this is achievable for them specifically, citing figures or facts they gave",
 "return_source":"one short paragraph on where the return actually comes from, in their figures and freed capacity, no invented amounts",
 "blind_spot":"the one thing they are not seeing, 2 to 3 sentences, specific to their answers",
 "frankenstein":"bolted on tools and silos vs connected systems, include unmanaged AI or shadow IT if relevant, and apply should you not just can you to their AI plans",
 "ransomware_exposure":"their maintenance debt and patch reality in plain terms and what that class of gap has historically meant, no invented figures",
 "ai_readiness":"what the business is asking for on AI vs what their infrastructure can actually carry, and the single gap to close first",
 "one_initiative":{"title":"the single highest leverage move","why":"why this one and not the other three"},
 "frankenstein_risk":"Low" or "Medium" or "High",
 "shadow_ai":{"level":"Low" or "Medium" or "High","text":"2 to 3 sentences on unofficial AI tool use in their environment, grounded in their answers; if nothing suggests it, say so and recommend a one page policy anyway"},
 "pain_service_map":[
   {"pain":"their named pain in their words","pillar":"the exact name of the pillar (of the four) where this challenge scored","service":"the Protected Harbor service that fixes it","how":"one plain sentence"}
 ],
 "ranking":[
   {"title":"improvement opportunity","score":0.0,"components":{"impact":0,"feasibility":0,"time_to_value":0,"risk_reduction":0},"reason":"why this score, referencing the weights"}
 ],
 "why_winner":"one short paragraph comparing number 1 to number 2 and why it wins; the ranking is relative",
 "roadmap":[
   {"phase":1,"days":"Days 0 to 30","title":"quick wins first","what":"what happens, plain English","service":"which Protected Harbor service does the work","effort":"Light or Moderate or Heavy","recovery_pct":20},
   {"phase":2,"days":"Days 30 to 60","title":"...","what":"...","service":"...","effort":"...","recovery_pct":45},
   {"phase":3,"days":"Days 60 to 90","title":"...","what":"...","service":"...","effort":"...","recovery_pct":65}
 ],
 "current_state":"one paragraph describing today, built directly from their answers, specific not generic",
 "future_state":"one paragraph describing day 90 if the roadmap lands, same specifics resolved",
 "goal_tieback":"one paragraph connecting the roadmap to their 12 month goal, quoting their goal verbatim",
 "trace":[
   {"answer_quote":"short quote of what they said","what_it_changed":"what it changed in this report"}
 ],
 "pilot":{"title":"the two week pilot","what":"the smallest first win that proves value, plain English"}
}

Rules for specific fields:
- pain_service_map: map EVERY pain they raised to one of: SaaS infrastructure engineering, Migration and DevOps repair, Managed hosting and high availability environments, Security hardening, Custom programming. Frame in their positioning: one accountable partner for performance, security and costs. Tag each with the pillar that flagged it so the reader can trace challenge, score, and fix in one line.
- Narrative linkage rules: ai_readiness must reference the weakest pillar by name (the pillars ARE the AI readiness scores). one_initiative must reference the ranking that follows it. blind_spot, frankenstein, and ransomware_exposure must each trace to a pillar score. The report is one story: verdict, cost, exposure, map, plan, receipts.
- ranking: 3 to 5 items, scored with weights Impact 35, Feasibility 25, Time to value 20, Risk reduction 20 (out of 10 overall). A 10 does not exist.
- roadmap recovery_pct: cumulative and conservative. Phase 1 max 25, phase 2 max 50, phase 3 max 70.
- trace: one entry per core answer they gave, including sizing figures and follow ups. Quote them.
- Peel the onion framing in the roadmap: the first fix takes the biggest bite, then diminishing returns."""

REROUTE_SYSTEM = HOUSE_RULES + """

The prospect tapped "Not sure, ask me a different way" on a question. Rewrite it as a tappable multiple choice question. Return STRICT JSON only:
{"question":"the same question asked a simpler, more concrete way","options":["3 to 5 options phrased as answers in the prospect's own voice, first person"]}
Options must be mutually distinct, cover the realistic range, use a scale format where natural, and be grounded in their industry and role."""

FOLLOWUP_SYSTEM = HOUSE_RULES + """

Given a prospect's answer, write ONE short follow up question that digs into the most revealing thing they said. Conversational, specific to their words, one sentence. Return STRICT JSON only: {"followup":"..."} or {"followup":null} if their answer is already complete."""


# ---------------------------------------------------------------- verdict

BANDS = [
    {"key": "luck", "name": "Critical Exposure", "min": 0, "max": 3.99,
     "meaning": "At least one layer will turn an ordinary failure into a business affecting outage. The next incident sets your timeline, not you."},
    {"key": "exposed", "name": "Partially Resilient", "min": 4, "max": 6.99,
     "meaning": "The foundations are sound, but named gaps remain in specific layers. Addressable inside 90 days."},
    {"key": "hardened", "name": "Resilient", "min": 7, "max": 10,
     "meaning": "You could lose a system tomorrow and recover inside your own tolerance. Remaining work is optimisation, not remediation."},
]


def compute_verdict(pillars):
    """Doctrine: the verdict follows the lowest pillar. Computed, not vibed."""
    scores = [max(0.0, min(10.0, float(p.get("score", 0)))) for p in pillars]
    weakest = min(scores) if scores else 0.0
    weakest_pillar = pillars[scores.index(weakest)]["name"] if pillars else ""
    band = next(b for b in BANDS if b["min"] <= weakest <= b["max"])
    nxt = None
    for b in BANDS:
        if b["min"] > weakest:
            nxt = {"name": b["name"], "gap": round(b["min"] - weakest, 1)}
            break
    return {
        "verdict": band["name"],
        "verdict_key": band["key"],
        "verdict_meaning": band["meaning"],
        "weakest_pillar": weakest_pillar,
        "weakest_score": round(weakest, 1),
        "readiness_pct": round(sum(scores) / (10 * len(scores)) * 100) if scores else 0,
        "next_band": nxt,
        "bands": BANDS,
        "rule": "Weakest pillar 7 or above: Resilient. 4 to 6.9: Partially Resilient. Under 4: Critical Exposure. Your verdict follows your lowest pillar, because an environment is only as resilient as its weakest layer.",
    }


RANK_BANDS = [
    (8.0, "Exceptional leverage"),
    (6.5, "Strong candidate"),
    (5.0, "Viable with preparation"),
    (0.0, "Not yet"),
]


def rank_band(score):
    for floor, label in RANK_BANDS:
        if score >= floor:
            return label
    return "Not yet"


# ---------------------------------------------------------------- routes


@app.route("/")
def index():
    return render_template("index.html", calendly_url=CALENDLY_URL, contact_email=CONTACT_EMAIL)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ai": bool(ANTHROPIC_API_KEY), "email": bool(RESEND_API_KEY)})


@app.route("/api/reroute", methods=["POST"])
def api_reroute():
    if rate_limited(client_ip()):
        return jsonify({"error": "rate"}), 429
    d = request.get_json(force=True, silent=True) or {}
    user = json.dumps({
        "industry": d.get("industry"), "role": d.get("role"),
        "question": d.get("question"), "rationale": d.get("rationale"),
        "prior_answers": d.get("prior", {}),
    })
    try:
        out = parse_json(call_ai(REROUTE_SYSTEM, user, max_tokens=800, tag="reroute"))
        if not isinstance(out.get("options"), list) or not out["options"]:
            raise RuntimeError("bad shape")
        return jsonify(out)
    except Exception as e:
        app.logger.warning("reroute fallback: %s", e)
        return jsonify({"error": "ai"}), 502


@app.route("/api/followup", methods=["POST"])
def api_followup():
    if rate_limited(client_ip()):
        return jsonify({"error": "rate"}), 429
    d = request.get_json(force=True, silent=True) or {}
    user = json.dumps({
        "industry": d.get("industry"), "role": d.get("role"),
        "question": d.get("question"), "answer": d.get("answer"),
    })
    try:
        out = parse_json(call_ai(FOLLOWUP_SYSTEM, user, max_tokens=400, tag="followup"))
        return jsonify({"followup": out.get("followup")})
    except Exception as e:
        app.logger.warning("followup skipped: %s", e)
        return jsonify({"followup": None})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    if rate_limited(client_ip(), "analyze"):
        return jsonify({"error": "rate"}), 429
    d = request.get_json(force=True, silent=True) or {}
    payload = {
        "industry": d.get("industry"), "role": d.get("role"),
        "twelve_month_goal": d.get("goal"),
        "answers": d.get("answers"),
        "sizing_figures_note": "money math is computed client side from these; do not restate amounts",
        "sizing": d.get("sizing"),
        "unsure_flags": d.get("unsure", []),
    }
    try:
        out = parse_json(call_ai(ANALYZE_SYSTEM, json.dumps(payload), max_tokens=4500, tag="analyze"))
        pillars = out.get("pillars") or []
        if len(pillars) != 4:
            raise RuntimeError("expected 4 pillars")
        out["computed"] = compute_verdict(pillars)
        # enforce conservative cumulative recovery caps
        caps = [25, 50, 70]
        for i, ph in enumerate(out.get("roadmap", [])[:3]):
            try:
                ph["recovery_pct"] = min(int(ph.get("recovery_pct", caps[i])), caps[i])
            except Exception:
                ph["recovery_pct"] = caps[i]
        for item in out.get("ranking", []):
            try:
                item["score"] = round(min(9.9, max(0.0, float(item.get("score", 0)))), 1)
            except Exception:
                item["score"] = 5.0
            item["band"] = rank_band(item["score"])
        return jsonify(out)
    except Exception as e:
        app.logger.error("analyze failed: %s", e)
        return jsonify({"error": "ai"}), 502


# ---------------------------------------------------------------- leads


def send_resend(to, subject, html, attachments=None):
    """attachments: list of {"filename": str, "content": base64 str}."""
    if not RESEND_API_KEY:
        return False, "resend not configured"
    payload = {"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html}
    if attachments:
        payload["attachments"] = attachments
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    ok = r.status_code in (200, 201)
    if not ok:
        app.logger.error("resend %s: %s", r.status_code, r.text[:300])
    return ok, r.text[:200]


# ---------------------------------------------------------------- hosted reports


def _esc(t):
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _report_paths(rid):
    if not re.match(r"^[A-Za-z0-9_-]{8,32}$", rid or ""):
        return None, None
    return os.path.join(REPORTS_DIR, rid + ".html"), os.path.join(REPORTS_DIR, rid + ".pdf")


def _base_url():
    return BASE_URL or request.url_root.rstrip("/")


def _save_report(rid, report_doc, pdf_b64):
    html_path, pdf_path = _report_paths(rid)
    if report_doc:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(report_doc)
    if pdf_b64:
        try:
            raw = base64.b64decode(pdf_b64.split(",")[-1], validate=False)
            if raw[:4] == b"%PDF":
                with open(pdf_path, "wb") as f:
                    f.write(raw)
                return raw
        except Exception as e:
            app.logger.error("pdf decode failed: %s", e)
    return None


def _company_slug(company):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company or "").strip("-")
    return f"Infrastructure-Resilience-Report{'-' + slug if slug else ''}.pdf"


def prospect_email(first, summary, report_url, has_pdf):
    """Short, branded email: the verdict at a glance, one button to the hosted
    report (which looks exactly as it did on screen), PDF attached."""
    sm = summary or {}
    verdict = sm.get("verdict") or ""
    vkey = sm.get("verdict_key") or ""
    vcol = {"hardened": "#3E9C6E", "luck": "#C4472F"}.get(vkey, "#DD7A36")
    rows = []
    if verdict:
        rows.append(("Verdict", f'<span style="display:inline-block;background:{vcol};color:#fff;font-weight:700;padding:4px 12px;border-radius:999px;font-size:13px">{_esc(verdict)}</span>'))
    if sm.get("weakest_pillar"):
        rows.append(("Weakest layer", f"{_esc(sm['weakest_pillar'])} &middot; {_esc(sm.get('weakest_score'))}/10"))
    if sm.get("readiness_pct") not in (None, ""):
        rows.append(("Overall readiness", f"{_esc(sm['readiness_pct'])}% (average of the four pillars)"))
    if sm.get("cost_month"):
        rows.append(("Cost of doing nothing", f"{_esc(sm['cost_month'])} a month, from your own figures"))
    table = "".join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #EFE2D4;color:#61707A;font-size:13px;width:44%;vertical-align:top">{k}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #EFE2D4;color:#12222E;font-size:14px;font-weight:600;vertical-align:top">{v}</td></tr>'
        for k, v in rows)
    pdf_line = ("Your PDF copy is attached, so it can be forwarded internally without a link."
                if has_pdf else "Forward the link freely: the glossary travels with the report.")
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:24px 12px;background:#FBF6F0">
<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:auto;color:#12222E">
  <div style="background:#22333D;color:#fff;padding:26px 28px;border-radius:16px 16px 0 0">
    <div style="font-size:11px;letter-spacing:2.4px;color:#F0995B;font-weight:700">PROTECTED HARBOR &middot; INFRASTRUCTURE RESILIENCE AUDIT</div>
    <h1 style="margin:10px 0 0;font-size:22px;line-height:1.3;color:#fff">{_esc(sm.get('headline') or 'Your resilience report is ready')}</h1>
  </div>
  <div style="background:#fff;border:1px solid #EFE2D4;border-top:0;padding:26px 28px;border-radius:0 0 16px 16px">
    <p style="margin:0 0 16px;font-size:15px;line-height:1.55">Hi{(' ' + _esc(first)) if first else ''},</p>
    <p style="margin:0 0 18px;font-size:15px;line-height:1.55">Thanks for taking the audit. Here is your verdict at a glance. The full report, presented exactly as it was on screen, is one tap away.</p>
    <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin:0 0 22px">{table}</table>
    {f'<p style="margin:0 0 22px;font-size:14px;line-height:1.55;color:#33434D">{_esc(sm.get("verdict_meaning"))}</p>' if sm.get("verdict_meaning") else ''}
    <div style="text-align:center;margin:8px 0 12px">
      <a href="{report_url}" style="display:inline-block;background:#DD7A36;color:#fff;padding:14px 30px;border-radius:999px;text-decoration:none;font-weight:700;font-size:15px">View your full report</a>
    </div>
    <p style="text-align:center;margin:0 0 26px;font-size:12.5px;color:#61707A;line-height:1.5">{pdf_line}</p>
    <div style="border-top:1px solid #EFE2D4;padding-top:20px">
      <p style="margin:0 0 6px;font-size:15px;font-weight:700">The next step: a free working session, not a sales call</p>
      <p style="margin:0 0 14px;font-size:14px;line-height:1.55;color:#33434D">Current state on one side of the whiteboard, future state on the other. Bring your business leaders, not just IT.</p>
      <a href="{CALENDLY_URL}" style="display:inline-block;border:2px solid #22333D;color:#22333D;padding:10px 22px;border-radius:999px;text-decoration:none;font-weight:700;font-size:14px">Book the working session</a>
    </div>
  </div>
  <p style="text-align:center;font-size:11.5px;color:#8A959D;margin:18px 0 0;line-height:1.5">Honest by design: if this report says you are in good shape, you are. Every money figure was computed from the figures you provided, never invented.<br>Protected Harbor &middot; protectedharbor.com</p>
</div></body></html>"""


@app.route("/r/<rid>")
def hosted_report(rid):
    html_path, pdf_path = _report_paths(rid)
    if not html_path or not os.path.exists(html_path):
        return Response(
            "<!DOCTYPE html><html><body style=\"font-family:Arial,sans-serif;max-width:560px;margin:80px auto;padding:0 20px;color:#12222E;line-height:1.55\">"
            "<div style=\"font-size:11px;letter-spacing:2px;color:#C2621F;font-weight:700\">PROTECTED HARBOR</div>"
            "<h1 style=\"font-size:22px\">This report link is no longer available</h1>"
            "<p>The PDF copy attached to your email is the same report. If you no longer have it, you can "
            f"<a href=\"{_base_url()}/\" style=\"color:#C2621F\">retake the audit</a> in about ten minutes, or write to "
            f"<a href=\"mailto:{CONTACT_EMAIL}\" style=\"color:#C2621F\">{CONTACT_EMAIL}</a>.</p></body></html>",
            status=404, mimetype="text/html")
    with open(html_path, "r", encoding="utf-8") as f:
        doc = f.read()
    # If the PDF was stored, the page offers it directly instead of print to PDF.
    if os.path.exists(pdf_path):
        doc = doc.replace('<button onclick="window.print()">Save as PDF</button>',
                          f'<a href="/r/{rid}.pdf" download style="display:inline-block;font:600 13px Poppins,Arial,sans-serif;background:#fff;border:1.5px solid #22333D;color:#22333D;border-radius:999px;padding:8px 16px;text-decoration:none">Download PDF</a>')
    return Response(doc, mimetype="text/html", headers={"X-Robots-Tag": "noindex"})


@app.route("/r/<rid>.pdf")
def hosted_report_pdf(rid):
    _, pdf_path = _report_paths(rid)
    if not pdf_path or not os.path.exists(pdf_path):
        abort(404)
    with open(pdf_path, "rb") as f:
        return Response(f.read(), mimetype="application/pdf",
                        headers={"Content-Disposition": "inline; filename=Infrastructure-Resilience-Report.pdf",
                                 "X-Robots-Tag": "noindex"})


@app.route("/api/lead", methods=["POST"])
def api_lead():
    if rate_limited(client_ip()):
        return jsonify({"error": "rate"}), 429
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get("email") or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "email"}), 400
    # Reuse the report id on a resend so the link in every email is the same one.
    rid = d.get("report_id") if _report_paths(d.get("report_id") or "")[0] else secrets.token_urlsafe(9)
    lead = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "name": (d.get("name") or "").strip(),
        "last_name": (d.get("last_name") or "").strip(),
        "company": (d.get("company") or "").strip(),
        "role": d.get("role"), "industry": d.get("industry"),
        "headline": d.get("headline"), "verdict": d.get("verdict"),
        "resend": bool(d.get("resend")),
        "delivery": d.get("delivery") or "instant",
        "report_id": rid,
    }
    report_url = f"{_base_url()}/r/{rid}"
    # Two stage flow from the client: "capture" logs the lead and stores the
    # hosted copy the moment the gate is unlocked (so closing the tab loses
    # nothing), then "send" arrives a few seconds later with the PDF and
    # triggers the emails. A request with no stage does both at once.
    stage = d.get("stage") or "both"
    if stage in ("capture", "both") and not lead["resend"]:
        _append(LEADS_PATH, lead)
        app.logger.info("LEAD %s | %s | %s | %s | %s", email, lead["company"], lead["verdict"], lead["headline"], report_url)

    # The client sends the report exactly as rendered on screen (standalone HTML)
    # plus a PDF of the same page. Both are stored so the email can link to a
    # hosted copy and attach the PDF.
    pdf_raw = _save_report(rid, d.get("report_doc") or "", d.get("pdf_b64") or "")
    if stage == "capture":
        return jsonify({"ok": True, "emailed": False, "report_id": rid, "report_url": report_url})
    if not pdf_raw:
        _, pdf_path = _report_paths(rid)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_raw = f.read()
    attachments = None
    if pdf_raw:
        attachments = [{"filename": _company_slug(lead["company"]), "content": base64.b64encode(pdf_raw).decode()}]

    summary = d.get("summary") or {}
    if not summary and (d.get("verdict") or d.get("headline")):
        summary = {"verdict": d.get("verdict"), "headline": d.get("headline")}
    emailed = False
    if summary:
        html = prospect_email(lead["name"], summary, report_url, bool(pdf_raw))
        # Split test: when the report is held for post podcast delivery, skip the
        # prospect send. The internal copy below still goes out so the host walks
        # into the recording already holding the findings.
        send_to_prospect = d.get("send_to_prospect", True)
        if send_to_prospect:
            emailed, _ = send_resend(email, "Your Infrastructure Resilience Report | Protected Harbor", html, attachments)
        if not lead["resend"] and NOTIFY_EMAIL:
            send_resend(
                NOTIFY_EMAIL,
                f"New resilience audit lead: {lead['company'] or email} ({lead['verdict']})"
                + (" | REPORT HELD for post podcast delivery" if not send_to_prospect else ""),
                f"<p><b>{_esc(lead['name'])} {_esc(lead['last_name'])}</b> | {_esc(lead['company'])} | {_esc(lead['role'])} | {_esc(lead['industry'])}<br>"
                f"{_esc(email)}<br>Verdict: <b>{_esc(lead['verdict'])}</b><br>{_esc(lead['headline'])}</p>"
                f"<p><a href=\"{report_url}\">Open the hosted report</a>"
                + (f" &middot; <a href=\"{report_url}.pdf\">PDF</a>" if pdf_raw else "") + "</p>"
                + ("<p>PDF attached.</p>" if pdf_raw else ""),
                attachments,
            )
    return jsonify({"ok": True, "emailed": emailed, "report_id": rid, "report_url": report_url})


def _admin_ok():
    return ADMIN_KEY and request.args.get("key") == ADMIN_KEY


@app.route("/api/leads")
def api_leads():
    if not _admin_ok():
        return jsonify({"error": "key"}), 403
    return jsonify(_load(LEADS_PATH))


@app.route("/api/leads.csv")
def api_leads_csv():
    if not _admin_ok():
        return jsonify({"error": "key"}), 403
    rows = _load(LEADS_PATH)
    cols = ["ts", "email", "name", "last_name", "company", "role", "industry", "headline", "verdict", "delivery", "report_id"]
    out = [",".join(cols)]
    for r in rows:
        out.append(",".join('"' + str(r.get(c, "")).replace('"', '""') + '"' for c in cols))
    return Response("\n".join(out), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.route("/api/event", methods=["POST"])
def api_event():
    d = request.get_json(force=True, silent=True) or {}
    ev = {"ts": datetime.now(timezone.utc).isoformat(), "name": str(d.get("name", ""))[:60],
          "props": d.get("props", {})}
    _append(EVENTS_PATH, ev)
    app.logger.info("EVENT %s %s", ev["name"], json.dumps(ev["props"])[:200])
    return jsonify({"ok": True})


@app.route("/api/events")
def api_events():
    if not _admin_ok():
        return jsonify({"error": "key"}), 403
    return jsonify(_load(EVENTS_PATH))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

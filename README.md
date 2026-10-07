# LeakGuard Finance Backend API v0.1

## Endpoints
GET /health
POST /analyze

POST /analyze accepts multipart files:
- ap_file: AP CSV
- ar_file: AR CSV
- analysis_date: YYYY-MM-DD

Returns analysis_run_id, metrics and structured findings.

## Production architecture
Jotform → automation/webhook → POST /analyze → engine → JSON findings → findings database → Jotform dashboard.

## Important
This service is not deployed publicly from this ChatGPT session. It is deployment-ready code. A public HTTPS endpoint and an automation connector/webhook are still required before the Jotform upload button can call it in production.

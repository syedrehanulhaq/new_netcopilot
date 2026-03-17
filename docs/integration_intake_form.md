# Customer Integration Intake Form

Use this form to collect the minimum details required to integrate any external tool.

## 1) Tool Overview
- Tool name:
- Vendor / product:
- Use cases (what should NetCopilot do?):
  - [ ] Read alerts
  - [ ] Create/update tickets
  - [ ] Fetch metrics/logs
  - [ ] Query inventory
  - [ ] Other:

## 2) API Access
- Base API URL (include region/tenant):
- Auth method:
  - [ ] API token
  - [ ] OAuth2
  - [ ] Basic auth
  - [ ] mTLS
  - [ ] Service account
- Credentials provided (type + delivery method):
- Required scopes/permissions (least‑privilege):
- Is there a sandbox/test tenant? If yes, details:

## 3) API Documentation
- OpenAPI / Swagger URL:
- Other API docs (PDF/wiki/Postman collection):
- Example requests/responses (if docs are restricted):

## 4) Data & Mapping
- Key objects to integrate (alerts, tickets, devices, metrics, etc.):
- Required fields:
- Field mapping (tool → NetCopilot):
  - Example:
    - device_name →
    - interface →
    - severity →
    - timestamp →

## 5) Network Connectivity
- Where will NetCopilot run (on‑prem/VPC)?
- Is the API reachable from that network?
- Firewall/allowlist requirements:

## 6) Limits & Reliability
- Rate limits / quotas:
- Expected request volume:
- Required retry/backoff behavior:

## 7) Security & Compliance
- Data retention constraints:
- Logging requirements:
- PII/PHI restrictions:

## 8) Success Criteria
- What should a successful integration demo show?
- Who signs off on acceptance?


> **Status (October 2026):** version 2 follows the direction in this brief and iPay's alignment plan: iPay decides, moves the money and records the payment; this app is a thin front door that asks iPay through the plugin API and records only on iPay's signed instruction. Sections below that describe calling the transfers API directly ("Option B") are historical. See the README.

# iPay Integration for ERPNext — Scope & Handoff Brief

**App name:** `ipay_integration` · **Module:** "iPay Integration"
**Target:** Frappe/ERPNext v15 (with a v16 branch — see §7)
**Owner:** Doftwerks West Africa Limited
**Brief written:** 20 Aug 2026

> This document is the working spec for a fresh session. It carries the business context, the
> iPay API contract as currently known, the ERPNext design, and — importantly — the patterns
> and mistakes learned building `doftwerks_nrs` (the NRS e-invoicing app by the same team,
> already on the Frappe marketplace). **Do not re-derive those lessons; reuse them.**

---

## 1. What we are building and why

**Product in one line:** pay vendor bills and collect customer payments through iPay, without
leaving ERPNext.

**The market gap** (from an internal five-ERP study of Zoho, Sage, QuickBooks, Xero, Dynamics):
every major ERP has the same three blanks for Nigeria — no local vendor payout, no withholding
tax at source, no NRS/FIRS e-invoicing. AR (collecting) is a commodity: four Nigerian gateways
all priced at 1.5% capped ₦2,000. **AP (paying vendors) is the open field** — no competitor
pays a Nigerian vendor from inside the ledger. Even the big AP tools (Bill.com, Tipalti, Melio)
execute in their own app and sync back.

**Why iPay can do the hard half:** iPay already runs a payout rail for its payroll product
(`dev.payroll.ipaysuite.app`). The licensed, capital-intensive money-out capability — the
reason competitors stop at collections — already exists.

**Strategic shape:** "Bill.com for Nigeria, with compliance built in." The differentiator no
exemplar has is WHT deducted at source plus the NRS e-invoice filed in the same flow. The
e-invoicing half already exists as `doftwerks_nrs`, so these two apps compose.

**A working Zoho widget exists** (browser-side, in the Zoho Books extension). It proves the
payout flow end-to-end but is constrained by running in a browser: it embeds OAuth credentials,
tries three fallback routes to mark a bill paid, cannot receive webhooks, and hardcodes the
vendor bank account. **ERPNext removes every one of those constraints** — see §4.6.

---

## 2. Current status — what is blocked

> **⚠ ADDENDUM (7 Sep 2026): Phase 0 is DONE — the contract below is superseded.**
> The authoritative Postman collection landed and **B1 is answered: arbitrary
> `bankAccountNumber` + `bankCode` per transfer ARE accepted** (no vendor
> pre-registration). What the confirmed contract changed vs. §3 as written:
>
> - **`x-idempotency-key` must be a UUID we generate** — one per payment intent,
>   stored (`Purchase Invoice.ipay_idempotency_key` + the iPay Transaction row)
>   and **reused verbatim on every retry**. A fresh key on a retry is a second
>   payment. It is NOT the invoice name anymore.
> - **Check-status exists**: `GET /api/v1/transactions/check-status/{idempotencyKey}`
>   (lookup by OUR key; 404 = iPay never received the transfer, safe to re-send).
> - **`Processing` is acceptance, not settlement** (`success:true`, HTTP 200).
>   The Payment Entry is only created after check-status reports `Successful` —
>   enqueued poll + a `*/10` cron safety net (`poll_pending_transfers`).
> - **`Content-Type: application/json` is required on EVERY request, GETs
>   included** — a missing one returns 415.
> - Response shape: parse by the **`success` flag**; `message` on success,
>   **`errorMessage` on failure**. 409 = duplicate/still processing (re-check,
>   don't re-send); 502 = gateway-side, retry with the SAME key.
> - Body additions: `vendorPaymentBankId` = the provisioned "iPay" ledger
>   account id (same account the PE is paid from); `senderErp: "erpnext"`.
> - Wallet balance is **indicative only** (ignores in-flight) — never a spend
>   gate; a true shortfall comes back as a 422 on initiate.
> - Amounts are **major units (naira)**, confirmed.
> - The audit/idempotency ledger is the **iPay Transaction** doctype (unique
>   `idempotency_key`, request/response snapshots, links to PI + PE).
> - Vendor bank capture: native Bank Account (party_type=Supplier) + a curated
>   `ipay_nip_code` Custom Field on Bank, shipped with `fixtures/bank.json`.
>   Commercial-bank codes are standard; **fintech codes (Opay 999992, Kuda
>   090267, Moniepoint 50515, PalmPay 999991) still need backend confirmation.**
> - Still open with the iPay backend: confirm it accepts `senderErp:"erpnext"`
>   and how it uses `vendorId`/`vendorPaymentBankId`; webhooks remain Phase 3.

**Phase 0 (API contract) is not finished.** One question gates the whole design:

> **B1 — Does `POST /api/v1/transactions/initiate` accept arbitrary `bankAccountNumber` +
> `bankCode` per transfer, or must vendors be pre-registered on iPay to get a `vendorId`?**

The Zoho widget passes *Zoho's* vendor id as `vendorId` and hardcodes the bank account with the
comment *"vendor bank details must come from iPay's vendor records — Zoho bills don't carry
them."* So no shipped integration has paid a real arbitrary vendor yet.

- If **arbitrary bank details are accepted** → ERPNext is ideal; supplier bank data is native
  (§4.3) and there is no registration step. Build proceeds immediately.
- If **pre-registration is required** → we need vendor create/lookup/list endpoints, and the
  Supplier onboarding flow grows an "register with iPay" step plus an ID mapping field.

Other open items in §3.4. **Do not design the Supplier flow until B1 is answered.**

**Safe to build before B1 lands:** app scaffold, settings doctype with per-company credentials,
OAuth token acquisition + caching, wallet-balance display, Test Connection button. All of that
is independent of the vendor question.

---

## 3. The iPay API (as currently known)

Base (sandbox/dev): `https://dev.payroll.ipaysuite.app`
Sources: a Postman collection + the live Zoho widget. **No official spec yet.**

### 3.1 Auth — OAuth2 client credentials
```
POST /oauth/token
Authorization: Basic base64(clientId:clientSecret)
Accept: application/json
form fields: grant_type=client_credentials
             scope=transfers:create transfers:status transfers:walletBalance
→ { access_token, expires_in, ... }
```
Cache the token; refresh ~60s before expiry. Note this differs from `doftwerks_nrs`, which used
static `x-client-id`/`x-client-secret` headers on every call.

### 3.2 Payout (confirmed working)
```
POST /api/v1/transactions/initiate
Authorization: Bearer <token>
x-client-id: <clientId>
x-timestamp: <unix seconds>          # purpose undocumented — replay protection?
x-idempotency-key: <our reference>   # MUST be our ERP document reference
{
  "vendorId": "...",                 # see B1
  "senderErp": "erpnext",            # widget sends "zoho"
  "bankAccountNumber": "0127257894",
  "bankCode": "058",
  "currency": "NGN",
  "amount": 500,                     # appears to be MAJOR units (naira), confirm
  "description": "Payment for bill PINV-2026-00013"
}
→ { code: 0, success: true, message: "Transfer Successful",
    data: { dateTime, reference: "qck-trsf_…", amount, status: "Successful" } }
```
The widget also sends `reference`, `erpBillId`, `erpOrgId` (unconfirmed whether stored).
Transaction id arrives as `data.reference`.

### 3.3 Wallet balance (confirmed working)
```
GET /api/v1/transactions/wallet-balance
Authorization: Bearer <token>
x-client-id: <clientId>
→ { code: 0, success: true, message: "Wallet Balance: N", data: { balance: N } }
```

### 3.4 Not yet available / undefined
| Gap | Impact |
|---|---|
| **Transfer status endpoint** (scope `transfers:status` exists, endpoint undocumented) | Cannot confirm a payout landed; cannot reconcile |
| **Transfer webhooks** + HMAC signing | No async outcome; cannot handle later failure or reversal |
| **Amount units** — decimal naira vs kobo | Float/rounding drift risk between systems |
| **Error code table** | Widget guesses success across six shapes; we need stable codes |
| **Bank list** endpoint | Plugin must source `bankCode` values |
| **Name enquiry** (verify account before paying) | Prevents the least recoverable AP error |
| **Collections / AR** — nothing exists | Determines whether we ship AP-only |
| PIN | Widget shows a PIN screen but never transmits it — cosmetic? |

---

## 4. ERPNext design

### 4.1 DocTypes (app-owned)
- **iPay Settings** (Single): `enabled`, `base_url`, `auto_*` toggles, `accounts` (Table →
  iPay Account), HTML onboarding banner, Test Connection + Copy Webhook URL buttons.
- **iPay Account** (child table): `company` (Link → Company — **the entity selector**),
  `client_id`, `client_secret` (**Password / encrypted**), plus any iPay-side identifiers.
  Sandbox vs production is determined **by the credentials**, not by an environment field.
  (The NRS app shipped an environment Select and had to remove it — do not repeat.)

### 4.2 Custom Fields (fixtures, filtered by `ipay_` prefix)
- **Purchase Invoice:** `ipay_status` (read-only), `ipay_reference`, `ipay_error`,
  `ipay_paid_at`. Group in a section after totals; keep the section printable but tidy
  (two-column with a Column Break — learned on NRS).
- **Supplier:** whatever B1 requires (e.g. `ipay_vendor_id`) — **decide after B1**.
- **Sales Invoice:** AR fields only if/when collections exist.

### 4.3 Vendor bank details — use ERPNext natives
ERPNext's **Bank Account** doctype already has `party_type` / `party` (link to Supplier),
`bank_account_no`, `bank`, `branch_code`, `is_default`. Read the supplier's default Bank
Account rather than inventing fields. A custom `ipay_bank_code` field on Bank Account may be
needed if `branch_code` doesn't match iPay's bank code list.

### 4.4 Flows
**AP (build first):**
1. Purchase Invoice submitted and approved (ERPNext **Workflow** provides maker-checker).
2. "Pay via iPay" button appears on submitted, unpaid bills with no `ipay_reference`.
3. Pre-flight collects **all** problems at once: no supplier bank account, missing bank code,
   wallet balance below bill amount, already paid, currency unsupported (NGN only at launch).
4. On confirm: acquire token → POST initiate with `x-idempotency-key = <invoice name>`.
5. On success: write `ipay_reference` + status, then **create a Payment Entry** against the
   bill (this is the whole "mark paid" problem — trivial in ERPNext, three fallback routes in
   the Zoho widget).
6. On webhook `transfer.failed`: cancel the Payment Entry, set status, notify.
   On `transfer.reversed`: reopen the bill. **Reversal handling is mandatory, not optional.**

**AR (only when collections exist):** Payment Request → hosted checkout → webhook →
Payment Entry → invoice Paid. If `doftwerks_nrs` is installed, that Payment Entry already
triggers the NRS PAID push — free integration between the two apps.

**WHT (phase 3):** ERPNext has a native **Tax Withholding Category** doctype. Ship Nigerian
rates as fixtures; deduct in the pay step (vendor receives net, WHT accrues to a payable).
Depends on whether iPay splits the payout server-side — an open business decision.

### 4.5 Hooks inventory (mirror the NRS app)
`doc_events` on Purchase Invoice (validate/on_submit/before_cancel as needed);
`scheduler_events` daily for reconciliation; `doctype_js` for form buttons;
`doctype_list_js` for status pills; `fixtures` filtered; `before_tests` shim;
`add_to_apps_screen`; a workspace.

### 4.6 Why ERPNext beats the Zoho widget (design confidence)
| Zoho widget constraint | ERPNext |
|---|---|
| Embeds Zoho OAuth client secret + refresh token in the HTML | Writes to its own DB; no external auth at all |
| Three fallback routes to mark a bill paid | One `Payment Entry` insert |
| Credentials in browser `localStorage` | Encrypted Password field, server-side |
| Cannot receive webhooks (browser) | Real webhook endpoint (pattern already built in NRS) |
| Vendor bank account hardcoded | Native Bank Account per Supplier |
| Marks paid on the synchronous response; never learns of reversal | Webhook + daily reconciliation |

---

## 5. Patterns to reuse from `doftwerks_nrs` (hard-won — do not re-derive)

The NRS app lives at `~/frappe-bench/apps/doftwerks_nrs`; read `docs/CONTEXT.md` and
`einvoice.py` for working implementations of every item below.

**Structural**
- All server logic in one module (`einvoice.py` there → `ipay.py` here) — easy to review/audit.
- Per-company credential child table; resolve entity by `doc.company`.
- Secrets **only** via `row.get_password("client_secret")`, read at call time into headers.
  Never logged, never in fixtures, never in code. The marketplace repo is public.
- Fixtures **filtered** (`[["dt","in",[…]]]` + fieldname prefix) — never a bare
  `fixtures = ["Custom Field"]`, which would export every field on the site.

**Correctness traps**
- **Lazy logger.** `frappe.logger()` at module level opens files at import time and breaks
  imports from arbitrary working directories. Wrap in a `_logger()` function.
- **No `frappe.db.commit()` in document hooks.** The Frappe Cloud marketplace audit fails it
  (`frappe-manual-commit`, Major), and v16 forbids it outright. The framework commits at the
  end of a successful request.
- **`db_set(..., update_modified=False)`** when writing to already-submitted documents.
- **Notification Log directly.** Writes via `db_set` do NOT fire the Notification doctype's
  "Value Change" trigger, so create Notification Log rows in code for staff alerts.
- **Idempotency guard**: if `ipay_reference` is already set, skip — makes retries safe.
- **Pre-flight collects ALL errors**, then writes them joined. One error per attempt is a
  terrible staff experience.
- **Friendly error map**: list of `(regex, plain guidance)`, **specific before general**, and
  use word boundaries — a bare `"tin"` substring matched inside "Accoun**tin**g" and
  mistranslated errors for days before a unit test caught it.

**Webhook receiver (pattern proven in production)**
- `@frappe.whitelist(allow_guest=True, methods=["POST"])` + `@rate_limit(limit=120, seconds=60)`.
- **Verify HMAC signature** — insist iPay signs from day one; NRS shipped unsigned and it is
  still a retrofit item.
- Acknowledge non-status events (portal "test POST on save") with 200.
- Return **404 for an unknown reference** so the platform redelivers — this covers the race
  where the event arrives before our transaction commits.
- **Forward-only status transitions**; a replayed older event must never downgrade a record.
- Expose the URL on the settings page with a **Copy Webhook URL** button
  (`frappe.urllib.get_base_url() + "/api/method/…"`), since each client site has its own.

**UX**
- Action button on the form via `doctype_js`; status pills in the list via `doctype_list_js`
  (wrap ERPNext's existing `get_indicator`, don't replace it).
- Onboarding banner (HTML field) on Settings explaining that credentials come from iPay, with
  contact details — this is the commercial funnel for anyone who installs from the marketplace.
- Workspace so the app appears in the sidebar.

---

## 6. Phase plan

| Phase | Scope | Gate |
|---|---|---|
| **0** | API contract; sandbox credentials; **one payout via curl alone** | B1 answered |
| **1** | Scaffold, Settings + iPay Account, OAuth token handling, Test Connection, wallet balance | — |
| **2** | AP: Pay via iPay on Purchase Invoice, pre-flight, Payment Entry on success, status field, retry button, list pills | Needs B1 |
| **3** | Webhook receiver (signed), failure/reversal handling, daily reconciliation, notifications | Needs webhook spec |
| **4** | WHT at source; NRS e-invoice linkage (the differentiator) | Business decision |
| **5** | AR/collections | Only if the platform builds them |
| **6** | Tests, CI green, security pass, docs, marketplace listing | — |

---

## 7. Marketplace requirements (learned the hard way with `doftwerks_nrs`)

- `pyproject.toml` **must** contain `[tool.bench.frappe-dependencies]` with `frappe` and
  `erpnext` version ranges, or the marketplace "Add App" dialog rejects the repo.
- **Version 16 support is mandatory for new apps**: a separate `version-16` branch with its own
  CI workflow testing against Frappe/ERPNext version-16 (Python 3.14, Node 24, MariaDB 11.8).
  Branch model: `develop` → `main` (v15 track), and merge `main` → `version-16`.
- CI must pin `bench init --frappe-branch version-15` (or 16) — unpinned pulls `develop` and
  fails on Python version syntax.
- CI must install **erpnext** before the app, and the app needs its **own** `before_tests`
  shim: ERPNext v16 removed `erpnext.setup.utils.before_tests`, so write a wrapper that
  delegates to it when present and completes the setup wizard when not.
- The marketplace **long description must contain no install instructions and no links** —
  the automated auditor flags both (Minor warnings). Keep them in the README instead.
- Logo must be **square**, ≥300×300.
- Repo hygiene: `.gitattributes` with `* text=auto eol=lf`; never run git as root
  (it leaves root-owned `.git/objects` and breaks later commits).

---

## 8. Testing

- Unit tests under `ipay_integration/tests/`, run with
  `bench --site <site> set-config allow_tests true` then `run-tests --app ipay_integration`.
- Highest-value targets: payload builder, amount/unit conversion, pre-flight completeness,
  webhook signature verification, forward-only status transitions, idempotency guard,
  friendly-error ordering.
- Tests on the NRS app caught two real regressions that manual testing missed. Write them
  early, not at the end.
- **Never** run a real payout in a test. Mock the HTTP layer; use the sandbox for manual
  end-to-end only.

---

## 9. First actions for the next session

1. Confirm whether **B1** has been answered. If not, everything in §4.3/§4.4 stays provisional.
2. Scaffold the app (see the sibling setup steps) and commit the initial structure.
3. Build Phase 1: Settings + iPay Account child table + OAuth token + Test Connection +
   wallet balance. All of it is safe without B1.
4. Read `~/frappe-bench/apps/doftwerks_nrs/einvoice.py` before writing the HTTP layer — the
   token/headers/error-handling shape there is proven and should be mirrored.

## 10. Reference material (on the Desktop, not in any repo — internal)

- `ipay_erpnext_api_contract.md` — the full endpoint contract requested from the platform team
- `ipay_api_gaps_round1.md` — current gap list, revised after reviewing the Zoho widget
- `iPay-ERP-Payment-Research 1.docx` — the five-ERP market study
- `doftwerks_nrs_developer_documentation.md` — the sibling app's dev docs

**Security note:** the Zoho widget file contains live Zoho OAuth credentials in a `ZOHO_DEMO`
block. They are being revoked. Never carry that pattern into ERPNext — there is no need to,
because the ERP writes to its own database.

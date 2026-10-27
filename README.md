### iPay Integration

> [!IMPORTANT]
> **`develop` is the main branch of this repository.** GitHub opens on `main` by default,
> but the latest code lives on [`develop`](https://github.com/doftwerks-dev/frappe-ipay-payment/tree/develop).
> Branch from `develop` and open pull requests against `develop`.
>
> - `develop` - active development (Frappe/ERPNext v15)
> - `main` - stable v15 releases, merged from `develop`
> - `version-16` - Frappe/ERPNext v16 releases

Pay vendor bills with iPay from inside ERPNext. This app is a **front door** to iPay.

| Who | Does |
|---|---|
| **ERPNext (this app)** | **Pay with iPay** on a Purchase Invoice asks iPay to pay it and opens iPay's confirmation page. It answers iPay's signed calls: read the invoice, record the payment, payment events. |
| **iPay** | Checks the supplier account with the bank, re-reads the invoice, applies the company's approval flow, asks the payer for their iPay password, the company finance PIN and an emailed code, moves the money and is the record of the payment. |

The app holds no iPay password or PIN, calls no transfer API and writes no Payment Entry on its own authority. A Payment Entry is created
only when iPay tells ERPNext to, in a signed and encrypted call, once per payment, and only if the invoice can still take it.

> **This repository is public. Never commit a client id, secret, key, token or site configuration.** Everything secret is entered in
> iPay Settings on the site, where Frappe encrypts it.

### Setup

1. Install the app. Give the people who may pay the role **iPay Payer** (an Accounts Manager role alone is not enough).
2. iPay registers an API client for each Company as an **ERP plugin** and gives you a client id, a client secret and five keys, shown once
   (see iPay's `docs/erp-plugin-api.md`, `erp-plugin:client`).
3. **iPay Settings**: add a row per Company and paste them in. Save, then **Test Connection**.
4. Give iPay this site's callback address, `https://your-site/ipay/plugin`, and the server's outgoing IP address. The site must be reachable
   by iPay over **https**.

Rotating keys: iPay issues new ones and keeps the old callback keys working for a window; put the old callback keys in the row's
"During a key rotation" section until it ends, then clear them.

### Paying a bill

1. On a submitted Purchase Invoice, **Pay with iPay**: confirm the supplier's bank and NUBAN account number.
2. A tab opens on iPay. Enter your iPay password, the finance PIN and the emailed code. With an approval flow in iPay, the request goes to
   the approvers and the last approval pays.
3. iPay tells ERPNext when the payment moves; the invoice shows its status and the Payment Entry is recorded against the **iPay** account.

### Security

- Every call from iPay is checked before anything is read: client, key id, a five minute clock window, an HMAC-SHA256 signature, a nonce
  never seen before, and the AES-256-GCM encrypted body. A failure is a plain 401 with the reason only in the log. Answers are signed and
  encrypted too.
- The confirmation link is a secret for 30 minutes: it goes straight to the payer's browser and is never stored or logged.
- ERPNext keeps only the last four digits of the supplier account on its own request record.

### Upgrading from version 1

The upgrade switches iPay off until each company is registered as a plugin and its keys are entered, and flags invoices that version 1 left
in flight (check them in iPay before paying again). **Rotate the old API client secret in iPay**: old backups still hold it. Version 1's
history stays under **iPay Transaction**, read only.

### Tests

`kit_tests/` checks the signing and encryption against messages produced by iPay's own PHP implementation:
`pip install cryptography requests pytest && pytest kit_tests`. The Frappe tests run with `bench --site test_site run-tests --app ipay_integration`.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app ipay_integration
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ipay_integration
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit

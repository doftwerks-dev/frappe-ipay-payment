### iPay Integration

> [!IMPORTANT]
> **`develop` is the main branch of this repository.** GitHub opens on `main` by default,
> but the latest code lives on [`develop`](https://github.com/doftwerks-dev/frappe-ipay-payment/tree/develop).
> Branch from `develop` and open pull requests against `develop`.
>
> - `develop` - active development (Frappe/ERPNext v15)
> - `main` - stable v15 releases, merged from `develop`
> - `version-16` - Frappe/ERPNext v16 releases

Pay vendor bills and collect payments through iPay, from inside ERPNext

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

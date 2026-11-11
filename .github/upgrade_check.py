"""The upgrade from version 1 to version 2, checked on a real site (CI, job "Upgrade from version 1").

	cd frappe-bench/sites && ../env/bin/python upgrade_check.py before <site>   # on the version 1 site
	bench --site <site> migrate                                                   # with version 2's code
	cd frappe-bench/sites && ../env/bin/python upgrade_check.py after <site>

"before" leaves what a version 1 site in use has: iPay switched on, an invoice whose payment was still in flight, one already paid,
and payment history. "after" checks what patches/v2_front_door.py and the new doctypes promise.
"""

import sys

import frappe

IN_FLIGHT = "UPGRADE-PINV-IN-FLIGHT"
PAID = "UPGRADE-PINV-PAID"
HISTORY = "UPGRADE-IPAY-TXN"


def before():
	frappe.db.set_single_value("iPay Settings", "enabled", 1)
	# Raw rows: only what version 1 wrote on them matters to the upgrade, not a whole accounting setup.
	for name, status in ((IN_FLIGHT, "Initiated"), (PAID, "Paid")):
		frappe.db.sql(
			"insert into `tabPurchase Invoice` (name, creation, modified, ipay_status) values (%s, now(), now(), %s)",
			(name, status),
		)
	frappe.db.sql("insert into `tabiPay Transaction` (name, creation, modified) values (%s, now(), now())", (HISTORY,))
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - a standalone script, nothing else commits for it


def after():
	failures = []

	def check(ok, what):
		print(("ok      " if ok else "FAILED  ") + what)
		if not ok:
			failures.append(what)

	check(not frappe.db.get_single_value("iPay Settings", "enabled"), "iPay Settings is switched off")
	check(
		"version 1" in (frappe.db.get_value("Purchase Invoice", IN_FLIGHT, "ipay_error") or ""),
		"the invoice version 1 left in flight is flagged",
	)
	check(not frappe.db.get_value("Purchase Invoice", PAID, "ipay_error"), "a paid invoice is left alone")
	check(bool(frappe.db.exists("iPay Transaction", HISTORY)), "version 1's payment history is kept")
	check(bool(frappe.db.exists("Role", "iPay Payer")), "the iPay Payer role exists")
	for doctype in ("iPay Payment", "iPay Recording", "iPay Call Nonce"):
		check(bool(frappe.db.exists("DocType", doctype)), f"the {doctype} doctype exists")
	service = "ipay.callbacks@ipay-integration.invalid"
	check(bool(frappe.db.get_value("User", service, "enabled")), "iPay's service user exists")
	check(
		set(frappe.get_all("Has Role", filters={"parent": service, "parenttype": "User"}, pluck="role")) == {"Accounts User"},
		"iPay's service user has Accounts User and nothing else",
	)
	for patch in ("ipay_integration.patches.create_ipay_payer_role", "ipay_integration.patches.v2_front_door"):
		check(bool(frappe.db.exists("Patch Log", {"patch": patch})), f"{patch} ran")
	if failures:
		sys.exit(1)


if __name__ == "__main__":
	step, site = sys.argv[1], sys.argv[2]
	frappe.init(site=site, sites_path=".")
	frappe.connect()
	try:
		{"before": before, "after": after}[step]()
	finally:
		frappe.destroy()

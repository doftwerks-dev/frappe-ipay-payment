// "Pay with iPay" on submitted, unpaid Purchase Invoices.
// It asks iPay to pay the invoice and opens iPay's confirmation page, where the payer enters their iPay password, the company
// finance PIN and an emailed code. Nothing secret is here: no keys, no PIN, no transfer call. iPay tells ERPNext (signed) when the
// payment moves, and the Payment Entry is recorded then.

const IPAY_OPEN = ["Awaiting Confirmation", "In Approval", "Processing", "Transferred"];

frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		const doc = frm.doc;
		if (doc.docstatus !== 1 || flt(doc.outstanding_amount) <= 0) {
			return;
		}
		if (!frappe.user.has_role("iPay Payer")) {
			return;
		}

		// a request is already open: never offer a second one, only a look at where it stands
		if (IPAY_OPEN.includes(doc.ipay_status)) {
			frm.add_custom_button(__("Check iPay Status"), () => ipay_check_status(frm));
			return;
		}

		frm.add_custom_button(__("Pay with iPay"), () => ipay_open_dialog(frm));
	},
});

function ipay_open_dialog(frm) {
	const doc = frm.doc;
	frappe.call({
		method: "ipay_integration.ipay.get_payment_context",
		args: { name: doc.name },
		freeze: true,
		freeze_message: __("Contacting iPay..."),
		callback: (r) => {
			const ctx = r.message || {};
			if (!ctx.enabled) {
				frappe.msgprint({
					title: __("iPay Payment"),
					message: __("iPay integration is disabled. Enable it in iPay Settings."),
					indicator: "orange",
				});
				return;
			}
			if (!ctx.configured) {
				frappe.msgprint({
					title: __("iPay Payment"),
					message: __(
						"Company {0} is not set up for iPay yet. Add its client id, secret and keys in iPay Settings and Save.",
						[frappe.utils.escape_html(doc.company)]
					),
					indicator: "orange",
				});
				return;
			}
			ipay_show_dialog(frm, ctx);
		},
	});
}

function ipay_show_dialog(frm, ctx) {
	const esc = frappe.utils.escape_html;
	const amount = format_currency(ctx.outstanding, ctx.currency);

	const d = new frappe.ui.Dialog({
		title: __("Pay with iPay"),
		fields: [
			{ fieldtype: "HTML", fieldname: "summary" },
			{ fieldtype: "Section Break", label: __("Supplier's Bank Account") },
			{
				fieldtype: "Link",
				fieldname: "bank",
				label: __("Bank"),
				options: "Bank",
				reqd: 1,
				default: (ctx.bank && ctx.bank.bank) || "",
			},
			{
				fieldtype: "Data",
				fieldname: "account_number",
				label: __("Account Number (NUBAN)"),
				reqd: 1,
				default: (ctx.bank && ctx.bank.bank_account_no) || "",
				description: __(
					"10 digits. iPay verifies the account with the bank before paying, so a wrong number fails cleanly instead of mispaying."
				),
			},
		],
		primary_action_label: __("Continue in iPay"),
		primary_action(values) {
			if (!/^\d{10}$/.test(cstr(values.account_number).trim())) {
				frappe.msgprint(__("NUBAN account numbers are exactly 10 digits."));
				return;
			}
			d.hide();
			ipay_save_bank_and_pay(frm, values);
		},
	});

	d.fields_dict.summary.$wrapper.html(`
		<div style="margin-bottom: var(--margin-md);">
			<div><strong>${esc(ctx.supplier_name)}</strong> &mdash; ${__("bill")} ${esc(frm.doc.name)}</div>
			<div class="h4" style="margin: var(--margin-xs) 0;">${esc(amount)}</div>
			<div class="text-muted small">${__(
				"You will enter your iPay password, the finance PIN and an emailed code on iPay's page. Nothing is paid until you do."
			)}</div>
		</div>`);
	d.show();
}

function ipay_save_bank_and_pay(frm, values) {
	frappe.call({
		method: "ipay_integration.ipay.save_supplier_bank",
		args: {
			supplier: frm.doc.supplier,
			bank: values.bank,
			account_number: cstr(values.account_number).trim(),
		},
		freeze: true,
		freeze_message: __("Saving bank details..."),
		callback: () => ipay_request_payment(frm),
	});
}

function ipay_request_payment(frm) {
	// Open the tab straight away, from the click, so the browser allows it; it is pointed at iPay when the answer arrives. It must not
	// be opened with noopener: iPay's page reports back through window.opener.
	const tab = window.open("", "_blank");
	frappe.call({
		method: "ipay_integration.ipay.pay_invoice",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Asking iPay..."),
		error: () => {
			if (tab) tab.close();
		},
		callback: (r) => {
			const res = r.message || {};
			frm.reload_doc();
			if (res.route === "approval") {
				if (tab) tab.close();
				frappe.msgprint({
					title: __("Sent for approval"),
					message: __("iPay sent this payment to your approvers. It is paid when the last one approves."),
					indicator: "blue",
				});
				return;
			}
			if (res.confirm_url && tab) {
				// The link is a secret for 30 minutes: it goes to the payer's browser only, never to storage or a log.
				tab.location.href = res.confirm_url;
			} else {
				if (tab) tab.close();
				frappe.msgprint({ title: __("iPay Payment"), message: __("iPay did not give a confirmation link. Try again."), indicator: "red" });
			}
		},
	});
}

function ipay_check_status(frm) {
	frappe.call({
		method: "ipay_integration.ipay.refresh_payment_status",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Checking with iPay..."),
		callback: () => frm.reload_doc(),
	});
}

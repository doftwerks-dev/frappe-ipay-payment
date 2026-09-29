// "Pay via iPay" on submitted, unpaid Purchase Invoices.
// All secrets and API calls live on the server; this script only drives the
// dialog and calls whitelisted methods.

frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		const doc = frm.doc;
		if (doc.docstatus !== 1 || flt(doc.outstanding_amount) <= 0) {
			return;
		}

		// a transfer is in flight - never offer a second initiate
		if (doc.ipay_status === "Processing" || doc.ipay_status === "Initiated") {
			frm.add_custom_button(__("Check iPay Status"), () => ipay_check_status(frm));
			return;
		}

		// transfer settled but the Payment Entry is missing (ordering guard):
		// this button only re-records the ledger entry, never the transfer
		if (doc.ipay_status === "Paid") {
			frm.add_custom_button(__("Record iPay Payment"), () => {
				frappe.confirm(
					__(
						"The iPay transfer already succeeded (reference {0}) but no Payment Entry was recorded. Record it now? The transfer will NOT run again.",
						[frappe.utils.escape_html(doc.ipay_reference || "-")]
					),
					() => ipay_call_pay(frm)
				);
			});
			return;
		}

		const retrying = doc.ipay_status === "Failed" || doc.ipay_status === "Reversed";
		const label = retrying ? __("Retry iPay Payment") : __("Pay via iPay");
		frm.add_custom_button(label, () => ipay_open_dialog(frm));
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
						"Company {0} is not set up for iPay yet. Add its credentials in iPay Settings and Save.",
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

	let wallet_html;
	if (ctx.wallet && !ctx.wallet.error) {
		const low = flt(ctx.wallet.balance) < flt(ctx.outstanding);
		wallet_html = `<span class="indicator-pill ${low ? "orange" : "green"}">
			${__("Wallet balance")}: ${esc(ctx.wallet.formatted)}</span>` +
			(low
				? `<div class="text-muted small">${__(
						"Balance is indicative and ignores in-flight transfers - iPay makes the final call."
				  )}</div>`
				: "");
	} else {
		wallet_html = `<span class="text-muted">${__("Wallet balance unavailable")}${
			ctx.wallet && ctx.wallet.error ? ": " + esc(ctx.wallet.error) : ""
		}</span>`;
	}

	const d = new frappe.ui.Dialog({
		title: __("Pay via iPay"),
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
		primary_action_label: __("Pay {0}", [amount]),
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
			<div>${wallet_html}</div>
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
		callback: () => ipay_call_pay(frm),
	});
}

function ipay_call_pay(frm) {
	frappe.call({
		method: "ipay_integration.ipay.pay_invoice",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Sending transfer to iPay..."),
		callback: (r) => {
			const res = r.message || {};
			frm.reload_doc();
			if (res.processing) {
				frappe.msgprint({
					title: __("iPay Payment"),
					message: frappe.utils.escape_html(
						res.message ||
							__("iPay accepted the transfer. It will be recorded automatically once it settles.")
					),
					indicator: "blue",
				});
			} else if (res.ok && res.warning) {
				frappe.msgprint({
					title: __("iPay Payment"),
					message: frappe.utils.escape_html(res.warning),
					indicator: "orange",
				});
			} else if (res.ok) {
				frappe.show_alert({
					message: __("Paid via iPay. Reference: {0}", [
						frappe.utils.escape_html(res.reference || "-"),
					]),
					indicator: "green",
				});
			} else {
				frappe.msgprint({
					title: __("iPay Payment"),
					message: (res.errors || [__("Payment failed - check iPay Settings.")])
						.map((e) => frappe.utils.escape_html(e))
						.join("<br>"),
					indicator: "red",
				});
			}
		},
	});
}

function ipay_check_status(frm) {
	frappe.call({
		method: "ipay_integration.ipay.refresh_transfer_status",
		args: { name: frm.doc.name },
		freeze: true,
		freeze_message: __("Checking transfer status with iPay..."),
		callback: (r) => {
			const res = r.message || {};
			frm.reload_doc();
			if (res.status === "Successful") {
				frappe.show_alert({
					message: __("Transfer settled. Reference: {0}", [
						frappe.utils.escape_html(res.reference || "-"),
					]),
					indicator: "green",
				});
			} else if (res.status === "Processing" || res.status === "Initiated") {
				frappe.show_alert({
					message: __("Still settling on iPay's side - it will be recorded automatically."),
					indicator: "blue",
				});
			} else if (res.error) {
				frappe.msgprint({
					title: __("iPay Status"),
					message: frappe.utils.escape_html(res.error),
					indicator: "red",
				});
			}
		},
	});
}

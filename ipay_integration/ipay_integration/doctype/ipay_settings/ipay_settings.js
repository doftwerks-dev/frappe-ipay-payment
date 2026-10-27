frappe.ui.form.on("iPay Settings", {
	setup(frm) {
		frm.set_query("payment_account", "accounts", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				filters: { company: row.company, is_group: 0, account_type: "Bank" },
			};
		});
	},

	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "ipay_integration.ipay.test_connection",
				freeze: true,
				freeze_message: __("Contacting iPay..."),
				callback: (r) => {
					const rows = r.message || [];
					if (!rows.length) {
						frappe.msgprint({
							title: __("iPay Connection Test"),
							message: __("No iPay accounts configured yet. Add a row and save first."),
							indicator: "orange",
						});
						return;
					}
					const html = rows
						.map(
							(x) =>
								`${x.ok ? "✅" : "❌"} <strong>${frappe.utils.escape_html(x.company)}</strong>: ` +
								frappe.utils.escape_html(x.detail)
						)
						.join("<br>");
					frappe.msgprint({
						title: __("iPay Connection Test"),
						message: html,
						indicator: rows.every((x) => x.ok) ? "green" : "red",
					});
				},
			});
		});
	},
});

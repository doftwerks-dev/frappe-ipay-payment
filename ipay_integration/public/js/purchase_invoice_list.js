// Surface iPay payment state in the Purchase Invoice list view.
// Loads after ERPNext's listview settings (app load order), so we wrap the
// existing get_indicator instead of replacing the whole settings object.
(function () {
	const settings =
		frappe.listview_settings["Purchase Invoice"] ||
		(frappe.listview_settings["Purchase Invoice"] = {});

	settings.add_fields = (settings.add_fields || []).concat(["ipay_status", "ipay_reference"]);

	const original_get_indicator = settings.get_indicator;

	settings.get_indicator = function (doc) {
		if (doc.docstatus === 1) {
			if (["Awaiting Confirmation", "In Approval", "Processing", "Transferred", "Initiated"].includes(doc.ipay_status)) {
				return [__("iPay: {0}", [__(doc.ipay_status)]), "blue", "ipay_status,=," + doc.ipay_status];
			}
			if (["Failed", "Rejected", "Cancelled"].includes(doc.ipay_status)) {
				return [__("iPay {0}", [__(doc.ipay_status)]), "red", "ipay_status,=," + doc.ipay_status];
			}
			if (doc.ipay_status === "Reversed") {
				return [__("iPay Reversed"), "orange", "ipay_status,=,Reversed"];
			}
			if (doc.ipay_status === "Paid") {
				return [__("Paid via iPay"), "green", "ipay_status,=,Paid"];
			}
		}
		return original_get_indicator ? original_get_indicator(doc) : undefined;
	};
})();

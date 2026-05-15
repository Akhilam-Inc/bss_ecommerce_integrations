// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.ui.form.on('Ecommerce Integration Log', {
	refresh: function(frm) {
		if (frm.doc.request_data && frm.doc.status=='Error'){
			frm.add_custom_button('Retry', function() {
				frappe.call({
					method:"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log.resync",
					args:{
						method:frm.doc.method,
						name: frm.doc.name,
						request_data: frm.doc.request_data
					},
					callback: function(r){
						frappe.msgprint(__("Reattempting to sync"))
					}
				})
			}).addClass('btn-primary');

			if (frm.doc.method && frm.doc.method.includes('sync_sales_order')) {
				frm.add_custom_button('Log Order Object', function() {
					frappe.call({
						method: "ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log.log_order_object",
						args: {
							name: frm.doc.name,
							request_data: frm.doc.request_data
						},
						callback: function(r) {
							frappe.msgprint(__("Order object logged to Error Log. Check Error Log for title: 'Debug: SO Object - " + frm.doc.name + "'"));
						}
					});
				});
			}
		}
	}
});

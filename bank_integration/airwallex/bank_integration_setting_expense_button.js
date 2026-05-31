// Client Script
// DocType: Bank Integration Setting
// Apply To: Form
//
// Adds a manual "Import Expenses to Draft PIs" button under the Airwallex group.
// Self contained: it does not modify the app's bank_integration_setting.js.

frappe.ui.form.on('Bank Integration Setting', {
    refresh: function (frm) {
        if (frm.doc.enable_airwallex) {
            frm.add_custom_button(__('Import Expenses to Draft PIs'), function () {
                const d = new frappe.ui.Dialog({
                    title: __('Import Airwallex Expenses'),
                    fields: [
                        {
                            fieldname: 'from_date',
                            label: __('From (expense created)'),
                            fieldtype: 'Date',
                            reqd: 1,
                            default: frappe.datetime.add_days(frappe.datetime.get_today(), -30)
                        },
                        {
                            fieldname: 'to_date',
                            label: __('To (expense created)'),
                            fieldtype: 'Date',
                            reqd: 1,
                            default: frappe.datetime.get_today()
                        },
                        {
                            fieldtype: 'HTML',
                            options: '<p class="text-muted small">Imports APPROVED expenses only as draft Purchase Invoices. Safe to re-run: already imported expenses are skipped.</p>'
                        }
                    ],
                    primary_action_label: __('Start Import'),
                    primary_action: function (values) {
                        d.hide();
                        frappe.call({
                            method: 'bank_integration.airwallex.expense_invoice.enqueue_expense_import',
                            args: { from_date: values.from_date, to_date: values.to_date },
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.show_alert({
                                        message: __('Import queued. You will be notified when it finishes.'),
                                        indicator: 'blue'
                                    });
                                }
                            }
                        });
                    }
                });
                d.show();
            }, __('Airwallex'));
        }

        // Refresh fires every time the form re-renders, so drop any
        // previous listener before re-binding. Without this guard the
        // single "import complete" event fans out into one msgprint per
        // accumulated handler.
        frappe.realtime.off('expense_import_complete');
        frappe.realtime.on('expense_import_complete', function (data) {
            frappe.msgprint({
                title: __('Airwallex Expense Import'),
                indicator: data.status === 'success' ? 'green' : 'orange',
                message: data.message
            });
        });
    }
});

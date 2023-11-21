import frappe
from frappe.desk.form.linked_with import get_linked_docs, get_linked_doctypes
import requests

def on_sales_invoice_update(sales_invoice, trigger):
    if sales_invoice.status == "Paid":
        si = sales_invoice.as_dict()
        items = si.get("items")
        item = items[0]
        so_name = item.get("sales_order")
        frappe.db.set_value("Sales Order", so_name, "status", "Closed")
        
        # update woocommerce order in wordpress
        so = frappe.get_doc("Sales Order", so_name)
        if hasattr(so, "woocommerce_id") and so.woocommerce_id:
            woocommerce_settings = frappe.get_doc("Woocommerce Settings")
            url = f"{woocommerce_settings.woocommerce_server_url}/wp-json/qwave-booking/v1/complete-order"
            params = {
                "order_id": so.woocommerce_id,
                "status": "completed"
            }
            requests.get(url, params=params)


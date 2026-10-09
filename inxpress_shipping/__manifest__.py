{
    "name": "InXpress Shipping",
    "version": "18.0.1.0.0",
    "category": "Delivery",
    "summary": "Live multi-carrier shipping rates via InXpress",
    "description": """
Live multi-carrier shipping rates, dispatch, labels and tracking via InXpress.

Quote a sales order against your own InXpress carrier rates, book the shipment
from the delivery order using the boxes actually packed, and get the label and
tracking number back in Odoo. Supports both parcel and freight (LTL) shipping,
international customs declarations, and scheduled tracking updates.

Requires an InXpress account.
""",
    "author": "InXpress",
    "website": "https://www.inxpress.com",
    "support": "support@inxpress.com",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": [
        "delivery",
        "sale",
        # hs_code / country_of_origin on product.template, and sale_line_id on
        # stock.move, both used to build customsItems
        "stock_delivery",
    ],
    "data": [
        "security/ir.model.access.csv",
        "wizard/inxpress_rate_wizard_views.xml",
        "views/delivery_carrier_views.xml",
        "views/product_views.xml",
        "views/sale_order_views.xml",
        "views/stock_picking_views.xml",
        "data/delivery_data.xml",
        "data/inxpress_cron.xml",
    ],
    "price": 0.00,
    "currency": "EUR",
    "installable": True,
    "application": False,
    "auto_install": False,
}

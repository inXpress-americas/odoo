from odoo import fields, models

# The eighteen NMFC classes, lowest first. Kept ordered so the order-level
# class can be picked by position rather than by parsing the label, and held as
# a Selection rather than free text because a mistyped class is not rejected by
# the carrier at quote time - it is corrected on the invoice weeks later.
FREIGHT_CLASSES = [
    "50", "55", "60", "65", "70", "77.5", "85", "92.5", "100",
    "110", "125", "150", "175", "200", "250", "300", "400", "500",
]


class ProductTemplate(models.Model):
    _inherit = "product.template"

    inxpress_freight_class = fields.Selection(
        selection=[(code, code) for code in FREIGHT_CLASSES],
        string="Freight Class",
        help="NMFC freight class for this product, used when the delivery "
             "method is in freight mode. An order carrying several classes is "
             "quoted at the highest one present. Left empty, the class "
             "configured on the delivery method is used instead.",
    )

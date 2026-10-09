import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class InXpressRateWizard(models.TransientModel):
    _name = "inxpress.rate.wizard"
    _description = "InXpress Rate Selection"

    order_id = fields.Many2one("sale.order", required=True, readonly=True)
    carrier_id = fields.Many2one("delivery.carrier", required=True, readonly=True)
    line_ids = fields.One2many("inxpress.rate.wizard.line", "wizard_id")
    selected_line_id = fields.Many2one("inxpress.rate.wizard.line")
    weight_unit = fields.Char(
        string="Weight Unit",
        readonly=True,
        help="Unit Odoo stores weights in. InXpress converts to its own "
             "units, so this is shown for reference and is not an input.",
    )
    total_weight = fields.Float(
        string="Total Weight",
        readonly=True,
        help="Total shipment weight, in the unit shown above.",
    )
    def action_refresh_rates(self):
        """Re-quote the order and reopen this wizard with the new rates."""
        self.ensure_one()
        self.carrier_id.inxpress_rate_shipment(self.order_id)
        return self.carrier_id.action_inxpress_view_rates(self.order_id)

    def action_apply_rate(self):
        """Apply the selected rate to the sale order."""
        self.ensure_one()
        line = self.selected_line_id
        if not line:
            raise UserError(_("Please select a rate."))

        order = self.order_id

        # Store the selected carrier/service on the order for dispatch
        order.inxpress_selected_carrier_code = line.carrier_code
        order.inxpress_selected_service_code = line.service_code
        order.inxpress_selected_service_level_code = line.service_level_code or ""
        order.inxpress_selected_service_name = line.service_name
        order.inxpress_selected_carrier_name = line.carrier_name
        order.inxpress_quote_data = line.quote_data

        # Set delivery line with selected price
        delivery_line = order.order_line.filtered(lambda l: l.is_delivery)
        if delivery_line:
            delivery_line.price_unit = line.price
            delivery_line.name = f"Shipping: {line.carrier_name} - {line.service_name}"
        else:
            # Add a delivery line via the standard flow
            order._create_delivery_line(self.carrier_id, line.price)

        return {"type": "ir.actions.act_window_close"}


class InXpressRateWizardLine(models.TransientModel):
    _name = "inxpress.rate.wizard.line"
    _description = "InXpress Rate Option"
    _order = "price asc"

    wizard_id = fields.Many2one("inxpress.rate.wizard", required=True, ondelete="cascade")
    carrier_code = fields.Char(string="Carrier", readonly=True)
    carrier_name = fields.Char(string="Carrier Name", readonly=True)
    service_name = fields.Char(string="Service", readonly=True)
    service_code = fields.Char(readonly=True)
    service_level_code = fields.Char(readonly=True)
    price = fields.Float(string="Price", readonly=True)
    transit_time = fields.Char(string="Transit Time", readonly=True)
    currency = fields.Char(readonly=True)
    quote_data = fields.Text(readonly=True, string="Quote JSON")

    def action_select(self):
        """Select this rate and apply it to the order."""
        self.ensure_one()
        self.wizard_id.selected_line_id = self
        return self.wizard_id.action_apply_rate()

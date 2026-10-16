from unittest.mock import patch

from odoo.tests import Form
from odoo.tests.common import TransactionCase, tagged

from ..models.inxpress_client import InXpressClient

RATES = {
    "rates": [
        {
            "carrierCode": "C_1",
            "carrierName": "DHL",
            "serviceCode": "DHL_EXP",
            "serviceName": "DHL Express Worldwide",
            "amount": 222.36,
        },
        {
            "carrierCode": "C_2",
            "carrierName": "UPS",
            "serviceCode": "UPS_SAVER",
            "serviceName": "UPS Worldwide Saver",
            "amount": 240.10,
        },
    ],
}


@tagged("post_install", "-at_install")
class TestInXpressDeliveryLineName(TransactionCase):
    """PUB-153: the shipping line names the carrier picked, then (InXpress)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        delivery_product = cls.env["product.product"].create({
            "name": "InXpress Delivery",
            "type": "service",
        })
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "InXpress",
            "delivery_type": "inxpress",
            "product_id": delivery_product.id,
            "inxpress_base_url": "https://example.invalid",
            "inxpress_api_token": "ixpx_test",
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Acme Belgium NV",
            "street": "Grote Markt 12",
            "city": "Brussels",
            "zip": "1000",
            "country_id": cls.env.ref("base.be").id,
        })
        cls.goods = cls.env["product.product"].create({
            "name": "Test Goods",
            "type": "consu",
            "weight": 5.0,
        })

    def _order(self):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {
                "product_id": self.goods.id,
                "product_uom_qty": 1,
            })],
        })

    def _delivery_line(self, order):
        return order.order_line.filtered("is_delivery")

    def test_add_shipping_names_the_cheapest_carrier(self):
        order = self._order()
        with patch.object(InXpressClient, "get_quote", return_value=RATES):
            wizard = Form(self.env["choose.delivery.carrier"].with_context(
                default_order_id=order.id,
                default_carrier_id=self.carrier.id,
            )).save()
            wizard.update_price()
            wizard.button_confirm()

        self.assertEqual(self._delivery_line(order).name, "DHL (InXpress)")
        self.assertEqual(self._delivery_line(order).price_unit, 222.36)

    def test_compare_rates_uses_the_same_name(self):
        order = self._order()
        order.inxpress_selected_carrier_name = "DHL"
        order.set_delivery_line(self.carrier, 222.36)
        order.inxpress_all_quotes = "[]"

        wizard = self.env["inxpress.rate.wizard"].create({
            "order_id": order.id,
            "carrier_id": self.carrier.id,
        })
        ups = self.env["inxpress.rate.wizard.line"].create({
            "wizard_id": wizard.id,
            "carrier_code": "C_2",
            "carrier_name": "UPS",
            "service_code": "UPS_SAVER",
            "service_name": "UPS Worldwide Saver",
            "price": 240.10,
        })
        ups.action_select()

        self.assertEqual(self._delivery_line(order).name, "UPS (InXpress)")
        self.assertEqual(self._delivery_line(order).price_unit, 240.10)

    def test_compare_rates_creates_the_line_with_the_name(self):
        order = self._order()
        wizard = self.env["inxpress.rate.wizard"].create({
            "order_id": order.id,
            "carrier_id": self.carrier.id,
        })
        dhl = self.env["inxpress.rate.wizard.line"].create({
            "wizard_id": wizard.id,
            "carrier_code": "C_1",
            "carrier_name": "DHL",
            "service_code": "DHL_EXP",
            "service_name": "DHL Express Worldwide",
            "price": 222.36,
        })
        dhl.action_select()

        self.assertEqual(self._delivery_line(order).name, "DHL (InXpress)")

    def test_no_carrier_picked_keeps_the_delivery_method_name(self):
        order = self._order()
        order.set_delivery_line(self.carrier, 10.0)

        self.assertEqual(self._delivery_line(order).name, "InXpress")

    def test_free_shipping_is_still_marked(self):
        self.carrier.write({"free_over": True, "amount": 1.0})
        order = self._order()
        order.inxpress_selected_carrier_name = "DHL"
        order.set_delivery_line(self.carrier, 0.0)

        self.assertEqual(
            self._delivery_line(order).name, "DHL (InXpress)\nFree Shipping")

    def test_other_delivery_methods_are_untouched(self):
        fixed = self.env["delivery.carrier"].create({
            "name": "Local Courier",
            "delivery_type": "fixed",
            "product_id": self.carrier.product_id.id,
        })
        order = self._order()
        order.inxpress_selected_carrier_name = "DHL"
        order.set_delivery_line(fixed, 5.0)

        self.assertEqual(self._delivery_line(order).name, "Local Courier")

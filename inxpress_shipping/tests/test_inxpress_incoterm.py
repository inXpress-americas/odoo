from types import SimpleNamespace

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInXpressIncoterm(TransactionCase):
    """Incoterm sent on an international dispatch, and where it comes from."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "InXpress Test",
            "delivery_type": "inxpress",
            "product_id": cls.env["product.product"].create({
                "name": "InXpress Delivery",
                "type": "service",
            }).id,
            "inxpress_base_url": "https://example.invalid",
            "inxpress_api_token": "ixpx_test",
        })
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.env["res.partner"].create({
                "name": "Acme Belgium NV",
                "country_id": cls.env.ref("base.be").id,
            }).id,
        })
        cls.env.company.incoterm_id = False

    def _incoterm(self, order=None):
        # Only the order and the company are read, so a stand-in will do
        picking = SimpleNamespace(
            sale_id=order if order is not None else self.order,
            company_id=self.env.company,
        )
        return self.carrier._inxpress_incoterm(picking)

    def test_duties_paid_by_defaults_to_recipient(self):
        self.assertEqual(self.carrier.inxpress_duties_paid_by, "recipient")

    def test_recipient_sends_dap(self):
        code, source = self._incoterm()
        self.assertEqual(code, "DAP")
        self.assertIn("Duties Paid By", source)

    def test_sender_sends_ddp(self):
        self.carrier.inxpress_duties_paid_by = "sender"
        code, source = self._incoterm()
        self.assertEqual(code, "DDP")
        self.assertIn("Duties Paid By", source)

    def test_company_default_beats_duties_paid_by(self):
        self.carrier.inxpress_duties_paid_by = "sender"
        self.env.company.incoterm_id = self.env.ref("account.incoterm_EXW")
        code, source = self._incoterm()
        self.assertEqual(code, "EXW")
        self.assertEqual(source, "the company default")

    def test_order_incoterm_beats_everything(self):
        self.carrier.inxpress_duties_paid_by = "sender"
        self.env.company.incoterm_id = self.env.ref("account.incoterm_EXW")
        self.order.incoterm = self.env.ref("account.incoterm_DAP")
        code, source = self._incoterm()
        self.assertEqual(code, "DAP")
        self.assertEqual(source, "the sales order")

    def test_picking_without_order_uses_duties_paid_by(self):
        self.carrier.inxpress_duties_paid_by = "sender"
        code, _source = self._incoterm(order=self.env["sale.order"])
        self.assertEqual(code, "DDP")

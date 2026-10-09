# InXpress Odoo connector

An Odoo 18 addon that brings InXpress shipping into Odoo. Quote a sales order
against your own InXpress carrier rates, book the shipment from the delivery
order using the boxes actually packed, and get the label and tracking number
back in Odoo. Parcel and freight (LTL) are both supported.

The module talks to WebShip-X over the public v3 API. It never talks to
carriers or to TPI directly, so when a rate or a dispatch looks wrong, the
question is what WebShip-X returned.

An InXpress account is required.

## Requirements

Odoo 18, and the `delivery`, `sale` and `stock_delivery` addons it depends on.
`stock_delivery` is what provides `hs_code` and `country_of_origin` on the
product, both used to build customs declarations.

## Running it locally

```bash
docker compose up -d          # postgres:15 and odoo:18, app on :8069
```

Python changes need a container restart:

```bash
docker compose restart odoo
```

XML and manifest changes need a module upgrade. Either use the Apps UI, or run
it directly against your database:

```bash
docker compose stop odoo
docker compose run --rm odoo odoo -d YOUR_DB -u inxpress_shipping --stop-after-init
docker compose start odoo
```

The module is bind-mounted into the container, so neither needs a rebuild.

`docker-compose.yml` also mounts `./enterprise` read-only. That directory holds
licensed Odoo Enterprise source and is not committed. A fresh clone starts
without it.

## Configuring a carrier

Create a delivery method under Inventory > Configuration > Delivery Methods and
set its provider to InXpress.

| Field | What it does |
| --- | --- |
| InXpress API URL | Base URL of your InXpress server. Required. |
| API Token | Your `ixpx_` token. Leave empty to use username and password. |
| Username, Password | Used only when the API Token is empty. |
| Carrier Code | Restricts quotes to one carrier. Empty returns every carrier. |
| Service Code | Restricts quotes to one service. |
| Default Package Type | The box whose dimensions every quote sends. |
| Shipping Mode | Parcel, or freight (LTL). |

Freight adds a freight class, a handling unit type, its own package type, and
the residential and liftgate accessorials. It needs a token carrying the
`freight:quote` and `freight:write` scopes.

Rates are used as WebShip-X returns them, with no currency conversion. The
`currency` field on a rate response is always `"USD"` on this path, whatever
the amounts are really in, so Odoo cannot convert. The Odoo company currency
must match the InXpress account currency.

Tracking status refreshes when a user clicks Tracking on the transfer. The
scheduled action "InXpress: Sync Tracking" refreshes it every 4 hours, but it
ships inactive. Activate it under Settings > Technical > Scheduled Actions.

## How a quote gets its dimensions

In order of precedence, from `_inxpress_dimensions_payload`:

1. The Default Package Type on the delivery method, if one is set. In freight
   mode the freight package type is checked first.
2. Otherwise the summed volume of the products on the order, sent as the side
   of the cube that holds it.
3. With neither, no dimensions are sent, the rate comes back on weight alone,
   and the module logs a warning naming the order.

Setting a Default Package Type means product volume is never used for rating.
That is deliberate. A declared box is one that exists, and carriers price the
box they are handed.

Dispatch works differently. It reads the real packed boxes, but `packageDetails`
holds one set of values per shipment, so it sends the number of boxes, the
shipment weight split evenly across them, and the dimensions of the largest box
(`_inxpress_details_dimensions`). A box with no dimensions of its own falls back
to the same helper as the quote. When box weights differ by more than 20% from
the average, `_inxpress_warn_uneven_packages` logs a warning that the heaviest
box may be under-rated. Quote and dispatch describe the same order differently
on purpose, so their prices can differ.

Units are read from the Odoo database rather than assumed. Kilograms go out
with centimetres, pounds with inches.

## Layout

```
inxpress_shipping/
  __manifest__.py          depends: delivery, sale, stock_delivery
  models/
    delivery_carrier.py    nearly all the connector logic
    inxpress_client.py     HTTP client for /api/v3/*
    product_template.py
  wizard/                  the rate-picker wizard
  views/                   delivery.carrier, product, sale.order, stock.picking
  data/                    delivery method record, tracking cron
  security/
  static/description/      Apps listing page, icon and banner
  tests/
```

`delivery_carrier.py` is where nearly every change lands. It inherits
`delivery.carrier`. Every method is prefixed `_inxpress_` except the framework
hooks such as `inxpress_rate_shipment` and `inxpress_send_shipping`, which keep
the names Odoo's delivery framework looks for.

## Tests

```bash
docker compose run --rm odoo odoo -d YOUR_DB -u inxpress_shipping \
  --test-enable --test-tags /inxpress_shipping --stop-after-init
```

`tests/test_inxpress_units.py` covers weight and length conversion, the volume
cube root, and freight accessorials. `tests/test_inxpress_payload.py` covers
the quote payload, including the package-type precedence described above.

## Conventions

New helper methods take the `_inxpress_` prefix, which keeps them separate from
Odoo's own namespace. Never log or echo the API token or the password. Errors
shown to the end user go through `_inxpress_opaque_error`, which hides upstream
carrier detail on purpose. Any payload field with a unit gets converted, see
`_inxpress_weight_value` and `_inxpress_length_value`.

## Support

support@inxpress.com

## License

LGPL-3. See `LICENSE`.

import json
import logging

import requests

_logger = logging.getLogger(__name__)

TIMEOUT = 60  # seconds


class InXpressClient:
    """HTTP client for the InXpress v3 API.

    Supports two auth modes:
    - API token (ixpx_*): pass api_token, leave username/password empty
    - JWT (username/password): pass username + password, leave api_token empty
    """

    def __init__(self, base_url, api_token=None, username=None, password=None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        if api_token:
            self.session.headers["Authorization"] = f"Bearer {api_token}"
        elif username and password:
            self._login(username, password)
        else:
            raise ValueError("Provide either api_token or username+password")

    def _login(self, username, password):
        """POST /api/v3/auth/login to obtain a JWT token."""
        url = f"{self.base_url}/api/v3/auth/login"
        _logger.debug("InXpress login %s", url)
        resp = self.session.post(url, json={
            "username": username,
            "password": password,
        }, timeout=TIMEOUT)
        resp.raise_for_status()

        # JWT is returned in the Authorization header (case-insensitive)
        token = resp.headers.get("Authorization") or resp.headers.get("authorization", "")
        if not token:
            # Some proxies strip the header, so try the response body
            try:
                body = resp.json()
                token = body.get("token") or body.get("accessToken", "")
            except Exception:
                pass
        if not token:
            _logger.error("InXpress login %s returned %s but no JWT token", url, resp.status_code)
            _logger.debug("Login response headers: %s", dict(resp.headers))
            _logger.debug("Login response body: %s", resp.text[:500])
            raise ValueError("Login succeeded but no JWT token found in response")
        # Ensure Bearer prefix
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        self.session.headers["Authorization"] = token

    # ------------------------------------------------------------------
    # Carriers
    # ------------------------------------------------------------------

    def get_carriers(self):
        """GET /api/v3/carriers - list all carriers with capabilities."""
        return self._get("/api/v3/carriers")

    def get_services(self):
        """GET /api/v3/services - list shipper's enabled services."""
        return self._get("/api/v3/services")

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------

    def get_quote(self, payload):
        """POST /api/v3/integrations/quote - get live rates."""
        url = f"{self.base_url}/api/v3/integrations/rates"
        _logger.info("InXpress QUOTE POST %s", url)
        _logger.debug("InXpress QUOTE payload=%s", json.dumps(payload, indent=2, default=str)[:3000])
        resp = self.session.post(url, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            _logger.error("InXpress QUOTE %s returned %s: %s", url, resp.status_code, resp.reason)
            _logger.debug("InXpress QUOTE error body: %s", resp.text[:2000])
        resp.raise_for_status()
        result = resp.json()
        _logger.debug("InXpress QUOTE response: %s", json.dumps(result, indent=2)[:5000])
        return result

    # ------------------------------------------------------------------
    # Draft shipment (save before dispatch)
    # ------------------------------------------------------------------

    def save_draft_shipment(self, payload):
        """POST /api/v3/draft-shipments - save a draft shipment.

        Returns the draft with an ID (pendingShipmentId) needed for dispatch.
        """
        url = f"{self.base_url}/api/v3/draft-shipments"
        _logger.info("InXpress SAVE DRAFT %s", url)
        _logger.debug("InXpress SAVE DRAFT payload=%s", json.dumps(payload, indent=2, default=str)[:5000])
        resp = self.session.post(url, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            _logger.error("InXpress SAVE DRAFT %s returned %s: %s", url, resp.status_code, resp.reason)
            _logger.debug("InXpress SAVE DRAFT error body: %s", resp.text[:2000])
        resp.raise_for_status()
        _logger.debug("InXpress SAVE DRAFT response: %s", resp.text[:2000])
        return resp.json()

    # ------------------------------------------------------------------
    # Commercial Invoice
    # ------------------------------------------------------------------

    def create_commercial_invoice(self, draft_id, invoice_data):
        """POST /api/v3/commercial-invoice - create a commercial invoice for a draft."""
        url = f"{self.base_url}/api/v3/commercial-invoice"
        payload = {**invoice_data, "draftId": draft_id}
        _logger.info("InXpress CREATE COMMERCIAL INVOICE for draft %s", draft_id)
        resp = self.session.post(url, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            _logger.error("InXpress COMMERCIAL INVOICE %s returned %s: %s", url, resp.status_code, resp.reason)
            _logger.debug("InXpress COMMERCIAL INVOICE error body: %s", resp.text[:2000])
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Dispatch (booking)
    # ------------------------------------------------------------------

    def dispatch_shipment(self, payload):
        """POST /api/v3/integrations/shipments - book a shipment and get label."""
        url = f"{self.base_url}/api/v3/integrations/shipments"
        _logger.info("InXpress DISPATCH %s", url)
        _logger.debug("InXpress DISPATCH payload=%s", json.dumps(payload, indent=2, default=str)[:5000])
        resp = self.session.post(url, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            _logger.error("InXpress DISPATCH %s returned %s: %s", url, resp.status_code, resp.reason)
            _logger.debug("InXpress DISPATCH error body: %s", resp.text[:2000])
        else:
            _logger.debug("InXpress DISPATCH response: %s", resp.text[:2000])
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    def get_tracking(self, shipment_id):
        """GET /api/v3/integrations/shipments/{shipment_id}/tracking

        Returns a TrackingDto: proNumber, statusEnumName, trackingEvents,
        trackingLink. The id is the numeric InXpress shipment id from the
        dispatch response, not the carrier tracking number.
        """
        return self._get(
            f"/api/v3/integrations/shipments/{shipment_id}/tracking"
        )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def void_shipment(self, shipment_id, reason=None):
        """POST /api/v3/integrations/shipments/{shipment_id}/void

        The documented way to cancel a booking. CommerceVoidRequest carries
        only a reason, so nothing else is sent. Returns the void result:
        shipmentId, status, voidDate, carrierVoided.
        """
        return self._post(
            f"/api/v3/integrations/shipments/{shipment_id}/void",
            {"reason": reason or "Cancelled from Odoo"},
        )

    # ------------------------------------------------------------------
    # Documents / Labels
    # ------------------------------------------------------------------

    def get_label(self, shipment_id):
        """GET /api/v3/document/label/{shipment_id} - returns PDF bytes."""
        return self._get_binary(f"/api/v3/document/label/{shipment_id}")

    def get_thermal_label(self, shipment_id):
        """GET /api/v3/document/thermal-label/{shipment_id}"""
        return self._get_binary(f"/api/v3/document/thermal-label/{shipment_id}")

    def get_label_url(self, shipment_id):
        """GET /api/v3/integrations/shipments/{shipment_id}/label

        A fresh presigned URL for a label already bought. The one in the
        dispatch response is short-lived - an hour by default - and this
        re-presigns the stored PDF rather than re-rendering or re-dispatching
        it. Returns CommerceLabelRetrievalResponse: labelUrl, expiresAt.
        """
        return self._get(
            f"/api/v3/integrations/shipments/{shipment_id}/label"
        )

    def download_label_url(self, url):
        """GET a presigned label URL from a dispatch response - returns PDF bytes.

        Sent without the session headers: the URL carries its own signature and
        S3 rejects requests that also pass an Authorization header.
        """
        _logger.info("InXpress GET label url %s", url.split("?")[0])
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path):
        url = f"{self.base_url}{path}"
        _logger.info("InXpress GET %s", url)
        resp = self.session.get(url, timeout=TIMEOUT)
        _logger.info("InXpress GET %s status=%s", url, resp.status_code)
        _logger.debug("InXpress GET %s content-type=%s body=%s",
                      url, resp.headers.get("Content-Type", "?"), resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload):
        url = f"{self.base_url}{path}"
        _logger.info("InXpress POST %s", url)
        _logger.debug("InXpress POST %s payload=%s", url, payload)
        resp = self.session.post(url, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            _logger.error("InXpress POST %s returned %s: %s", url, resp.status_code, resp.reason)
            _logger.debug("InXpress POST %s error body: %s", url, resp.text[:1000])
        resp.raise_for_status()
        return resp.json()

    def _put(self, path, payload):
        url = f"{self.base_url}{path}"
        _logger.debug("InXpress PUT %s", url)
        resp = self.session.put(url, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _get_binary(self, path):
        url = f"{self.base_url}{path}"
        _logger.debug("InXpress GET (binary) %s", url)
        resp = self.session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.content

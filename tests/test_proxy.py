import datetime
import unittest
from unittest.mock import Mock

from server_deploy.core import Blocked
from server_deploy.proxy import Proxy


class ProxyTests(unittest.TestCase):
    def test_certificates_require_valid_expiry_and_one_label_wildcard(self):
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=20)
        ).isoformat()
        cert = {"expires_on": future, "domain_names": ["*.example.com"]}
        self.assertTrue(Proxy.covers(cert, "app.example.com"))
        self.assertFalse(Proxy.covers(cert, "nested.app.example.com"))
        cert["expires_on"] = "2000-01-01T00:00:00Z"
        self.assertFalse(Proxy.covers(cert, "app.example.com"))

    def test_conflicting_redirect_domain_blocks_adoption(self):
        proxy = Proxy.__new__(Proxy)
        proxy.request = Mock(side_effect=[[], [{"domain_names": ["app.example.com"]}]])
        with self.assertRaises(Blocked):
            proxy.observe([{"domain": "app.example.com"}])

    def test_multidomain_route_is_never_silently_reassigned(self):
        proxy = Proxy.__new__(Proxy)
        proxy.request = Mock(
            side_effect=[
                [{"id": 1, "domain_names": ["app.example.com", "other.example.com"]}],
                [],
                [],
            ]
        )
        with self.assertRaises(Blocked):
            proxy.observe([{"domain": "app.example.com"}])

    def test_recovery_rejects_changed_proxy_ownership(self):
        proxy = Proxy.__new__(Proxy)
        proxy.observe = Mock(return_value={"a": {"id": 2}})
        with self.assertRaises(Blocked):
            proxy.restore([{"domain": "a"}], {"a": {"id": 1}})

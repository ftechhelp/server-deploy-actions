import datetime
import json
import urllib.error
import urllib.request

from .core import Blocked, protected


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Blocked("Proxy API redirects are forbidden")


class Proxy:
    def __init__(self, base, login_file):
        if base not in ("http://127.0.0.1:81/api", "http://localhost:81/api"):
            raise Blocked("NPM API must use the local loopback endpoint")
        self.base = base
        self.token = ""
        values = {}
        for line in protected(login_file).read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or key not in ("NPM_IDENTITY", "NPM_SECRET") or key in values:
                raise Blocked("Invalid NPM credential file")
            values[key] = value.strip().strip("\"'")
        if not values.get("NPM_IDENTITY") or not values.get("NPM_SECRET"):
            raise Blocked("NPM credential file is incomplete")
        self.token = self.request(
            "POST",
            "/tokens",
            {"identity": values["NPM_IDENTITY"], "secret": values["NPM_SECRET"]},
        ).get("token")
        if not self.token:
            raise Blocked("NPM login did not return a token")

    def request(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(
                request, timeout=180
            ) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise Blocked("NPM response exceeds size limit")
                return json.loads(data) if data else None
        except (urllib.error.URLError, ValueError) as error:
            raise Blocked(f"NPM {method} {path} failed") from error

    @staticmethod
    def covers(cert, domain):
        try:
            expiry = datetime.datetime.fromisoformat(
                cert["expires_on"].replace("Z", "+00:00")
            )
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            if expiry <= datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(days=1):
                return False
        except (KeyError, ValueError):
            return False
        for pattern in cert.get("domain_names", []):
            if pattern.lower() == domain.lower():
                return True
            if (
                pattern.startswith("*.")
                and pattern[2:].lower() == domain.partition(".")[2].lower()
            ):
                return True
        return False

    def observe(self, routes):
        hosts = self.request("GET", "/nginx/proxy-hosts")
        for kind in ("redirection-hosts", "dead-hosts"):
            for host in self.request("GET", f"/nginx/{kind}"):
                if set(host.get("domain_names", [])) & {r["domain"] for r in routes}:
                    raise Blocked("Proxy domain is owned by a redirect or 404 host")
        result = {}
        for route in routes:
            matching = [
                h for h in hosts if route["domain"] in h.get("domain_names", [])
            ]
            if len(matching) > 1 or (
                matching and len(matching[0]["domain_names"]) != 1
            ):
                raise Blocked("Proxy domain ownership is ambiguous")
            # Full prior configuration is protected on disk, never logged or uploaded.
            result[route["domain"]] = matching[0] if matching else None
        return result

    def ensure(self, routes):
        before = self.observe(routes)
        certs = self.request("GET", "/nginx/certificates")
        for route in routes:
            host = before[route["domain"]]
            valid = [c for c in certs if self.covers(c, route["domain"])]
            if not valid:
                self.request(
                    "POST",
                    "/nginx/certificates",
                    {
                        "provider": "letsencrypt",
                        "domain_names": [route["domain"]],
                        "meta": {"dns_challenge": False},
                    },
                )
                certs = self.request("GET", "/nginx/certificates")
                valid = [c for c in certs if self.covers(c, route["domain"])]
            if not valid:
                raise Blocked("Proxy has no valid TLS certificate")
            cert = next(
                (c for c in valid if host and c["id"] == host.get("certificate_id")),
                valid[0],
            )
            want = {
                "domain_names": [route["domain"]],
                "forward_scheme": "http",
                "forward_host": route["container"],
                "forward_port": route["port"],
                "certificate_id": cert["id"],
                "ssl_forced": True,
                "allow_websocket_upgrade": True,
                "enabled": True,
            }
            if host and all(host.get(k) == v for k, v in want.items()):
                continue
            if host:
                self.request("PUT", f"/nginx/proxy-hosts/{host['id']}", want)
            else:
                self.request(
                    "POST", "/nginx/proxy-hosts", {**want, "block_exploits": True}
                )
        after = self.observe(routes)
        for route in routes:
            host = after[route["domain"]]
            if (
                not host
                or host["forward_host"] != route["container"]
                or host["forward_port"] != route["port"]
                or not host.get("ssl_forced")
                or not host.get("allow_websocket_upgrade")
            ):
                raise Blocked("Proxy routing verification failed")

    def restore(self, routes, prior):
        current = self.observe(routes)
        fields = (
            "domain_names",
            "forward_scheme",
            "forward_host",
            "forward_port",
            "certificate_id",
            "ssl_forced",
            "allow_websocket_upgrade",
            "enabled",
            "access_list_id",
            "advanced_config",
            "block_exploits",
            "caching_enabled",
            "http2_support",
            "hsts_enabled",
            "hsts_subdomains",
            "locations",
            "meta",
        )
        for domain, old in prior.items():
            now = current[domain]
            if old:
                if not now or now["id"] != old["id"]:
                    raise Blocked("Proxy ownership changed before recovery")
                self.request(
                    "PUT",
                    f"/nginx/proxy-hosts/{old['id']}",
                    {k: old[k] for k in fields if k in old},
                )
            elif now:
                route = next(r for r in routes if r["domain"] == domain)
                if now["forward_host"] != route["container"]:
                    raise Blocked("New proxy host changed ownership before recovery")
                self.request("DELETE", f"/nginx/proxy-hosts/{now['id']}")
        restored = self.observe(routes)
        for domain, old in prior.items():
            actual = restored[domain]
            if old is None and actual is not None:
                raise Blocked("Proxy removal verification failed")
            if old and any(old.get(k) != actual.get(k) for k in fields if k != "meta"):
                raise Blocked("Proxy restoration verification failed")

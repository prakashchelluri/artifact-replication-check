"""Read tag inventories from a Docker Registry v2 API or a generic JSON listing.

Both sources expose the same two methods:
    tags(repo)        -> list of tags, or None if the repository does not exist
    digest(repo, tag) -> manifest digest, or None if unknown / not present
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Site

# Ask for every manifest type, otherwise some registries convert the manifest
# on the fly and return a different digest than the one that was pushed.
MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
_AUTH_PARAM = re.compile(r'(\w+)="([^"]*)"')


class CheckError(Exception):
    """A site could not be queried: network error, timeout, auth failure, bad data."""


def _basic(site: Site) -> str | None:
    if site.username is None:
        return None
    raw = f"{site.username}:{site.password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def http(site: Site, method: str, url: str, headers: dict | None = None):
    """One HTTP request with the site's timeout. Returns (headers, body).

    Non-2xx responses raise urllib.error.HTTPError so callers can react to
    401/404; everything else (DNS, refused, timeout) becomes CheckError.
    """
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=site.timeout) as resp:
            return resp.headers, resp.read()
    except urllib.error.HTTPError:
        raise
    except TimeoutError as exc:
        raise CheckError(f"{site.name}: timed out after {site.timeout:g}s: {method} {url}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise CheckError(
                f"{site.name}: timed out after {site.timeout:g}s: {method} {url}") from exc
        raise CheckError(f"{site.name}: {method} {url}: {exc.reason}") from exc
    except OSError as exc:  # connection reset, remote closed, ...
        raise CheckError(f"{site.name}: {method} {url}: {exc}") from exc


def _http_error(site: Site, method: str, url: str, exc: urllib.error.HTTPError) -> CheckError:
    return CheckError(f"{site.name}: {method} {url}: HTTP {exc.code} {exc.reason}")


class RegistryClient:
    """Docker Registry HTTP API v2 (Docker Distribution, Harbor, Artifactory, Nexus, ...)."""

    def __init__(self, site: Site):
        self.site = site
        self._tokens: dict[str, str] = {}  # repo -> bearer token from the auth challenge

    def _auth(self, repo: str) -> str | None:
        if repo in self._tokens:
            return "Bearer " + self._tokens[repo]
        if self.site.token:
            return "Bearer " + self.site.token
        return _basic(self.site)

    def _fetch_token(self, challenge: str) -> str:
        params = dict(_AUTH_PARAM.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            raise CheckError(f"{self.site.name}: bearer challenge without realm: {challenge}")
        url = realm + "?" + urllib.parse.urlencode(params)
        basic = _basic(self.site)
        try:
            _, body = http(self.site, "GET", url, {"Authorization": basic} if basic else {})
        except urllib.error.HTTPError as exc:
            raise _http_error(self.site, "GET", url, exc) from exc
        try:
            data = json.loads(body)
            return data.get("token") or data["access_token"]
        except (ValueError, KeyError) as exc:
            raise CheckError(f"{self.site.name}: token endpoint returned no token") from exc

    def _request(self, method: str, url: str, repo: str, headers: dict | None = None):
        headers = dict(headers or {})
        auth = self._auth(repo)
        if auth:
            headers["Authorization"] = auth
        try:
            return http(self.site, method, url, headers)
        except urllib.error.HTTPError as exc:
            challenge = exc.headers.get("WWW-Authenticate", "")
            if exc.code != 401 or not challenge.startswith("Bearer") or repo in self._tokens:
                raise
            # Token auth (Docker Hub style): get a token for this repo and retry once.
            self._tokens[repo] = self._fetch_token(challenge)
            headers["Authorization"] = "Bearer " + self._tokens[repo]
            return http(self.site, method, url, headers)

    def tags(self, repo: str) -> list[str] | None:
        url: str | None = f"{self.site.url}/v2/{repo}/tags/list"
        tags: list[str] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                raise CheckError(f"{self.site.name}: pagination loop at {url}")
            seen.add(url)
            try:
                headers, body = self._request("GET", url, repo)
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and not tags:
                    return None
                raise _http_error(self.site, "GET", url, exc) from exc
            try:
                tags.extend(json.loads(body).get("tags") or [])
            except (ValueError, AttributeError) as exc:
                raise CheckError(f"{self.site.name}: invalid JSON from {url}") from exc
            match = _LINK_NEXT.search(headers.get("Link", ""))
            url = urllib.parse.urljoin(url, match.group(1)) if match else None
        return tags

    def digest(self, repo: str, tag: str) -> str | None:
        url = f"{self.site.url}/v2/{repo}/manifests/{tag}"
        try:
            headers, _ = self._request("HEAD", url, repo, {"Accept": MANIFEST_ACCEPT})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # tag listed but manifest missing: a broken replica
            raise _http_error(self.site, "HEAD", url, exc) from exc
        digest = headers.get("Docker-Content-Digest")
        if not digest:
            raise CheckError(f"{self.site.name}: no Docker-Content-Digest header from {url}")
        return digest


class GenericSource:
    """A JSON listing from an HTTP endpoint or a local file.

    Accepted shape (tags as a list, or as a tag -> digest mapping):
        {"repositories": {"libs/app": ["1.0", "1.1"],
                          "team/api": {"v1": "sha256:..."}}}
    """

    def __init__(self, site: Site):
        self.site = site
        self._repos: dict[str, dict[str, str | None]] | None = None

    def _load(self) -> dict[str, dict[str, str | None]]:
        if self._repos is not None:
            return self._repos
        loc = self.site.url
        if loc.startswith(("http://", "https://")):
            auth = ("Bearer " + self.site.token) if self.site.token else _basic(self.site)
            try:
                _, body = http(self.site, "GET", loc, {"Authorization": auth} if auth else {})
            except urllib.error.HTTPError as exc:
                raise _http_error(self.site, "GET", loc, exc) from exc
        else:
            try:
                body = Path(loc).read_bytes()
            except OSError as exc:
                raise CheckError(f"{self.site.name}: cannot read {loc}: {exc}") from exc
        try:
            raw = json.loads(body)["repositories"]
            self._repos = {
                name: dict(tags) if isinstance(tags, dict) else dict.fromkeys(tags)
                for name, tags in raw.items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CheckError(
                f"{self.site.name}: {loc} is not a {{\"repositories\": {{...}}}} listing") from exc
        return self._repos

    def tags(self, repo: str) -> list[str] | None:
        found = self._load().get(repo)
        return None if found is None else list(found)

    def digest(self, repo: str, tag: str) -> str | None:
        return self._load().get(repo, {}).get(tag)


def make_source(site: Site) -> RegistryClient | GenericSource:
    return GenericSource(site) if site.kind == "generic" else RegistryClient(site)

import functools
import json
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from conftest import tags

from artifact_replication_check import compare
from artifact_replication_check.cli import main
from artifact_replication_check.config import ConfigError, DnsSpec, load_config


def write_config(tmp_path, source, target, repos, **extra):
    cfg = {"source": source, "target": target, "repositories": repos, **extra}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def run(capsys, *argv):
    code = main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_in_sync(registry, tmp_path, capsys):
    src = registry({"team/app": tags(5)})
    tgt = registry({"team/app": tags(5)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    code, report = run(capsys, "-c", cfg, "--digests")
    assert code == 0
    assert report["repositories"][0]["status"] == "OK"
    assert report["result"] == "IN SYNC"


def test_pagination_follows_link_header(registry, tmp_path, capsys):
    src = registry({"team/app": tags(10)}, page_size=3)
    tgt = registry({"team/app": tags(10)}, page_size=4)
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    code, report = run(capsys, "-c", cfg)
    assert code == 0
    assert report["repositories"][0]["source_tags"] == 10
    assert report["repositories"][0]["target_tags"] == 10
    assert len([r for r in src.requests if "tags/list" in r]) == 4  # 3+3+3+1
    assert len([r for r in tgt.requests if "tags/list" in r]) == 3  # 4+4+2


def test_missing_tags_and_missing_repo(registry, tmp_path, capsys):
    # The incident: target only had up to v5 while source had up to v10.
    src = registry({"team/app": tags(10), "team/api": tags(2)})
    tgt = registry({"team/app": tags(5)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app", "team/api"])
    code, report = run(capsys, "-c", cfg)
    assert code == 1
    app, api = report["repositories"]
    assert app["missing"] == ["v6", "v7", "v8", "v9", "v10"]  # natural order
    assert app["status"] == "DRIFT"
    assert api["missing_repo"] is True and api["status"] == "MISSING"


def test_extra_tags_fail_only_on_any(registry, tmp_path, capsys):
    src = registry({"team/app": tags(3)})
    tgt = registry({"team/app": tags(4)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    code, report = run(capsys, "-c", cfg)
    assert code == 1 and report["repositories"][0]["extra"] == ["v4"]
    assert run(capsys, "-c", cfg, "--fail-on", "missing")[0] == 0


def test_digest_mismatch(registry, tmp_path, capsys):
    stale = tags(3)
    stale["v2"] = "sha256:" + "f" * 64  # same tag, different content: stale replica
    del stale["v3"]
    src = registry({"team/app": tags(3)})
    tgt = registry({"team/app": {**stale, "v3": None}})  # v3 listed but manifest missing
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"],
                       compare_digests=True)
    code, report = run(capsys, "-c", cfg)
    assert code == 1
    mismatches = {m["tag"]: m for m in report["repositories"][0]["digest_mismatch"]}
    assert set(mismatches) == {"v2", "v3"}
    assert mismatches["v3"]["target"] is None
    assert run(capsys, "-c", cfg, "--fail-on", "digest")[0] == 1
    assert run(capsys, "-c", cfg, "--fail-on", "missing")[0] == 0


def test_hanging_site_times_out(registry, tmp_path, capsys):
    src = registry({"team/app": tags(2)})
    tgt = registry({"team/app": tags(2)}, delay=3)
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    started = time.monotonic()
    code, report = run(capsys, "-c", cfg, "--timeout", "0.5")
    assert time.monotonic() - started < 2.5
    assert code == 2
    assert "timed out after 0.5s" in report["repositories"][0]["error"]


def test_connection_refused_is_an_error(registry, tmp_path, capsys):
    src = registry({"team/app": tags(2)})
    with socket.socket() as s:  # grab a free port, then close it so nothing listens
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = write_config(tmp_path, {"url": src.url}, {"url": f"http://127.0.0.1:{port}"},
                       ["team/app"])
    code, report = run(capsys, "-c", cfg, "--timeout", "2")
    assert code == 2 and report["repositories"][0]["status"] == "ERROR"


def test_bearer_token_challenge(registry, tmp_path, capsys):
    src = registry({"team/app": tags(2)}, token="s3cr3t")
    tgt = registry({"team/app": tags(2)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    code, _ = run(capsys, "-c", cfg, "--digests")
    assert code == 0
    assert any(r.startswith("GET /token?") for r in src.requests)


def test_generic_mode_against_local_files(tmp_path, capsys):
    (tmp_path / "src.json").write_text(json.dumps(
        {"repositories": {"libs/app": ["1.0", "1.1", "1.2"], "libs/lib": {"2.0": "sha256:a"}}}))
    (tmp_path / "tgt.json").write_text(json.dumps(
        {"repositories": {"libs/app": ["1.0"], "libs/lib": {"2.0": "sha256:b"}}}))
    cfg = write_config(tmp_path, {"type": "generic", "url": "src.json"},
                       {"type": "generic", "url": "tgt.json"}, ["libs/app", "libs/lib"],
                       compare_digests=True)
    code, report = run(capsys, "-c", cfg)
    assert code == 1
    app, lib = report["repositories"]
    assert app["missing"] == ["1.1", "1.2"]
    assert lib["digest_mismatch"] == [{"tag": "2.0", "source": "sha256:a", "target": "sha256:b"}]


def test_generic_mode_over_http(tmp_path, capsys):
    (tmp_path / "listing.json").write_text(json.dumps({"repositories": {"x": ["1", "2"]}}))
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        cfg = write_config(tmp_path, {"type": "generic", "url": f"{base}/listing.json"},
                           {"type": "generic", "url": "listing.json"}, ["x"])
        assert run(capsys, "-c", cfg)[0] == 0
        cfg = write_config(tmp_path, {"type": "generic", "url": f"{base}/nope.json"},
                           {"type": "generic", "url": "listing.json"}, ["x"])
        code, report = run(capsys, "-c", cfg)
        assert code == 2 and "HTTP 404" in report["repositories"][0]["error"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("answer,phase,expected", [
    (("cdn.example.com", [], ["203.0.113.20"]), "post", 0),
    (("cdn.example.com", [], ["198.51.100.10"]), "post", 1),
    (("cdn.example.com", [], ["198.51.100.10"]), "pre", 0),  # informational before cut-over
])
def test_dns_check(monkeypatch, registry, tmp_path, capsys, answer, phase, expected):
    monkeypatch.setattr(compare.socket, "gethostbyname_ex", lambda name: answer)
    src = registry({"team/app": tags(1)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": src.url}, ["team/app"],
                       dns={"name": "artifacts.example.com",
                            "expect_addresses": ["203.0.113.20"]})
    code, report = run(capsys, "-c", cfg, "--phase", phase)
    assert code == expected
    assert report["dns"]["addresses"] == answer[2]


def test_dns_cname_and_failure(monkeypatch):
    spec = DnsSpec("artifacts.example.com", [], "artifacts-west.example.com.")
    monkeypatch.setattr(compare.socket, "gethostbyname_ex",
                        lambda n: ("ARTIFACTS-WEST.example.com", [n], ["203.0.113.20"]))
    assert compare.check_dns(spec, 1).matches is True

    def boom(name):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(compare.socket, "gethostbyname_ex", boom)
    assert "failed" in compare.check_dns(spec, 1).error


def test_config_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("REG_TOKEN", raising=False)
    source = {"url": "https://a.example.com", "auth": {"token_env": "REG_TOKEN"}}
    cfg = write_config(tmp_path, source, {"url": "https://b.example.com"}, ["x"])
    with pytest.raises(ConfigError, match="REG_TOKEN"):
        load_config(cfg)
    assert main(["-c", cfg]) == 2
    assert main(["-c", str(tmp_path / "missing.yaml")]) == 2


def test_human_report(registry, tmp_path, capsys):
    src = registry({"team/app": tags(10)})
    tgt = registry({"team/app": tags(5)})
    cfg = write_config(tmp_path, {"url": src.url}, {"url": tgt.url}, ["team/app"])
    assert main(["-c", cfg]) == 1
    out = capsys.readouterr().out
    assert "missing on target: v6, v7, v8, v9, v10" in out
    assert "Result: DRIFT FOUND (exit 1)" in out

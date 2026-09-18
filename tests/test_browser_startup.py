from unittest.mock import Mock

import pytest

from jev_ultrafast import browser


@pytest.mark.parametrize("detail", ["fatal: DevToolsActivePort not found in profiles\n", "other failure\n", None])
def test_startup_diagnosis(monkeypatch, tmp_path, detail):
    log = tmp_path / "daemon.log"
    if detail is not None:
        log.write_text(detail)
    error = RuntimeError("daemon default didn't come up")
    monkeypatch.setattr(browser, "ensure_daemon", Mock(side_effect=error))
    monkeypatch.setattr(browser.harness_ipc, "log_path", lambda name: log)
    cdp = Mock()
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError) as raised:
        browser.Browser("https://example.com")
    if detail and "DevToolsActivePort" in detail:
        assert "brave://inspect/#remote-debugging" in str(raised.value)
        assert "approve the connection" in str(raised.value)
    else:
        assert raised.value is error
    cdp.assert_not_called()

"""SSH target validation prevents workflow inputs from becoming command options."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dispatch-deploy.py"
_SPEC = importlib.util.spec_from_file_location("dispatch_deploy", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
dispatch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dispatch)


def test_valid_ssh_target():
    assert dispatch.deployment_target({
        "DEPLOY_HOST": "expense.example.com", "DEPLOY_USER": "expense-deploy",
    }) == ("expense.example.com", "expense-deploy", "22", "/opt/dingtalk-expense")


@pytest.mark.parametrize(("key", "value"), [
    ("DEPLOY_HOST", "-oProxyCommand=evil"),
    ("DEPLOY_HOST", "host;touch /tmp/injected"),
    ("DEPLOY_USER", "user@host"),
    ("DEPLOY_USER", "root;echo injected"),
    ("DEPLOY_PORT", "22 -oStrictHostKeyChecking=no"),
    ("DEPLOY_PORT", "0"),
    ("DEPLOY_PORT", "65536"),
    ("DEPLOY_DIR", "/"),
    ("DEPLOY_DIR", "/opt"),
    ("DEPLOY_DIR", "/opt/app/../../etc"),
    ("DEPLOY_DIR", "/opt/$(id)"),
])
def test_rejects_unsafe_ssh_target(key, value):
    with pytest.raises(ValueError):
        dispatch.deployment_target({
            "DEPLOY_HOST": "expense.example.com", "DEPLOY_USER": "expense", key: value,
        })


def test_release_images_belong_to_repository():
    environ = {
        "GITHUB_REPOSITORY": "Example/expense",
        "BACKEND_IMAGE": "ghcr.io/example/dingtalk-expense-backend@sha256:" + "a" * 64,
        "WEB_IMAGE": "ghcr.io/example/dingtalk-expense-web@sha256:" + "b" * 64,
    }
    assert dispatch.release_source(environ) == "https://github.com/Example/expense"
    with pytest.raises(ValueError):
        dispatch.release_source(environ | {
            "WEB_IMAGE": "ghcr.io/another/dingtalk-expense-web@sha256:" + "b" * 64,
        })

"""Tests for launchd deployment helpers.

The tests use fake subprocess runners; they never call the real launchctl,
plutil, pip, or venv commands.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
from pathlib import Path

import pytest

from orch_hub import launchd, resources
from orch_hub.config import (
    BARK_URL_ENV,
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_PATH_ENV,
    PORT_ENV,
    TOKEN_ENV,
)
from orch_hub.launchd import (
    BOOTSTRAP_MAX_ATTEMPTS,
    BOOTSTRAP_RETRY_INTERVAL,
    LABEL,
    DeployOptions,
    LaunchdError,
    PLIST_NAME,
    TEMPLATE_RELATIVE_PATH,
    UNLOAD_POLL_INTERVAL,
    _is_transient_bootstrap_failure,
    _resolve_leftover_placeholders,
    deploy_service,
    installed_bark_url,
    resolve_deployment,
    restart_service,
    service_domain,
    service_target,
    start_service,
    status_service,
    stop_service,
)
from orch_hub.subprocess_utils import CommandResult


class FakeRunner:
    def __init__(self, *, bootout_not_loaded: bool = True, print_loaded: bool = True):
        self.calls: list[tuple[str, ...]] = []
        self.bootout_not_loaded = bootout_not_loaded
        self.print_loaded = print_loaded

    def __call__(self, args, **kwargs) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        self.calls.append(argv)
        if argv[:2] == ("launchctl", "bootout") and self.bootout_not_loaded:
            return CommandResult(argv, 113, "", "No such process")
        if argv[:2] == ("launchctl", "print") and not self.print_loaded:
            return CommandResult(argv, 113, "", "Could not find service")
        return CommandResult(argv, 0, "ok", "")


class ScriptedLaunchctl:
    """Fake runner with per-verb scripted results and a recording sleeper.

    Each launchctl verb (``bootout`` / ``print`` / ``bootstrap``) draws from
    its own queue of ``(returncode, stdout, stderr)`` tuples; once a queue is
    down to its last entry that entry repeats, so a one-element queue models a
    persistent outcome. Any other command (plutil, pip, venv) returns ok.
    """

    def __init__(self, *, bootout=None, prints=None, bootstrap=None):
        self.calls: list[tuple[str, ...]] = []
        self.sleeps: list[float] = []
        self._queues = {
            "bootout": list(bootout or [(0, "ok", "")]),
            "print": list(prints or [(113, "", "Could not find service")]),
            "bootstrap": list(bootstrap or [(0, "ok", "")]),
        }

    def __call__(self, args, **kwargs) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        self.calls.append(argv)
        if argv[0] == "launchctl":
            queue = self._queues.get(argv[1])
            if queue is not None:
                rc, out, err = queue.pop(0) if len(queue) > 1 else queue[0]
                return CommandResult(argv, rc, out, err)
        return CommandResult(argv, 0, "ok", "")

    def sleeper(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def calls_of(self, verb: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:2] == ("launchctl", verb)]


def make_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "orch-hub"
    (checkout / "orch_hub").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\nname = \"orch-hub\"\n", encoding="utf-8"
    )
    template_src = Path(__file__).parents[1] / TEMPLATE_RELATIVE_PATH
    template_dst = checkout / TEMPLATE_RELATIVE_PATH
    template_dst.parent.mkdir(parents=True)
    shutil.copyfile(template_src, template_dst)
    return checkout


def read_plist(path: Path) -> dict:
    with path.open("rb") as fh:
        return plistlib.load(fh)


def test_deploy_writes_plist_and_bootstraps(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / "com.qinglin89.orch-hub.plist"
    log_dir = tmp_path / "logs"
    mandrel = tmp_path / "mandrel"
    data_dir = tmp_path / "data"
    runner = FakeRunner()

    result = deploy_service(
        DeployOptions(
            checkout=checkout,
            mandrel_path=mandrel,
            host="127.0.0.1",
            port=9876,
            data_dir=data_dir,
            plist_path=plist_path,
            log_dir=log_dir,
            install_package=False,
        ),
        runner=runner,
        environ={},
        token_generator=lambda: "generated-token",
    )

    assert result.generated_token is True
    assert plist_path.stat().st_mode & 0o777 == 0o600
    plist = read_plist(plist_path)
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [str(checkout / ".venv/bin/orch-hub-api")]
    assert plist["EnvironmentVariables"][TOKEN_ENV] == "generated-token"
    assert plist["EnvironmentVariables"][HOST_ENV] == "127.0.0.1"
    assert plist["EnvironmentVariables"][PORT_ENV] == "9876"
    assert plist["EnvironmentVariables"][MANDREL_PATH_ENV] == str(mandrel)
    assert plist["EnvironmentVariables"][DATA_DIR_ENV] == str(data_dir)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == str(log_dir / "orch-hub.log")
    assert plist["StandardErrorPath"] == str(log_dir / "orch-hub.err.log")
    assert "REPLACE-" not in plist_path.read_text(encoding="utf-8")
    assert runner.calls == [
        ("plutil", "-lint", str(plist_path)),
        ("launchctl", "bootout", service_target()),
        ("launchctl", "bootstrap", service_domain(os.getuid()), str(plist_path)),
    ]


@pytest.mark.parametrize(
    ("explicit_name", "environ_names", "installed_names", "expected_name"),
    [
        (
            "flag-mandrel",
            {
                MANDREL_PATH_ENV: "current-env-mandrel",
                LEGACY_MANDREL_PATH_ENV: "legacy-env-mandrel",
            },
            {MANDREL_PATH_ENV: "installed-mandrel"},
            "flag-mandrel",
        ),
        (
            None,
            {
                MANDREL_PATH_ENV: "current-env-mandrel",
                LEGACY_MANDREL_PATH_ENV: "legacy-env-mandrel",
            },
            {MANDREL_PATH_ENV: "installed-mandrel"},
            "current-env-mandrel",
        ),
        (
            None,
            {
                MANDREL_PATH_ENV: "  ",
                LEGACY_MANDREL_PATH_ENV: "legacy-env-mandrel",
            },
            {MANDREL_PATH_ENV: "installed-mandrel"},
            "legacy-env-mandrel",
        ),
        (
            None,
            {},
            {
                MANDREL_PATH_ENV: "installed-mandrel",
                LEGACY_MANDREL_PATH_ENV: "installed-legacy-mandrel",
            },
            "installed-mandrel",
        ),
        (
            None,
            {},
            {LEGACY_MANDREL_PATH_ENV: "installed-legacy-mandrel"},
            "installed-legacy-mandrel",
        ),
    ],
)
def test_deploy_mandrel_path_precedence(
    tmp_path, explicit_name, environ_names, installed_names, expected_name
):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    installed = {
        key: str(tmp_path / value) for key, value in installed_names.items()
    }
    with plist_path.open("wb") as fh:
        plistlib.dump({"EnvironmentVariables": installed}, fh, fmt=plistlib.FMT_XML)
    environ = {
        key: str(tmp_path / value) if value.strip() else value
        for key, value in environ_names.items()
    }

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout,
            mandrel_path=tmp_path / explicit_name if explicit_name else None,
            plist_path=plist_path,
        ),
        environ=environ,
        token_generator=lambda: "token",
    )

    assert deployment.mandrel_path == tmp_path / expected_name


def test_first_deploy_requires_explicit_mandrel_path(tmp_path):
    checkout = make_checkout(tmp_path)

    with pytest.raises(LaunchdError, match="pass --mandrel or set") as error:
        resolve_deployment(
            DeployOptions(checkout=checkout, plist_path=tmp_path / PLIST_NAME),
            environ={},
            token_generator=lambda: "token",
        )

    assert MANDREL_PATH_ENV in str(error.value)
    assert LEGACY_MANDREL_PATH_ENV in str(error.value)


def test_deploy_preserves_existing_plist_token(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "com.qinglin89.orch-hub.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "EnvironmentVariables": {
                    TOKEN_ENV: "old-token",
                    MANDREL_PATH_ENV: str(tmp_path / "installed-mandrel"),
                }
            },
            fh,
            fmt=plistlib.FMT_XML,
        )

    result = deploy_service(
        DeployOptions(
            checkout=checkout,
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
            install_package=False,
        ),
        runner=FakeRunner(),
        environ={},
        token_generator=lambda: "new-token",
    )

    assert result.generated_token is False
    assert result.deployment.token_source == "existing plist"
    assert result.deployment.mandrel_path == tmp_path / "installed-mandrel"
    assert read_plist(plist_path)["EnvironmentVariables"][TOKEN_ENV] == "old-token"


BARK_URL = "https://bark.example/DEVICEKEY"


def test_deploy_writes_bark_url_from_environment(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME

    result = deploy_service(
        DeployOptions(
            checkout=checkout,
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
            install_package=False,
        ),
        runner=FakeRunner(),
        environ={
            BARK_URL_ENV: BARK_URL,
            MANDREL_PATH_ENV: str(tmp_path / "mandrel"),
        },
        token_generator=lambda: "t",
    )

    assert result.deployment.bark_url == BARK_URL
    assert read_plist(plist_path)["EnvironmentVariables"][BARK_URL_ENV] == BARK_URL


def test_deploy_renders_blank_bark_url_when_unset(tmp_path):
    # Optional: an unset Bark URL renders as an empty string (push disabled),
    # never a leftover REPLACE- placeholder.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME

    result = deploy_service(
        DeployOptions(
            checkout=checkout,
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
            install_package=False,
        ),
        runner=FakeRunner(),
        environ={MANDREL_PATH_ENV: str(tmp_path / "mandrel")},
        token_generator=lambda: "t",
    )

    assert result.deployment.bark_url is None
    assert read_plist(plist_path)["EnvironmentVariables"][BARK_URL_ENV] == ""
    assert "REPLACE-" not in plist_path.read_text(encoding="utf-8")


def test_deploy_preserves_existing_plist_bark_url(tmp_path):
    # Like the token: an update that forgets to re-export ORCH_HUB_BARK_URL keeps
    # the value already installed rather than silently disabling push.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "EnvironmentVariables": {
                    TOKEN_ENV: "tok",
                    BARK_URL_ENV: BARK_URL,
                    MANDREL_PATH_ENV: str(tmp_path / "mandrel"),
                }
            },
            fh,
            fmt=plistlib.FMT_XML,
        )

    result = deploy_service(
        DeployOptions(
            checkout=checkout,
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
            install_package=False,
        ),
        runner=FakeRunner(),
        environ={},  # no ORCH_HUB_BARK_URL in env -> preserve the installed value
        token_generator=lambda: "unused",
    )

    assert result.deployment.bark_url == BARK_URL
    assert read_plist(plist_path)["EnvironmentVariables"][BARK_URL_ENV] == BARK_URL


def test_installed_bark_url_reads_stripped_plist_value(tmp_path):
    # `notify test` from a plain shell resolves the Bark URL the resident
    # service carries in its installed plist; surrounding whitespace is stripped.
    plist_path = tmp_path / PLIST_NAME
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {"EnvironmentVariables": {BARK_URL_ENV: f"  {BARK_URL}  "}},
            fh,
            fmt=plistlib.FMT_XML,
        )

    assert installed_bark_url(plist_path=plist_path) == BARK_URL


def test_installed_bark_url_none_when_absent_or_blank(tmp_path):
    # Missing plist -> None (nothing installed).
    assert installed_bark_url(plist_path=tmp_path / "absent.plist") is None

    # Present but blank value -> None (blank counts as unset, like the env).
    blank = tmp_path / "blank.plist"
    with blank.open("wb") as fh:
        plistlib.dump(
            {"EnvironmentVariables": {BARK_URL_ENV: "   "}},
            fh,
            fmt=plistlib.FMT_XML,
        )
    assert installed_bark_url(plist_path=blank) is None


def test_resolve_leftover_placeholders_blanks_unknown_but_fails_required():
    # The reported update skew: an older installed CLI renders a newer template
    # carrying REPLACE-ORCH-HUB-BARK-URL it does not know. Treated as unknown
    # (not in the required set), an optional placeholder is blanked, not fatal.
    required = frozenset({"REPLACE-BEARER-TOKEN"})
    blanked = _resolve_leftover_placeholders(
        {"EnvironmentVariables": {"ORCH_HUB_BARK_URL": "REPLACE-ORCH-HUB-BARK-URL"}},
        required=required,
    )
    assert blanked["EnvironmentVariables"]["ORCH_HUB_BARK_URL"] == ""

    # A leftover REQUIRED placeholder still fails loudly.
    with pytest.raises(
        LaunchdError, match="unreplaced launchd template placeholder: REPLACE-BEARER-TOKEN"
    ):
        _resolve_leftover_placeholders(
            ["prefix REPLACE-BEARER-TOKEN suffix"], required=required
        )


def test_shipped_template_still_renders_under_the_previous_renderer():
    """The other half of the skew: a newer template, an older installed CLI.

    Before ``ProgramArguments`` was rendered from ``program_argv``, the renderer
    substituted a fixed set of REPLACE- tokens and blanked every token it did
    not recognize — the program array included. A template whose program value
    used a token that renderer never learned would therefore render
    ``ProgramArguments: [""]``, and an old ``orch-hub launchd deploy`` would
    write it, boot out the working service, and fail to start the replacement.

    The prior contract is restated here rather than called: what has to stay
    compatible is the checked-in template, and the code that would render it is
    by definition not this checkout's.
    """
    checkout = Path("/opt/orch-hub")
    legacy_required = {
        "REPLACE-ORCH-HUB-CHECKOUT": str(checkout),
        "REPLACE-BEARER-TOKEN": "token",
        "REPLACE-ORCH-HUB-HOST": "127.0.0.1",
        "REPLACE-ORCH-HUB-PORT": "8765",
        "REPLACE-MANDREL-CHECKOUT": "/opt/mandrel",
        "REPLACE-ORCH-HUB-DATA-DIR": "/opt/data",
        "REPLACE-ORCH-HUB-LOG-DIR": "/opt/logs",
        "REPLACE-PATH": "/usr/bin:/bin",
    }
    template = read_plist(Path(__file__).parents[1] / TEMPLATE_RELATIVE_PATH)

    rendered = _resolve_leftover_placeholders(
        launchd._replace_placeholders(template, legacy_required),
        required=frozenset(legacy_required),
    )

    assert rendered["ProgramArguments"] == [
        str(checkout / ".venv" / "bin" / "orch-hub-api")
    ]
    assert "REPLACE-" not in plistlib.dumps(rendered).decode("utf-8")


def test_deploy_tolerates_unknown_optional_template_placeholder(tmp_path):
    # Old-code / new-template skew end to end: a checkout whose launchd template
    # adds an optional env placeholder the installed code predates (the reported
    # case was REPLACE-ORCH-HUB-BARK-URL). Rendering blanks it and the deploy
    # still bootstraps instead of aborting with "unreplaced launchd template
    # placeholder".
    checkout = make_checkout(tmp_path)
    template = checkout / TEMPLATE_RELATIVE_PATH
    data = read_plist(template)
    data["EnvironmentVariables"]["ORCH_HUB_FUTURE_OPTION"] = (
        "REPLACE-ORCH-HUB-FUTURE-OPTION"
    )
    with template.open("wb") as fh:
        plistlib.dump(data, fh, fmt=plistlib.FMT_XML)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME
    runner = FakeRunner()

    deploy_service(
        _deploy_options(checkout, plist_path, tmp_path),
        runner=runner,
        environ={},
        token_generator=lambda: "t",
    )

    rendered = read_plist(plist_path)
    assert rendered["EnvironmentVariables"]["ORCH_HUB_FUTURE_OPTION"] == ""
    assert "REPLACE-" not in plist_path.read_text(encoding="utf-8")
    assert (
        "launchctl",
        "bootstrap",
        service_domain(os.getuid()),
        str(plist_path),
    ) in runner.calls


def test_deploy_fails_on_required_placeholder_before_writing(tmp_path, monkeypatch):
    # If a required substitution does not happen (simulated by neutralizing the
    # replacement pass), rendering fails loudly BEFORE the plist is written or
    # any plutil/launchctl/pip command runs.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME
    runner = FakeRunner()
    monkeypatch.setattr(
        "orch_hub.launchd._replace_placeholders", lambda value, replacements: value
    )

    with pytest.raises(
        LaunchdError, match="unreplaced launchd template placeholder"
    ):
        deploy_service(
            _deploy_options(checkout, plist_path, tmp_path),
            runner=runner,
            environ={},
            token_generator=lambda: "t",
        )

    assert not plist_path.exists()
    assert runner.calls == []


def test_deploy_installs_package_when_requested(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "com.qinglin89.orch-hub.plist"
    runner = FakeRunner()

    deploy_service(
        DeployOptions(
            checkout=checkout,
            mandrel_path=tmp_path / "mandrel",
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
        ),
        runner=runner,
        environ={},
        token_generator=lambda: "token",
    )

    assert runner.calls[:2] == [
        (sys.executable, "-m", "venv", str(checkout / ".venv")),
        (
            str(checkout / ".venv/bin/python"),
            "-m",
            "pip",
            "install",
            str(checkout),
        ),
    ]


def test_deploy_requires_checkout_template(tmp_path):
    checkout = tmp_path / "orch-hub"
    (checkout / "orch_hub").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\nname = \"orch-hub\"\n", encoding="utf-8"
    )
    runner = FakeRunner()

    with pytest.raises(LaunchdError, match="launchd template not found"):
        deploy_service(
            DeployOptions(
                checkout=checkout,
                mandrel_path=tmp_path / "mandrel",
                plist_path=tmp_path / PLIST_NAME,
                install_package=False,
            ),
            runner=runner,
            environ={},
            token_generator=lambda: "token",
        )
    assert runner.calls == []


def _deploy_options(checkout: Path, plist_path: Path, tmp_path: Path) -> DeployOptions:
    return DeployOptions(
        checkout=checkout,
        mandrel_path=tmp_path / "mandrel",
        plist_path=plist_path,
        log_dir=tmp_path / "logs",
        install_package=False,
    )


def test_deploy_waits_for_service_unload_before_bootstrap(tmp_path):
    # A live service is unloaded (bootout ok). launchd tears the job down
    # asynchronously, so deploy polls `launchctl print` until the label is gone
    # before bootstrapping into the domain.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME
    runner = ScriptedLaunchctl(
        bootout=[(0, "ok", "")],
        prints=[
            (0, "loaded", ""),
            (0, "loaded", ""),
            (113, "", "Could not find service"),
        ],
        bootstrap=[(0, "ok", "")],
    )

    deploy_service(
        _deploy_options(checkout, plist_path, tmp_path),
        runner=runner,
        environ={},
        token_generator=lambda: "t",
        sleeper=runner.sleeper,
    )

    assert runner.calls == [
        ("plutil", "-lint", str(plist_path)),
        ("launchctl", "bootout", service_target()),
        ("launchctl", "print", service_target()),
        ("launchctl", "print", service_target()),
        ("launchctl", "print", service_target()),
        ("launchctl", "bootstrap", service_domain(os.getuid()), str(plist_path)),
    ]
    assert runner.sleeps == [UNLOAD_POLL_INTERVAL, UNLOAD_POLL_INTERVAL]


def test_deploy_skips_unload_wait_when_nothing_was_loaded(tmp_path):
    # Fresh deploy: bootout reports "not loaded", so there is no teardown to
    # wait for and no `launchctl print` probe is issued.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME
    runner = ScriptedLaunchctl(
        bootout=[(113, "", "No such process")],
        bootstrap=[(0, "ok", "")],
    )

    deploy_service(
        _deploy_options(checkout, plist_path, tmp_path),
        runner=runner,
        environ={},
        token_generator=lambda: "t",
        sleeper=runner.sleeper,
    )

    assert runner.calls_of("print") == []
    assert runner.sleeps == []


def test_deploy_retries_transient_bootstrap_exit5(tmp_path):
    # The first bootstrap fails with transient exit 5; a single retry succeeds
    # without the operator rerunning update.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / "LaunchAgents" / PLIST_NAME
    runner = ScriptedLaunchctl(
        bootout=[(113, "", "No such process")],
        bootstrap=[
            (5, "", "Bootstrap failed: 5: Input/output error"),
            (0, "ok", ""),
        ],
    )

    result = deploy_service(
        _deploy_options(checkout, plist_path, tmp_path),
        runner=runner,
        environ={},
        token_generator=lambda: "t",
        sleeper=runner.sleeper,
    )

    assert len(runner.calls_of("bootstrap")) == 2
    assert runner.sleeps == [BOOTSTRAP_RETRY_INTERVAL]
    assert result.deployment.plist_path == plist_path


def test_deploy_raises_when_bootstrap_never_recovers(tmp_path):
    # A persistent transient failure is retried up to the bound, then raises a
    # clear non-zero CLI error instead of looping forever.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    runner = ScriptedLaunchctl(
        bootout=[(113, "", "No such process")],
        bootstrap=[(5, "", "Input/output error")],
    )

    with pytest.raises(LaunchdError, match="bootstrap launchd service failed"):
        deploy_service(
            _deploy_options(checkout, plist_path, tmp_path),
            runner=runner,
            environ={},
            token_generator=lambda: "t",
            sleeper=runner.sleeper,
        )

    assert len(runner.calls_of("bootstrap")) == BOOTSTRAP_MAX_ATTEMPTS
    assert len(runner.sleeps) == BOOTSTRAP_MAX_ATTEMPTS - 1


def test_deploy_does_not_retry_nontransient_bootstrap_failure(tmp_path):
    # A non-transient bootstrap failure (not exit 5, no I/O text) raises at once
    # without consuming the retry budget.
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    runner = ScriptedLaunchctl(
        bootout=[(113, "", "No such process")],
        bootstrap=[(9, "", "Bootstrap failed: 9: Permission denied")],
    )

    with pytest.raises(LaunchdError, match="Permission denied"):
        deploy_service(
            _deploy_options(checkout, plist_path, tmp_path),
            runner=runner,
            environ={},
            token_generator=lambda: "t",
            sleeper=runner.sleeper,
        )

    assert len(runner.calls_of("bootstrap")) == 1
    assert runner.sleeps == []


def test_is_transient_bootstrap_failure_matches_exit5_and_io_text():
    assert _is_transient_bootstrap_failure(CommandResult((), 5, "", "boom"))
    assert _is_transient_bootstrap_failure(
        CommandResult((), 1, "", "Input/Output error")
    )
    assert not _is_transient_bootstrap_failure(
        CommandResult((), 9, "", "permission denied")
    )


def test_stop_treats_absent_launchd_job_as_already_stopped():
    runner = FakeRunner(bootout_not_loaded=True)

    result = stop_service(runner=runner)

    assert result.stopped is False
    assert result.already_stopped is True
    assert runner.calls == [("launchctl", "bootout", service_target())]


def test_start_bootstraps_installed_plist_when_unloaded(tmp_path):
    plist_path = tmp_path / PLIST_NAME
    plist_path.write_text("plist", encoding="utf-8")
    runner = FakeRunner(print_loaded=False)

    result = start_service(plist_path=plist_path, runner=runner)

    assert result.started is True
    assert result.already_started is False
    assert runner.calls == [
        ("launchctl", "print", service_target()),
        ("launchctl", "bootstrap", service_domain(os.getuid()), str(plist_path)),
    ]


def test_start_is_idempotent_when_already_loaded(tmp_path):
    plist_path = tmp_path / PLIST_NAME
    plist_path.write_text("plist", encoding="utf-8")
    runner = FakeRunner(print_loaded=True)

    result = start_service(plist_path=plist_path, runner=runner)

    assert result.started is False
    assert result.already_started is True
    assert runner.calls == [("launchctl", "print", service_target())]


def test_restart_stops_then_starts_installed_plist(tmp_path):
    plist_path = tmp_path / PLIST_NAME
    plist_path.write_text("plist", encoding="utf-8")
    runner = FakeRunner(bootout_not_loaded=False, print_loaded=False)

    result = restart_service(plist_path=plist_path, runner=runner)

    assert result.stop.stopped is True
    assert result.start.started is True
    assert runner.calls == [
        ("launchctl", "bootout", service_target()),
        ("launchctl", "print", service_target()),
        ("launchctl", "bootstrap", service_domain(os.getuid()), str(plist_path)),
    ]


def test_stop_raises_on_real_bootout_failure():
    def failing_runner(args, **kwargs):
        argv = tuple(str(arg) for arg in args)
        return CommandResult(argv, 5, "", "permission denied")

    with pytest.raises(LaunchdError, match="permission denied"):
        stop_service(runner=failing_runner)


def test_status_reads_plist_and_health(tmp_path):
    plist_path = tmp_path / "com.qinglin89.orch-hub.plist"
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "ProgramArguments": ["/tmp/orch-hub-api"],
                "EnvironmentVariables": {HOST_ENV: "127.0.0.1", PORT_ENV: "9001"},
            },
            fh,
            fmt=plistlib.FMT_XML,
        )
    runner = FakeRunner(bootout_not_loaded=False, print_loaded=True)

    result = status_service(
        plist_path=plist_path,
        runner=runner,
        health_checker=lambda host, port: f"ok {host}:{port}",
    )

    assert result.loaded is True
    assert result.plist_installed is True
    assert result.program == "/tmp/orch-hub-api"
    assert result.dashboard_url == "http://127.0.0.1:9001/"
    assert result.health == "ok 127.0.0.1:9001"
    assert runner.calls == [("launchctl", "print", service_target())]


def test_template_path_reads_the_checkout_in_a_source_runtime(tmp_path):
    checkout = make_checkout(tmp_path)

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout,
            mandrel_path=tmp_path / "mandrel",
            token="t",
            plist_path=tmp_path / "agent.plist",
            log_dir=tmp_path / "logs",
        ),
        environ={},
    )

    assert deployment.template_path == checkout / TEMPLATE_RELATIVE_PATH


def test_template_path_prefers_a_bundled_copy_when_frozen(tmp_path, monkeypatch):
    # A compiled release has no checkout to read the template from; the build
    # copies it into the bundle under the same relative path. Preferring the
    # bundled copy also stops a frozen runtime from rendering against whatever
    # checkout path happens to be recorded, so the checkout here keeps its own
    # (distinguishable) template and must lose.
    checkout = make_checkout(tmp_path)
    bundle = tmp_path / "bundle"
    bundled_template = bundle / TEMPLATE_RELATIVE_PATH
    bundled_template.parent.mkdir(parents=True)
    shutil.copyfile(checkout / TEMPLATE_RELATIVE_PATH, bundled_template)
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resources.sys, "_MEIPASS", str(bundle), raising=False)

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout,
            mandrel_path=tmp_path / "mandrel",
            token="t",
            plist_path=tmp_path / "agent.plist",
            log_dir=tmp_path / "logs",
        ),
        environ={},
    )

    assert deployment.template_path == bundled_template
    assert deployment.template_path != checkout / TEMPLATE_RELATIVE_PATH


# --------------------------------------------------------------------------
# deploying an installed runtime instead of a checkout
# --------------------------------------------------------------------------


def make_installed_runtime(tmp_path: Path) -> Path:
    """A stand-in for `<prefix>/current/orch-hub`: just an executable file."""
    program = tmp_path / "prefix" / "current" / "orch-hub"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    return program


def test_deploy_runs_an_installed_runtime_with_serve(tmp_path, monkeypatch):
    """A compiled release has one executable and no console scripts."""
    program = make_installed_runtime(tmp_path)
    template = Path(__file__).parents[1] / TEMPLATE_RELATIVE_PATH
    monkeypatch.setattr(launchd, "bundled_resource", lambda relative: template)
    plist_path = tmp_path / PLIST_NAME
    runner = FakeRunner()

    result = deploy_service(
        DeployOptions(
            program=program,
            mandrel_path=tmp_path / "mandrel",
            token="t",
            plist_path=plist_path,
            log_dir=tmp_path / "logs",
        ),
        runner=runner,
        environ={},
    )

    assert result.deployment.checkout is None
    assert result.deployment.program == str(program)
    assert read_plist(plist_path)["ProgramArguments"] == [str(program), "serve"]
    # install_package defaults to True, but there is no package to install.
    assert not any("pip" in call for call in runner.calls)
    assert not any("venv" in call for call in runner.calls)


def test_deploy_refuses_a_missing_installed_runtime(tmp_path):
    with pytest.raises(LaunchdError, match="installed runtime not found"):
        resolve_deployment(
            DeployOptions(
                program=tmp_path / "absent" / "orch-hub",
                mandrel_path=tmp_path / "mandrel",
                token="t",
            ),
            environ={},
        )


def test_installed_deployment_has_no_virtualenv(tmp_path, monkeypatch):
    program = make_installed_runtime(tmp_path)
    template = Path(__file__).parents[1] / TEMPLATE_RELATIVE_PATH
    monkeypatch.setattr(launchd, "bundled_resource", lambda relative: template)

    deployment = resolve_deployment(
        DeployOptions(
            program=program,
            mandrel_path=tmp_path / "mandrel",
            token="t",
            plist_path=tmp_path / PLIST_NAME,
        ),
        environ={},
    )

    with pytest.raises(LaunchdError, match="not a checkout virtualenv"):
        _ = deployment.venv_python


def test_template_path_without_a_bundle_or_a_checkout(tmp_path, monkeypatch):
    program = make_installed_runtime(tmp_path)
    monkeypatch.setattr(launchd, "bundled_resource", lambda relative: None)

    deployment = resolve_deployment(
        DeployOptions(
            program=program,
            mandrel_path=tmp_path / "mandrel",
            token="t",
            plist_path=tmp_path / PLIST_NAME,
        ),
        environ={},
    )

    with pytest.raises(LaunchdError, match="no launchd template available"):
        _ = deployment.template_path


def test_render_requires_a_program_arguments_array():
    with pytest.raises(LaunchdError, match="no ProgramArguments array"):
        launchd._with_program_arguments({"Label": LABEL}, ("/bin/true",))


def test_find_checkout_points_a_release_at_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "is_frozen", lambda: True)

    with pytest.raises(LaunchdError, match="`orch-hub setup`"):
        launchd.find_checkout(tmp_path)


# --------------------------------------------------------------------------
# preserving an installed service's configuration
# --------------------------------------------------------------------------


def install_plist(path: Path, env: dict[str, str]) -> None:
    with path.open("wb") as fh:
        plistlib.dump(
            {"Label": LABEL, "ProgramArguments": ["/x"], "EnvironmentVariables": env},
            fh,
            fmt=plistlib.FMT_XML,
        )


def test_update_inherits_host_port_and_data_dir_from_the_installed_plist(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    install_plist(
        plist_path,
        {
            HOST_ENV: "100.64.0.5",
            PORT_ENV: "9101",
            DATA_DIR_ENV: str(tmp_path / "hub-data"),
            MANDREL_PATH_ENV: str(tmp_path / "mandrel"),
            TOKEN_ENV: "installed-token",
        },
    )

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout, plist_path=plist_path, log_dir=tmp_path / "logs"
        ),
        environ={},
    )

    assert deployment.host == "100.64.0.5"
    assert deployment.port == 9101
    assert deployment.data_dir == (tmp_path / "hub-data").resolve()
    assert deployment.token == "installed-token"


def test_environment_wins_over_the_installed_plist(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    install_plist(
        plist_path,
        {HOST_ENV: "100.64.0.5", PORT_ENV: "9101", MANDREL_PATH_ENV: str(tmp_path)},
    )

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout, plist_path=plist_path, log_dir=tmp_path / "logs"
        ),
        environ={HOST_ENV: "127.0.0.1", PORT_ENV: "9999"},
    )

    assert (deployment.host, deployment.port) == ("127.0.0.1", 9999)


def test_a_blank_environment_value_does_not_reset_a_configured_service(tmp_path):
    """Blank counts as unset, so an empty export must not silently rebind."""
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    install_plist(
        plist_path,
        {HOST_ENV: "100.64.0.5", PORT_ENV: "9101", MANDREL_PATH_ENV: str(tmp_path)},
    )

    deployment = resolve_deployment(
        DeployOptions(
            checkout=checkout, plist_path=plist_path, log_dir=tmp_path / "logs"
        ),
        environ={HOST_ENV: "  ", PORT_ENV: ""},
    )

    assert (deployment.host, deployment.port) == ("100.64.0.5", 9101)


def test_an_installed_port_is_parsed_fail_closed(tmp_path):
    checkout = make_checkout(tmp_path)
    plist_path = tmp_path / PLIST_NAME
    install_plist(
        plist_path, {PORT_ENV: "not-a-port", MANDREL_PATH_ENV: str(tmp_path)}
    )

    with pytest.raises(LaunchdError, match=PORT_ENV):
        resolve_deployment(
            DeployOptions(checkout=checkout, plist_path=plist_path), environ={}
        )


def test_installed_data_dir_and_bind(tmp_path):
    plist_path = tmp_path / PLIST_NAME
    install_plist(
        plist_path,
        {HOST_ENV: "100.64.0.5", PORT_ENV: "9101", DATA_DIR_ENV: str(tmp_path / "d")},
    )

    assert launchd.installed_data_dir(plist_path=plist_path) == tmp_path / "d"
    assert launchd.installed_bind(plist_path=plist_path) == ("100.64.0.5", 9101)


def test_installed_data_dir_and_bind_fall_back_without_a_plist(tmp_path):
    missing = tmp_path / "absent.plist"

    assert launchd.installed_data_dir(plist_path=missing) == DEFAULT_DATA_DIR
    assert launchd.installed_bind(plist_path=missing) == (DEFAULT_HOST, DEFAULT_PORT)


def test_installed_readers_fail_closed_on_a_malformed_plist(tmp_path):
    """"Not configured" and "cannot be parsed" must not answer the same.

    A purge deletes what ``installed_data_dir`` names and a rollback probes what
    ``installed_bind`` names; defaulting a corrupt plist would delete a
    directory the service never used and call an unrelated endpoint healthy.
    """
    broken = tmp_path / PLIST_NAME
    broken.write_text("<plist>not really", encoding="utf-8")

    with pytest.raises(LaunchdError, match="cannot read the installed launchd plist"):
        launchd.installed_data_dir(plist_path=broken)
    with pytest.raises(LaunchdError, match="cannot read the installed launchd plist"):
        launchd.installed_bind(plist_path=broken)


@pytest.mark.parametrize("port", ["not-a-port", "0", "70000", "-1"])
def test_installed_bind_refuses_an_unusable_port(tmp_path, port):
    plist_path = tmp_path / PLIST_NAME
    install_plist(plist_path, {HOST_ENV: "127.0.0.1", PORT_ENV: port})

    with pytest.raises(LaunchdError, match=PORT_ENV):
        launchd.installed_bind(plist_path=plist_path)


def test_status_still_reports_a_malformed_plist_instead_of_failing(tmp_path):
    """Status is diagnostic: it guesses *and* shows the parse error."""
    broken = tmp_path / PLIST_NAME
    broken.write_text("<plist>not really", encoding="utf-8")

    result = status_service(
        plist_path=broken,
        runner=FakeRunner(print_loaded=False),
        health_checker=lambda host, port: "unreachable: refused",
    )

    assert result.plist_error
    assert (result.host, result.port) == (DEFAULT_HOST, DEFAULT_PORT)


# --------------------------------------------------------------------------
# mandrel verification and health waiting
# --------------------------------------------------------------------------


def test_verify_mandrel_checkout(tmp_path):
    mandrel = tmp_path / "mandrel"
    (mandrel / "bin").mkdir(parents=True)
    entry = mandrel / "bin" / "mandrel"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    entry.chmod(0o755)

    launchd.verify_mandrel_checkout(mandrel)

    with pytest.raises(LaunchdError, match="mandrel checkout not found"):
        launchd.verify_mandrel_checkout(tmp_path / "absent")
    (tmp_path / "bare").mkdir()
    with pytest.raises(LaunchdError, match="does not look like a mandrel checkout"):
        launchd.verify_mandrel_checkout(tmp_path / "bare")


def test_verify_mandrel_checkout_requires_an_executable_entry_point(tmp_path):
    """The registry *runs* bin/mandrel; a readable file is not enough.

    A non-executable entry point passes a file test and then fails at every
    registry read, which shows up as the empty repo list this check exists to
    make impossible.
    """
    mandrel = tmp_path / "mandrel"
    (mandrel / "bin").mkdir(parents=True)
    entry = mandrel / "bin" / "mandrel"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    entry.chmod(0o644)

    with pytest.raises(LaunchdError, match="not executable"):
        launchd.verify_mandrel_checkout(mandrel)


def test_deploy_can_require_a_real_mandrel_checkout(tmp_path):
    checkout = make_checkout(tmp_path)

    with pytest.raises(LaunchdError, match="does not look like a mandrel checkout"):
        resolve_deployment(
            DeployOptions(
                checkout=checkout,
                mandrel_path=tmp_path,
                token="t",
                plist_path=tmp_path / PLIST_NAME,
                verify_mandrel=True,
            ),
            environ={},
        )


def test_wait_for_health_polls_until_the_service_answers():
    answers = ["unreachable: refused", "unreachable: refused", "ok"]
    sleeps: list[float] = []

    result = launchd.wait_for_health(
        "127.0.0.1",
        8765,
        checker=lambda host, port: answers.pop(0),
        sleeper=sleeps.append,
    )

    assert result == "ok"
    assert sleeps == [launchd.HEALTH_POLL_INTERVAL] * 2


def test_wait_for_health_reports_the_last_failure():
    sleeps: list[float] = []

    result = launchd.wait_for_health(
        "127.0.0.1",
        8765,
        timeout=1.0,
        interval=0.5,
        checker=lambda host, port: "unreachable: refused",
        sleeper=sleeps.append,
    )

    assert result == "unreachable: refused"
    assert len(sleeps) <= 2

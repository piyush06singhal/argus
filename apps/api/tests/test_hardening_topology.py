"""ARGUS hardening W6 — deployment topology: one leader for the background work.

The audit's G12 finding was that ``API_WORKERS`` defaults to 4 while nine
independent background timers start inside every process. The mutexes make that
*safe*, but "safe" is not the same as "intended": four passes over the same work
is four times the load for one outcome.

The fix is one flag, read in one place. These tests pin the decision table,
because a planner that quietly started running sweeps in an HTTP process again
would be invisible in production until the load arrived.
"""

from __future__ import annotations

from app.core.config import Settings


class TestBackgroundJobsGate:
    def test_the_unit_suite_never_runs_background_timers(self):
        """Tests must not race a clock."""
        assert Settings(API_ENVIRONMENT="test").background_jobs_active is False

    def test_development_runs_them_by_default(self):
        """One container is the documented default and must behave like one."""
        assert Settings(API_ENVIRONMENT="development").background_jobs_active is True

    def test_an_http_only_process_opts_out_explicitly(self):
        """The split topology: serve here, work in the dedicated container."""
        settings = Settings(API_ENVIRONMENT="production", BACKGROUND_JOBS_ENABLED=False)
        assert settings.background_jobs_active is False

    def test_the_worker_process_stays_on(self):
        settings = Settings(API_ENVIRONMENT="production", BACKGROUND_JOBS_ENABLED=True)
        assert settings.background_jobs_active is True

    def test_the_default_is_on_so_a_single_container_still_works(self):
        """Off-by-default would silently break a one-container install."""
        assert Settings().BACKGROUND_JOBS_ENABLED is True


class TestDeploymentSettingsExistAndAreDocumented:
    """A production topology the operator can express is a topology they can run."""

    def test_api_workers_is_configurable(self):
        assert Settings(API_WORKERS=1).API_WORKERS == 1

    def test_auth_cannot_be_disabled_in_production(self):
        import pytest

        with pytest.raises(ValueError, match="AUTH_DISABLED"):
            Settings(API_ENVIRONMENT="production", AUTH_DISABLED=True)

    def test_the_demo_seed_is_off_in_production_unless_asked_for(self):
        assert Settings(API_ENVIRONMENT="production").seed_demo_enabled is False
        assert Settings(API_ENVIRONMENT="development").seed_demo_enabled is True

    def test_request_body_and_json_depth_limits_are_configured(self):
        settings = Settings()
        assert settings.MAX_REQUEST_BODY_BYTES > 0
        assert settings.MAX_JSON_DEPTH > 0

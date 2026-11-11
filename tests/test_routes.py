import pytest
from unittest.mock import patch
from app import app

TEST_USER_UUID = "00000000-0000-0000-0000-000000000001"
TEST_SESSION_RECORD = {
    "id": "00000000-0000-0000-0000-000000000002",
    "user_id": TEST_USER_UUID,
    "email": "test@example.com",
    "role": "user",
    "full_name": "Test User",
    "extra_roles": ["live_fleet_view"],
    "is_primary_super_admin": False,
}

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = TEST_USER_UUID
            sess["session_id"] = TEST_SESSION_RECORD["id"]
            sess["email"] = TEST_SESSION_RECORD["email"]
            sess["role"] = TEST_SESSION_RECORD["role"]
            sess["extra_roles"] = TEST_SESSION_RECORD["extra_roles"]
        yield c

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json["status"] == "ok"

# Patch where the function was imported INTO, not where it was defined.
@patch("blueprints.live_fleet.get_fleet_state")
@patch("blueprints.live_fleet._load_facilities_diagnostic")
def test_live_fleet_api(mock_facilities, mock_get_fleet, client):
    mock_get_fleet.return_value = (
        [{"imei": "123", "lat": 5.0, "lng": -1.0,
          "direction": "unknown", "motion": "moving"}],
        False, False, {},
    )
    mock_facilities.return_value = ([], [], None)

    with patch("services.sessions.load", return_value=TEST_SESSION_RECORD):
        # has_role() also calls get_user_profile to double-check extra_roles
        with patch("services.auth.get_user_profile",
                   return_value={"extra_roles": ["live_fleet_view"]}):
            r = client.get("/live/api/fleet")

    assert r.status_code == 200
    assert "trucks" in r.json
    assert len(r.json["trucks"]) == 1

def test_unauthorized_download(client):
    # The route calls app.jobs.get_report_owner — patch that exact path.
    with patch("app.jobs.get_report_owner", return_value="other-user-id"):
        with patch("services.sessions.load", return_value=TEST_SESSION_RECORD):
            r = client.get("/download/fake.xlsx")
            assert r.status_code == 403
import pytest


@pytest.fixture(autouse=True)
def generic_musicbrainz_contact(monkeypatch):
    """The live lookup only puts the contact in a User-Agent, so a generic address serves and the suite runs
    without a `.env`. Tests that need it absent delete it themselves."""
    monkeypatch.setenv("MUSICBRAINZ_CONTACT", "music-agent@example.com")

"""extract_org_context: pure logic, no DB, no network — genuinely run in
any environment. Covers the exact bug this replaced: Phase 1's deps.py
read claims.get("org_id"), which is the OLD flat Clerk claim shape. The
current shape (checked against Clerk's docs, not assumed) nests org data
under a compact `o` claim to keep JWTs small.
"""
from app.core.security import extract_org_context


def test_current_nested_o_claim_shape():
    """The shape a current Clerk session token actually uses."""
    claims = {"sub": "user_123", "o": {"id": "org_abc", "rol": "admin", "slg": "grace-church"}}
    org = extract_org_context(claims)
    assert org is not None
    assert org.org_id == "org_abc"
    assert org.org_role == "admin"
    assert org.org_slug == "grace-church"


def test_role_with_org_prefix_is_stripped():
    """Some SDK versions/paths may emit 'org:admin' rather than 'admin' —
    normalize either way, since our own role-mapping logic (app-managed
    roles, not Clerk custom roles per the Phase 2 decision) shouldn't have
    to know which one it got."""
    claims = {"o": {"id": "org_abc", "rol": "org:admin", "slg": "grace-church"}}
    org = extract_org_context(claims)
    assert org is not None
    assert org.org_role == "admin"


def test_legacy_flat_claim_shape_still_works():
    """Defensive fallback for the older flat org_id/org_role/org_slug
    shape — not what a current Clerk app sends by default, but cheap
    insurance against an older SDK path."""
    claims = {"sub": "user_123", "org_id": "org_xyz", "org_role": "org:member", "org_slug": "hope-chapel"}
    org = extract_org_context(claims)
    assert org is not None
    assert org.org_id == "org_xyz"
    assert org.org_role == "member"
    assert org.org_slug == "hope-chapel"


def test_no_active_organization_returns_none():
    """A verified session with no org context at all — a legitimate
    state (a user not currently in an org), not an error. Callers must
    treat this as 'no tenant', which app/core/deps.py does by raising 403,
    not by crashing here."""
    claims = {"sub": "user_123"}
    assert extract_org_context(claims) is None


def test_o_claim_present_but_empty_dict():
    """An 'o' key that exists but has no id — same as no org context."""
    claims = {"sub": "user_123", "o": {}}
    assert extract_org_context(claims) is None
